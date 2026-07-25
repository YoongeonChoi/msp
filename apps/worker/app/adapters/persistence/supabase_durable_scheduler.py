from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import datetime
from typing import Literal, Never, TypeVar, cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from app.adapters.persistence.supabase_durable_scheduler_wire import (
    DurableSchedulerWireCodec,
)
from app.application.ports.durable_scheduler_port import (
    DURABLE_SCHEDULER_TOTAL_RPC_TIMEOUT_SECONDS,
    SchedulerMutationOutcomeUnknownError,
    SchedulerTransitionRejectedError,
    canonical_scheduler_outer_lease,
)
from app.application.ports.persistence_authority import (
    PersistenceAuthority,
    persistence_authority_fingerprint,
)
from app.config import Settings
from app.domain.common.json import JsonObject
from app.domain.execution_v2.models import WorkerLease
from app.domain.scheduler.models import (
    SCHEDULER_RETRYABLE_REASONS,
    ScheduledJobClaimReceiptV1,
    ScheduledJobClaimV1,
    ScheduledJobCompletionReceiptV1,
    ScheduledJobDefinitionReceiptV1,
    ScheduledJobDefinitionV1,
    ScheduledJobFailureReceiptV1,
    SchedulerDeadLetterInspectionReceiptV1,
    SchedulerDefinitionConvergenceReceiptV1,
    SchedulerInvariantError,
    SchedulerReplayAssessmentV1,
    SchedulerReplayReceiptV1,
    canonical_scheduler_claim,
    canonical_scheduler_definition,
    canonical_scheduler_replay_assessment,
    scheduler_definition_budget_is_safe,
    scheduler_replay_budget_is_safe,
    scheduler_retry_delay,
)
from app.infrastructure.bounded_json import BoundedJsonError, bounded_json_response
from app.infrastructure.release_metadata import worker_release_metadata
from app.infrastructure.supabase_headers import supabase_api_headers

DurableSchedulerRpc = Literal[
    "ensure_scheduler_job_definition",
    "converge_scheduler_job_definition",
    "claim_due_scheduler_job",
    "complete_scheduler_job_run",
    "fail_scheduler_job_run",
    "inspect_scheduler_dead_letter",
    "replay_scheduler_dead_letter",
]

DURABLE_SCHEDULER_RPC_ALLOWLIST: frozenset[str] = frozenset(
    {
        "ensure_scheduler_job_definition",
        "converge_scheduler_job_definition",
        "claim_due_scheduler_job",
        "complete_scheduler_job_run",
        "fail_scheduler_job_run",
        "inspect_scheduler_dead_letter",
        "replay_scheduler_dead_letter",
    }
)
DURABLE_SCHEDULER_MAX_RPC_RESPONSE_BYTES = 64 * 1024
DURABLE_SCHEDULER_DETERMINISTIC_REJECTION_STATUSES: frozenset[int] = frozenset(
    {400, 401, 403, 404, 405, 406, 409, 415, 422}
)

