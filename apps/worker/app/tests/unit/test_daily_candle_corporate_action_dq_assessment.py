from __future__ import annotations

import ast
import hashlib
import json
import traceback
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from app.application.services.daily_candle_corporate_action_dq_assessment import (
    PIT_DAILY_CANDLE_CORPORATE_ACTION_DQ_LIMITATIONS,
    PIT_DAILY_CANDLE_CORPORATE_ACTION_DQ_REJECTION_REASONS,
    PIT_DAILY_CANDLE_CORPORATE_ACTION_DQ_STATUS,
    PIT_DAILY_CANDLE_CORPORATE_ACTION_STATUS,
    PIT_DAILY_CANDLE_LOCAL_DQ_CHECK_IDS,
    DailyCandleCorporateActionDqAssessmentError,
    DailyCandleCorporateActionDqManifestV1,
    build_daily_candle_corporate_action_dq_assessment,
    require_corporate_action_dq_certification,
    validate_daily_candle_corporate_action_dq_assessment,
)
from app.domain.common.json import JsonObject, JsonValue
from app.tests.unit.test_daily_candle_research_certification import (
    SNAPSHOT_ISSUED_AT,
    _mixed_open_closed_source_pair,
    _source_pair,
)


def test_assessment_approves_only_exact_local_checks_and_blocks_full_dq() -> None:
    research, calendar = _source_pair()

    result = build_daily_candle_corporate_action_dq_assessment(
        research,
        calendar,
    )
    payload = result.to_payload()
    manifest_sha256 = cast(str, payload.pop("assessment_manifest_sha256"))
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    assert result.assessment_status == PIT_DAILY_CANDLE_CORPORATE_ACTION_DQ_STATUS
    assert result.dq_check_ids == PIT_DAILY_CANDLE_LOCAL_DQ_CHECK_IDS
    assert result.dq_check_count == len(PIT_DAILY_CANDLE_LOCAL_DQ_CHECK_IDS)
    assert result.local_retained_dq_checks_passed is True
    assert result.corporate_action_status == PIT_DAILY_CANDLE_CORPORATE_ACTION_STATUS
    assert result.corporate_action_evidence_sha256 is None
    assert result.corporate_action_coverage_verified is False
    assert result.corporate_action_adjustment_semantics_verified is False
    assert result.full_data_quality_certified is False
    assert result.dataset_registration_allowed is False
    assert result.feature_use_allowed is False
    assert result.backtest_use_allowed is False
    assert result.strategy_promotion_allowed is False
    assert result.order_use_allowed is False
    assert result.rejection_reasons == (PIT_DAILY_CANDLE_CORPORATE_ACTION_DQ_REJECTION_REASONS)
    assert result.limitations == PIT_DAILY_CANDLE_CORPORATE_ACTION_DQ_LIMITATIONS
    assert result.research_slice_spec_sha256 == research.slice_spec_sha256
    assert result.research_data_manifest_sha256 == research.data_manifest_sha256
    assert result.calendar_coverage_spec_sha256 == calendar.coverage_spec_sha256
    assert result.calendar_data_manifest_sha256 == calendar.data_manifest_sha256
    assert (
        result.dq_policy_sha256
        == "90b3d3a292c75ad4147345215ffbec064fa6a08e1e60e5fab34a9561995f28fb"
    )
    assert (
        result.dq_results_sha256
        == "cc032dfdb280ef0e7ef472936df925c74dc39293194b9721329d4dd673fb3fd2"
    )
    expected_dq_results: JsonObject = {
        "schema_version": result.dq_policy_version,
        "policy_sha256": result.dq_policy_sha256,
        "source_binding": {
            "research_certification_manifest_sha256": (
                result.research_certification_manifest_sha256
            ),
            "research_slice_spec_sha256": result.research_slice_spec_sha256,
            "research_data_manifest_sha256": result.research_data_manifest_sha256,
            "calendar_coverage_spec_sha256": result.calendar_coverage_spec_sha256,
            "calendar_data_manifest_sha256": result.calendar_data_manifest_sha256,
            "cross_source_shared_calendar_lineage_sha256": (
                result.cross_source_shared_calendar_lineage_sha256
            ),
        },
        "results": [
            {"check_id": check_id, "status": "pass"}
            for check_id in PIT_DAILY_CANDLE_LOCAL_DQ_CHECK_IDS
        ],
    }
    assert result.dq_results_sha256 == _payload_sha256(expected_dq_results)
    assert manifest_sha256 == result.assessment_manifest_sha256
    assert manifest_sha256 == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert manifest_sha256 == "dbb2468180475211305d6c72194b61a3c4e4d6dbf3b2df6c45221c3968ddabf6"

    serialized = json.dumps(result.to_payload(), sort_keys=True)
    assert "credential" not in serialized
    assert "secret" not in serialized
    assert "C:\\" not in serialized


@pytest.mark.parametrize("adjusted", [False, True])
def test_adjusted_request_flag_never_becomes_corporate_action_evidence(
    adjusted: bool,
) -> None:
    research, calendar = _source_pair(adjusted=adjusted)

    result = build_daily_candle_corporate_action_dq_assessment(
        research,
        calendar,
    )

    assert result.adjusted is adjusted
    assert result.local_retained_dq_checks_passed is True
    assert result.corporate_action_evidence_sha256 is None
    assert result.corporate_action_coverage_verified is False
    assert result.full_data_quality_certified is False


def test_assessment_binds_mixed_open_and_closed_calendar_days() -> None:
    research, calendar = _mixed_open_closed_source_pair()

    result = build_daily_candle_corporate_action_dq_assessment(
        research,
        calendar,
    )

    assert result.selected_session_count == 2
    assert result.calendar_day_count == 3
    assert result.local_retained_dq_checks_passed is True
    assert result.full_data_quality_certified is False


def test_extra_raw_candidates_do_not_turn_into_failed_dq_checks() -> None:
    research, calendar = _source_pair(research_candidate_count=3)

    result = build_daily_candle_corporate_action_dq_assessment(
        research,
        calendar,
    )

    assert research.source_candidate_count == 3
    assert research.selected_session_count == 1
    assert result.dq_check_count == len(PIT_DAILY_CANDLE_LOCAL_DQ_CHECK_IDS)
    assert result.local_retained_dq_checks_passed is True


def test_acquisition_metadata_does_not_change_logical_dq_manifest() -> None:
    first_research, first_calendar = _source_pair()
    second_research, second_calendar = _source_pair(
        research_page_size=25,
        calendar_page_size=25,
        research_token="101:202:303",
        calendar_token="404:505:606",
        snapshot_issued_at=SNAPSHOT_ISSUED_AT + timedelta(hours=1),
    )

    first = build_daily_candle_corporate_action_dq_assessment(
        first_research,
        first_calendar,
    )
    second = build_daily_candle_corporate_action_dq_assessment(
        second_research,
        second_calendar,
    )

    assert first.research_slice_spec_sha256 == second.research_slice_spec_sha256
    assert first.research_data_manifest_sha256 == second.research_data_manifest_sha256
    assert first.calendar_coverage_spec_sha256 == second.calendar_coverage_spec_sha256
    assert first.calendar_data_manifest_sha256 == second.calendar_data_manifest_sha256
    assert first.assessment_manifest_sha256 == second.assessment_manifest_sha256


def test_source_manifest_change_changes_dq_result_and_manifest_hashes() -> None:
    first_research, first_calendar = _source_pair()
    second_research, second_calendar = _source_pair(research_raw_manifest="e" * 64)

    first = build_daily_candle_corporate_action_dq_assessment(
        first_research,
        first_calendar,
    )
    second = build_daily_candle_corporate_action_dq_assessment(
        second_research,
        second_calendar,
    )

    assert first.research_slice_spec_sha256 == second.research_slice_spec_sha256
    assert first.research_data_manifest_sha256 != second.research_data_manifest_sha256
    assert first.dq_results_sha256 != second.dq_results_sha256
    assert first.assessment_manifest_sha256 != second.assessment_manifest_sha256