_LOCAL_ENVIRONMENTS = frozenset({"local", "test"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_HOSTED_SUPABASE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\.supabase\.co")
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,127}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_RELEASE_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")

DecodedT = TypeVar("DecodedT")


class SupabaseDurableScheduler:
    """Service-role transport for fixed durable scheduler RPC contracts."""

    __slots__ = (
        "_release_sha",
        "_persistence_authority",
        "_base_url",
        "_headers",
        "_client",
        "_managed_client",
        "_codec",
    )
    _SEALED_RUNTIME_FIELDS = frozenset(
        {
            "_release_sha",
            "_persistence_authority",
            "_base_url",
            "_headers",
            "_client",
            "_managed_client",
            "_codec",
        }
    )

    def __setattr__(self, name: str, value: object) -> None:
        if name in self._SEALED_RUNTIME_FIELDS and hasattr(self, name):
            raise AttributeError("scheduler_adapter_runtime_identity_is_read_only")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if name in self._SEALED_RUNTIME_FIELDS:
            raise AttributeError("scheduler_adapter_runtime_identity_is_read_only")
        object.__delattr__(self, name)

    def __init__(
        self,
        settings: Settings,
        *,
        release_sha: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not settings.supabase_url or settings.supabase_secret_key is None:
            raise SchedulerInvariantError("scheduler_adapter_credentials_are_missing")
        origin, authority = _validated_scheduler_origin(
            settings.supabase_url,
            environment=settings.env,
        )
        resolved_release_sha = release_sha or worker_release_metadata().get("release_sha")
        _require_release_sha(resolved_release_sha, "adapter_release_sha")
        secret = settings.supabase_secret_key.get_secret_value()
        self._release_sha = cast(str, resolved_release_sha)
        self._persistence_authority: PersistenceAuthority = authority
        self._base_url = origin + "/rest/v1/rpc"
        self._headers = supabase_api_headers(secret) | {
            "accept-profile": "worker_api",
            "accept-encoding": "identity",
            "content-profile": "worker_api",
            "content-type": "application/json",
        }
        if client is None:
            managed_client = httpx.AsyncClient(
                timeout=10.0,
                headers=self._headers,
                trust_env=False,
            )
            self._client = managed_client
            self._managed_client: httpx.AsyncClient | None = managed_client
        else:
            self._client = client
            self._managed_client = None
        self._codec = DurableSchedulerWireCodec()

    @property
    def release_sha(self) -> str:
        return self._release_sha

    @property
    def persistence_authority(self) -> PersistenceAuthority:
        return self._persistence_authority

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def transport_is_managed(self) -> bool:
        return self._managed_client is not None and self._client is self._managed_client

    async def close(self) -> None:
        managed_client = self._managed_client
        if managed_client is not None:
            await managed_client.aclose()

    async def ensure_job_definition(
        self,
        definition: ScheduledJobDefinitionV1,
        *,
        outer_lease: WorkerLease,
    ) -> ScheduledJobDefinitionReceiptV1:
        canonical_definition = canonical_scheduler_definition(definition)
        _require_safe_definition_budget(canonical_definition)
        lease = canonical_scheduler_outer_lease(outer_lease)
        receipt = await self._call_and_decode(
            "ensure_scheduler_job_definition",
            self._codec.ensure_payload(
                canonical_definition,
                outer_lease=lease,
                release_sha=self.release_sha,
            ),
            self._codec.decode_definition_receipt,
        )
        if (
            receipt.account_id != lease.account_id
            or receipt.job_key != canonical_definition.job_key
            or receipt.definition_sha256 != canonical_definition.definition_sha256
        ):
            _invalid_success_response()
        _validate_receipt_not_before_outer_acquisition(receipt.observed_at, lease)
        return receipt

    async def converge_job_definition(
        self,
        definition: ScheduledJobDefinitionV1,
        *,
        outer_lease: WorkerLease,
    ) -> SchedulerDefinitionConvergenceReceiptV1:
        canonical_definition = canonical_scheduler_definition(definition)
        _require_safe_definition_budget(canonical_definition)
        lease = canonical_scheduler_outer_lease(outer_lease)
        receipt = await self._call_and_decode(
            "converge_scheduler_job_definition",
            self._codec.convergence_payload(
                canonical_definition,
                outer_lease=lease,
                release_sha=self.release_sha,
            ),
            self._codec.decode_convergence,
        )
        _validate_receipt_not_before_outer_acquisition(receipt.observed_at, lease)
        if (
            receipt.definition.account_id != lease.account_id
            or receipt.definition.job_key != canonical_definition.job_key
            or (
                receipt.status == "converged"
                and receipt.definition.definition != canonical_definition
            )
        ):
            _invalid_success_response()
        if receipt.claim is not None:
            _validate_claim_actor(
                receipt.claim,
                lease,
                self.release_sha,
                invalid_success=True,
            )
        return receipt

    async def claim_due_job(
        self,
        *,
        outer_lease: WorkerLease,
    ) -> ScheduledJobClaimReceiptV1:
        lease = canonical_scheduler_outer_lease(outer_lease)
        receipt = await self._call_and_decode(
            "claim_due_scheduler_job",
            self._codec.actor_payload(lease, self.release_sha),
            self._codec.decode_claim,
        )
        _validate_receipt_not_before_outer_acquisition(receipt.observed_at, lease)
        claim = receipt.claim
        if claim is not None:
            if (
                not claim.definition.enabled
                or not scheduler_definition_budget_is_safe(claim.definition)
            ):
                _invalid_success_response()
            _validate_claim_actor(
                claim,
                lease,
                self.release_sha,
                invalid_success=True,
            )
        return receipt

    async def complete_job_run(
        self,
        claim: ScheduledJobClaimV1,
        *,
        outer_lease: WorkerLease,
        result_sha256: str,
    ) -> ScheduledJobCompletionReceiptV1:
        canonical_claim = canonical_scheduler_claim(claim)
        lease = canonical_scheduler_outer_lease(outer_lease)
        _require_sha256(result_sha256, "result_sha256")
        _validate_claim_actor(canonical_claim, lease, self.release_sha)
        receipt = await self._call_and_decode(
            "complete_scheduler_job_run",
            self._codec.complete_payload(
                canonical_claim,
                outer_lease=lease,
                release_sha=self.release_sha,
                result_sha256=result_sha256,
            ),
            self._codec.decode_completion,
        )
        _validate_settlement_observation(receipt.observed_at, canonical_claim, lease)
        _validate_completion_binding(receipt, canonical_claim, result_sha256)
        return receipt

    async def fail_job_run(
        self,
        claim: ScheduledJobClaimV1,
        *,
        outer_lease: WorkerLease,
        failure_reason_code: str,
        failure_sha256: str,
        retryable: bool,
    ) -> ScheduledJobFailureReceiptV1:
        canonical_claim = canonical_scheduler_claim(claim)
        lease = canonical_scheduler_outer_lease(outer_lease)
        _require_reason(failure_reason_code, "failure_reason_code")
        _require_sha256(failure_sha256, "failure_sha256")
        if type(retryable) is not bool:
            raise SchedulerInvariantError("scheduler_retryable_flag_is_invalid")
        if (
            retryable
            and failure_reason_code not in SCHEDULER_RETRYABLE_REASONS[canonical_claim.job_key]
        ):
            raise SchedulerInvariantError("scheduler_retry_classification_is_not_allowed")
        _validate_claim_actor(canonical_claim, lease, self.release_sha)
        receipt = await self._call_and_decode(
            "fail_scheduler_job_run",
            self._codec.fail_payload(
                canonical_claim,
                outer_lease=lease,
                release_sha=self.release_sha,
                failure_reason_code=failure_reason_code,
                failure_sha256=failure_sha256,
                retryable=retryable,
            ),
            self._codec.decode_failure,
        )
        _validate_settlement_observation(receipt.observed_at, canonical_claim, lease)
        _validate_failure_binding(
            receipt,
            canonical_claim,
            failure_reason_code,
            failure_sha256,
            retryable=retryable,
        )
        return receipt

    async def inspect_dead_letter(
        self,
        source_run_id: str,
        *,
        outer_lease: WorkerLease,
    ) -> SchedulerDeadLetterInspectionReceiptV1:
        _require_uuid_text(source_run_id, "source_run_id")
        lease = canonical_scheduler_outer_lease(outer_lease)
        receipt = await self._call_and_decode(
            "inspect_scheduler_dead_letter",
            self._codec.inspect_payload(
                source_run_id,
                outer_lease=lease,
                release_sha=self.release_sha,
            ),
            self._codec.decode_inspection,
        )
        _validate_receipt_not_before_outer_acquisition(receipt.observed_at, lease)
        assessment = receipt.assessment
        if assessment is not None:
            dead_letter = assessment.dead_letter
            if (
                dead_letter.account_id != lease.account_id
                or dead_letter.source_run_id != source_run_id
                or not scheduler_replay_budget_is_safe(dead_letter)
            ):
                _invalid_success_response()
        return receipt

    async def replay_dead_letter(
        self,
        assessment: SchedulerReplayAssessmentV1,
        *,
        replay_request_id: str,
        confirmed_reason_code: str,
        explicit_confirmation: bool,
        outer_lease: WorkerLease,
    ) -> SchedulerReplayReceiptV1:
        canonical_assessment = canonical_scheduler_replay_assessment(assessment)
        if not scheduler_replay_budget_is_safe(canonical_assessment.dead_letter):
            raise SchedulerInvariantError("scheduler_replay_budget_is_unsafe")
        _require_uuid_text(replay_request_id, "replay_request_id")
        _require_reason(confirmed_reason_code, "confirmed_reason_code")
        if explicit_confirmation is not True:
            raise SchedulerInvariantError("scheduler_replay_requires_confirmation")
        if not canonical_assessment.eligible:
            raise SchedulerInvariantError("scheduler_dead_letter_is_not_replayable")
        dead_letter = canonical_assessment.dead_letter
        if confirmed_reason_code != dead_letter.failure_reason_code:
            raise SchedulerInvariantError("scheduler_replay_reason_binding_mismatch")
        lease = canonical_scheduler_outer_lease(outer_lease)
        if dead_letter.account_id != lease.account_id:
            raise SchedulerInvariantError("scheduler_replay_account_binding_mismatch")
        receipt = await self._call_and_decode(
            "replay_scheduler_dead_letter",
            self._codec.replay_payload(
                canonical_assessment,
                replay_request_id=replay_request_id,
                confirmed_reason_code=confirmed_reason_code,
                outer_lease=lease,
                release_sha=self.release_sha,
            ),
            self._codec.decode_replay,
        )
        _validate_receipt_not_before_outer_acquisition(receipt.observed_at, lease)
        _validate_replay_binding(receipt, canonical_assessment, replay_request_id)
        return receipt

    async def _call_and_decode(
        self,
        rpc: DurableSchedulerRpc,
        payload: JsonObject,
        decoder: Callable[[object], DecodedT],
    ) -> DecodedT:
        raw = await self._rpc(rpc, payload)
        try:
            return decoder(raw)
        except SchedulerInvariantError:
            _invalid_success_response()

    async def _rpc(self, rpc: DurableSchedulerRpc, payload: JsonObject) -> object:
        if rpc not in DURABLE_SCHEDULER_RPC_ALLOWLIST:
            raise SchedulerInvariantError("scheduler_rpc_is_not_allowed")
        try:
            async with asyncio.timeout(DURABLE_SCHEDULER_TOTAL_RPC_TIMEOUT_SECONDS):
                async with self._client.stream(
                    "POST",
                    f"{self._base_url}/{rpc}",
                    json=payload,
                    headers=self._headers,
                    follow_redirects=False,
                ) as response:
                    if (
                        response.status_code
                        in DURABLE_SCHEDULER_DETERMINISTIC_REJECTION_STATUSES
                    ):
                        raise SchedulerTransitionRejectedError() from None
                    if response.status_code != 200:
                        raise SchedulerMutationOutcomeUnknownError() from None
                    try:
                        return await bounded_json_response(
                            response,
                            max_bytes=DURABLE_SCHEDULER_MAX_RPC_RESPONSE_BYTES,
                        )
                    except (BoundedJsonError, RuntimeError, ValueError):
                        raise SchedulerMutationOutcomeUnknownError() from None
        except (
            SchedulerMutationOutcomeUnknownError,
            SchedulerTransitionRejectedError,
        ):
            raise
        except Exception:
            raise SchedulerMutationOutcomeUnknownError() from None


def _validated_scheduler_origin(
    origin: str,
    *,
    environment: str,
) -> tuple[str, PersistenceAuthority]:
    try:
        authority = persistence_authority_fingerprint(
            namespace="supabase-worker-api",
            origin=origin,
            profile="worker_api",
        )
        parts = urlsplit(origin)
        hostname = parts.hostname
        port = parts.port
    except (AttributeError, TypeError, ValueError):
        raise SchedulerInvariantError("scheduler_adapter_origin_is_not_allowed") from None
    if hostname is None:
        raise SchedulerInvariantError("scheduler_adapter_origin_is_not_allowed")
    hostname = hostname.lower()
    if (
        parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise SchedulerInvariantError("scheduler_adapter_origin_is_not_allowed")
    if (
        parts.scheme.lower() == "https"
        and _HOSTED_SUPABASE_RE.fullmatch(hostname) is not None
        and port in {None, 443}
    ):
        port_suffix = ":443" if port == 443 else ""
        return f"https://{hostname}{port_suffix}", authority
    if (
        environment in _LOCAL_ENVIRONMENTS
        and parts.scheme.lower() == "http"
        and hostname in _LOOPBACK_HOSTS
        and port is not None
    ):
        rendered_host = f"[{hostname}]" if ":" in hostname else hostname
        return f"http://{rendered_host}:{port}", authority
    raise SchedulerInvariantError("scheduler_adapter_origin_is_not_allowed")


def _validate_claim_actor(
    claim: ScheduledJobClaimV1,
    outer_lease: WorkerLease,
    release_sha: str,
    *,
    invalid_success: bool = False,
) -> None:
    # The application layer validates observed_at and the inner expiry against
    # a post-response refresh of this same outer lease generation.
    if (
        claim.observed_at < outer_lease.acquired_at
        or claim.run.account_id != outer_lease.account_id
        or claim.lease.account_id != outer_lease.account_id
        or claim.lease.holder_id != outer_lease.holder_id
        or claim.lease.outer_fencing_token != outer_lease.fencing_token
        or claim.lease.release_sha != release_sha
        or claim.lease.leased_at != claim.observed_at
        or claim.run.updated_at != claim.observed_at
    ):
        if invalid_success:
            _invalid_success_response()
        raise SchedulerInvariantError("scheduler_claim_actor_binding_mismatch")


def _require_safe_definition_budget(definition: ScheduledJobDefinitionV1) -> None:
    if not scheduler_definition_budget_is_safe(definition):
        raise SchedulerInvariantError("scheduler_definition_budget_is_unsafe")


def _validate_completion_binding(
    receipt: ScheduledJobCompletionReceiptV1,
    claim: ScheduledJobClaimV1,
    result_sha256: str,
) -> None:
    if (
        receipt.run_id != claim.run.run_id
        or receipt.attempt_count != claim.run.attempt_count
        or receipt.result_sha256 != result_sha256
        or receipt.run_revision != claim.run.revision + 1
    ):
        _invalid_success_response()


def _validate_failure_binding(
    receipt: ScheduledJobFailureReceiptV1,
    claim: ScheduledJobClaimV1,
    failure_reason_code: str,
    failure_sha256: str,
    *,
    retryable: bool,
) -> None:
    expected_state = (
        "retry_wait"
        if retryable and claim.run.attempt_count < claim.definition.max_attempts
        else "dead_letter"
    )
    retry_schedule_is_valid = (
        expected_state == "dead_letter" and receipt.next_attempt_at is None
    )
    if expected_state == "retry_wait" and receipt.next_attempt_at is not None:
        try:
            transition_at = receipt.next_attempt_at - scheduler_retry_delay(
                claim.definition,
                claim.run.attempt_count,
            )
        except (OverflowError, SchedulerInvariantError):
            transition_at = None
        retry_schedule_is_valid = (
            transition_at is not None
            and claim.observed_at <= transition_at <= receipt.observed_at
        )
    if (
        receipt.run_id != claim.run.run_id
        or receipt.attempt_count != claim.run.attempt_count
        or receipt.failure_reason_code != failure_reason_code
        or receipt.result_sha256 != failure_sha256
        or receipt.run_revision != claim.run.revision + 1
        or receipt.state != expected_state
        or not retry_schedule_is_valid
        or (
            retryable
            and failure_reason_code not in SCHEDULER_RETRYABLE_REASONS[claim.job_key]
        )
    ):
        _invalid_success_response()


def _validate_replay_binding(
    receipt: SchedulerReplayReceiptV1,
    assessment: SchedulerReplayAssessmentV1,
    replay_request_id: str,
) -> None:
    dead_letter = assessment.dead_letter
    if (
        receipt.source_run_id != dead_letter.source_run_id
        or receipt.replay_request_id != replay_request_id
        or receipt.job_key != dead_letter.job_key
        or receipt.definition_sha256 != dead_letter.definition_sha256
        or receipt.source_revision != dead_letter.source_revision
        or receipt.failure_reason_code != dead_letter.failure_reason_code
        or receipt.replay_generation != dead_letter.replay_generation + 1
    ):
        _invalid_success_response()


def _validate_receipt_not_before_outer_acquisition(
    observed_at: object,
    outer_lease: WorkerLease,
) -> None:
    # A same-generation renewal may extend expires_at while this RPC is in
    # flight. Application use cases re-read that latest lease before using the
    # receipt and enforce its upper bound there.
    if (
        not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
        or observed_at < outer_lease.acquired_at
    ):
        _invalid_success_response()


def _validate_settlement_observation(
    observed_at: object,
    claim: ScheduledJobClaimV1,
    outer_lease: WorkerLease,
) -> None:
    _validate_receipt_not_before_outer_acquisition(observed_at, outer_lease)
    observed = cast(datetime, observed_at)
    # SQL authorizes every invocation against the current outer lease before
    # returning an exact terminal tuple. On an idempotent recovery call the
    # inner lease may already be expired even though the original transition
    # was committed while it was valid.
    if observed < claim.observed_at:
        _invalid_success_response()


def _invalid_success_response() -> Never:
    raise SchedulerMutationOutcomeUnknownError("scheduler_success_response_is_invalid") from None


def _require_uuid_text(value: object, field_name: str) -> None:
    if type(value) is not str:
        raise SchedulerInvariantError(f"scheduler_{field_name}_is_invalid")
    try:
        parsed = UUID(value)
    except ValueError:
        raise SchedulerInvariantError(f"scheduler_{field_name}_is_invalid") from None
    if str(parsed) != value:
        raise SchedulerInvariantError(f"scheduler_{field_name}_is_invalid")


def _require_sha256(value: object, field_name: str) -> None:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise SchedulerInvariantError(f"scheduler_{field_name}_is_invalid")


def _require_release_sha(value: object, field_name: str) -> None:
    if type(value) is not str or _RELEASE_SHA_RE.fullmatch(value) is None:
        raise SchedulerInvariantError(f"scheduler_{field_name}_is_invalid")


def _require_reason(value: object, field_name: str) -> None:
    if type(value) is not str or _REASON_RE.fullmatch(value) is None:
        raise SchedulerInvariantError(f"scheduler_{field_name}_is_invalid")