def test_require_gate_rejects_canonical_blocked_assessment() -> None:
    research, calendar = _source_pair()
    assessment = build_daily_candle_corporate_action_dq_assessment(
        research,
        calendar,
    )

    with pytest.raises(
        DailyCandleCorporateActionDqAssessmentError,
        match="daily_candle_corporate_action_dq_assessment_not_certified",
    ) as exc_info:
        require_corporate_action_dq_certification(assessment)

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_result_is_detached_and_cannot_be_constructed_directly() -> None:
    research, calendar = _source_pair()
    result = build_daily_candle_corporate_action_dq_assessment(
        research,
        calendar,
    )
    original_payload = result.to_payload()

    object.__setattr__(research, "data_manifest_sha256", "e" * 64)
    object.__setattr__(calendar, "data_manifest_sha256", "f" * 64)

    assert result.to_payload() == original_payload
    constructor = cast(Any, DailyCandleCorporateActionDqManifestV1)
    with pytest.raises(
        DailyCandleCorporateActionDqAssessmentError,
        match="daily_candle_corporate_action_dq_assessment_requires_gate",
    ):
        constructor()


@pytest.mark.parametrize(
    ("field_name", "payload_path", "forged_field_value", "forged_payload_value"),
    [
        (
            "assessment_status",
            ("assessment_status",),
            "certified",
            "certified",
        ),
        (
            "dq_check_count",
            ("local_data_quality", "check_count"),
            9,
            9,
        ),
        (
            "dq_check_count",
            ("local_data_quality", "check_count"),
            True,
            True,
        ),
        (
            "dq_check_count",
            ("local_data_quality", "check_count"),
            10.0,
            10.0,
        ),
        (
            "dq_check_ids",
            ("local_data_quality", "check_ids"),
            PIT_DAILY_CANDLE_LOCAL_DQ_CHECK_IDS[:-1],
            list(PIT_DAILY_CANDLE_LOCAL_DQ_CHECK_IDS[:-1]),
        ),
        (
            "dq_policy_sha256",
            ("local_data_quality", "policy_sha256"),
            "E" * 64,
            "E" * 64,
        ),
        (
            "dq_results_sha256",
            ("local_data_quality", "results_sha256"),
            "e" * 64,
            "e" * 64,
        ),
        (
            "research_data_manifest_sha256",
            ("source_binding", "research_data_manifest_sha256"),
            "e" * 64,
            "e" * 64,
        ),
        (
            "cross_source_shared_calendar_lineage_sha256",
            ("source_binding", "cross_source_shared_calendar_lineage_sha256"),
            "e" * 64,
            "e" * 64,
        ),
        (
            "selected_as_of",
            ("scope", "as_of"),
            datetime(2026, 3, 25, 3, 0),
            "2026-03-25T03:00:00",
        ),
        (
            "selected_session_count",
            ("scope", "selected_session_count"),
            1.0,
            1.0,
        ),
        (
            "corporate_action_evidence_sha256",
            ("corporate_action", "evidence_sha256"),
            "e" * 64,
            "e" * 64,
        ),
        (
            "corporate_action_coverage_verified",
            ("corporate_action", "coverage_verified"),
            True,
            True,
        ),
        (
            "corporate_action_adjustment_semantics_verified",
            ("corporate_action", "adjustment_semantics_verified"),
            True,
            True,
        ),
        (
            "local_retained_dq_checks_passed",
            ("local_data_quality", "checks_passed"),
            False,
            False,
        ),
        (
            "local_retained_dq_checks_passed",
            ("local_data_quality", "checks_passed"),
            1,
            1,
        ),
        (
            "full_data_quality_certified",
            ("certification", "full_data_quality_certified"),
            True,
            True,
        ),
        (
            "full_data_quality_certified",
            ("certification", "full_data_quality_certified"),
            0,
            0,
        ),
        (
            "dataset_registration_allowed",
            ("certification", "dataset_registration_allowed"),
            True,
            True,
        ),
        (
            "strategy_promotion_allowed",
            ("certification", "strategy_promotion_allowed"),
            True,
            True,
        ),
        (
            "rejection_reasons",
            ("rejection_reasons",),
            ("forged",),
            ["forged"],
        ),
        (
            "limitations",
            ("limitations",),
            ("forged",),
            ["forged"],
        ),
    ],
)
def test_recomputed_digest_does_not_accept_forged_manifest_fields(
    field_name: str,
    payload_path: tuple[str, ...],
    forged_field_value: object,
    forged_payload_value: JsonValue,
) -> None:
    research, calendar = _source_pair()
    result = build_daily_candle_corporate_action_dq_assessment(
        research,
        calendar,
    )
    forged_payload = deepcopy(result.to_payload())
    forged_payload.pop("assessment_manifest_sha256")
    _set_payload_path(forged_payload, payload_path, forged_payload_value)
    forged_sha256 = _payload_sha256(forged_payload)

    object.__setattr__(result, field_name, forged_field_value)
    object.__setattr__(result, "assessment_manifest_sha256", forged_sha256)

    with pytest.raises(
        DailyCandleCorporateActionDqAssessmentError,
        match="daily_candle_corporate_action_dq_assessment_manifest_invalid",
    ):
        result.to_payload()


@pytest.mark.parametrize("entrypoint", ["validate", "serialize", "require"])
def test_exact_type_confusion_is_rejected_by_every_public_entrypoint(
    entrypoint: str,
) -> None:
    research, calendar = _source_pair()
    result = build_daily_candle_corporate_action_dq_assessment(
        research,
        calendar,
    )
    forged_payload = deepcopy(result.to_payload())
    forged_payload.pop("assessment_manifest_sha256")
    _set_payload_path(forged_payload, ("local_data_quality", "checks_passed"), 1)

    object.__setattr__(result, "local_retained_dq_checks_passed", 1)
    object.__setattr__(result, "assessment_manifest_sha256", _payload_sha256(forged_payload))

    with pytest.raises(
        DailyCandleCorporateActionDqAssessmentError,
        match="daily_candle_corporate_action_dq_assessment_manifest_invalid",
    ):
        if entrypoint == "validate":
            validate_daily_candle_corporate_action_dq_assessment(result)
        elif entrypoint == "serialize":
            result.to_payload()
        else:
            require_corporate_action_dq_certification(result)


def test_forged_retained_source_is_rejected_even_with_original_manifest() -> None:
    research, calendar = _source_pair()
    result = build_daily_candle_corporate_action_dq_assessment(
        research,
        calendar,
    )

    object.__setattr__(result._research_slice, "data_manifest_sha256", "e" * 64)

    with pytest.raises(
        DailyCandleCorporateActionDqAssessmentError,
        match="daily_candle_corporate_action_dq_assessment_manifest_invalid",
    ):
        result.to_payload()


def test_invalid_source_error_does_not_retain_attacker_text() -> None:
    secret = "credential=must-not-leak"

    with pytest.raises(
        DailyCandleCorporateActionDqAssessmentError,
        match="daily_candle_corporate_action_dq_assessment_source_invalid",
    ) as exc_info:
        build_daily_candle_corporate_action_dq_assessment(
            cast(Any, _ExplodingSource(secret)),
            cast(Any, object()),
        )

    formatted = "".join(traceback.format_exception(exc_info.value))
    assert secret not in str(exc_info.value)
    assert secret not in formatted
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_negative_only_assessment_is_not_imported_by_runtime_code() -> None:
    app_root = Path(__file__).resolve().parents[2]
    violations: list[str] = []

    for source_path in sorted(app_root.rglob("*.py")):
        relative_path = source_path.relative_to(app_root)
        if (
            "tests" in relative_path.parts
            or "tools" in relative_path.parts
            or source_path.name == "daily_candle_corporate_action_dq_assessment.py"
        ):
            continue
        if _imports_dq_assessment(
            source_path.read_text(encoding="utf-8"),
            filename=str(source_path),
        ):
            violations.append(relative_path.as_posix())

    assert violations == []


@pytest.mark.parametrize(
    "source",
    [
        "import app.application.services.daily_candle_corporate_action_dq_assessment",
        (
            "from app.application.services.daily_candle_corporate_action_dq_assessment "
            "import require_corporate_action_dq_certification"
        ),
        ("from app.application.services import daily_candle_corporate_action_dq_assessment"),
        "from . import daily_candle_corporate_action_dq_assessment",
        (
            "import importlib\n"
            "importlib.import_module("
            "'app.application.services.daily_candle_corporate_action_dq_assessment')"
        ),
        (
            "from importlib import import_module\n"
            "import_module('.daily_candle_corporate_action_dq_assessment', "
            "package=__package__)"
        ),
        (
            "from importlib import import_module as im\n"
            "im('app.application.services.' "
            "+ 'daily_candle_corporate_action_dq_assessment')"
        ),
        (
            "import importlib as loader\n"
            "module_name = 'app.application.services.' "
            "+ 'daily_candle_corporate_action_dq_assessment'\n"
            "loader.import_module(module_name)"
        ),
        "__import__('app.application.services.daily_candle_corporate_action_dq_assessment')",
    ],
)
def test_no_wiring_guard_recognizes_supported_import_forms(source: str) -> None:
    assert _imports_dq_assessment(source, filename="synthetic.py") is True


class _ExplodingSource:
    def __init__(self, secret: str) -> None:
        self.secret = secret

    @property
    def provider(self) -> str:
        raise RuntimeError(self.secret)


def _set_payload_path(
    payload: JsonObject,
    path: tuple[str, ...],
    value: JsonValue,
) -> None:
    cursor = payload
    for segment in path[:-1]:
        nested = cursor[segment]
        assert isinstance(nested, dict)
        cursor = nested
    cursor[path[-1]] = value


def _payload_sha256(payload: JsonObject) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _imports_dq_assessment(source: str, *, filename: str) -> bool:
    target_module = "app.application.services.daily_candle_corporate_action_dq_assessment"
    target_leaf = "daily_candle_corporate_action_dq_assessment"

    tree = ast.parse(source, filename=filename)
    importlib_aliases = {"importlib"}
    import_module_aliases = {"import_module"}
    static_strings: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    import_module_aliases.add(alias.asname or alias.name)

    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
    for _ in range(len(assignments) + 1):
        changed = False
        for assignment in assignments:
            value = _static_string(assignment.value, static_strings)
            if value is None:
                continue
            for target in assignment.targets:
                if isinstance(target, ast.Name) and static_strings.get(target.id) != value:
                    static_strings[target.id] = value
                    changed = True
        if not changed:
            break

    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                modules.append(node.module)
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Call) and node.args:
            first_argument = node.args[0]
            is_import_call = (
                isinstance(node.func, ast.Name)
                and node.func.id in {"__import__", *import_module_aliases}
            ) or (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in importlib_aliases
                and node.func.attr == "import_module"
            )
            imported_module = _static_string(first_argument, static_strings)
            if is_import_call and imported_module is not None:
                modules.append(imported_module)
        if any(
            module_name.lstrip(".") in {target_module, target_leaf}
            or module_name.endswith(f".{target_leaf}")
            for module_name in modules
        ):
            return True
    return False


def _static_string(node: ast.expr, names: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string(node.left, names)
        right = _static_string(node.right, names)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                return None
            parts.append(value.value)
        return "".join(parts)
    return None
