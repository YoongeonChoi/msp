import base64

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings

CURRENT_ACK_KEY_B64 = base64.b64encode(b"c" * 32).decode("ascii")
PREVIOUS_ACK_KEY_B64 = base64.b64encode(b"p" * 32).decode("ascii")


def test_mock_providers_default_to_in_memory_repository() -> None:
    settings = Settings(
        MOCK_PROVIDERS=True,
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SECRET_KEY=SecretStr("dummy-test-token"),
    )

    assert settings.use_supabase_repository() is False


def test_mock_providers_can_write_to_supabase_when_explicitly_enabled() -> None:
    settings = Settings(
        MOCK_PROVIDERS=True,
        USE_SUPABASE_REPOSITORY=True,
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SECRET_KEY=SecretStr("dummy-test-token"),
    )

    assert settings.use_supabase_repository() is True


def test_real_provider_mode_uses_supabase_when_configured() -> None:
    settings = Settings(
        MOCK_PROVIDERS=False,
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SECRET_KEY=SecretStr("dummy-test-token"),
    )

    assert settings.use_supabase_repository() is True


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LOOP_INTERVAL_SEC", 0),
        ("HEARTBEAT_INTERVAL_SEC", 3601),
        ("MAX_CONCURRENT_API_CALLS", 0),
        ("ALERT_WEBHOOK_TIMEOUT_SEC", float("nan")),
        ("ALERT_WEBHOOK_TIMEOUT_SEC", float("inf")),
        ("ALERT_DRILL_MAX_LATENCY_MS", 0),
        ("PAPER_HEALTH_DB_WARNING_BYTES", 0),
        ("OUTCOME_TRACKING_DECISION_LIMIT", 0),
        ("OUTCOME_TRACKING_PRICE_LIMIT", 0),
    ],
)
def test_runtime_settings_reject_unsafe_numeric_values(name: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({name: value})


@pytest.mark.parametrize("environment", ["live", "production", "sandbox"])
def test_execution_v2_rejects_unapproved_environments(environment: str) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"EXECUTION_V2_ENVIRONMENT": environment})


def test_contract_test_execution_is_forbidden_in_production() -> None:
    with pytest.raises(ValidationError, match="forbidden_in_production"):
        Settings.model_validate(
            _alert_webhook_settings(
                ENV="production",
                MOCK_PROVIDERS=True,
                EXECUTION_V2_ENABLED=True,
                EXECUTION_V2_ENVIRONMENT="contract_test",
            )
        )


def test_worker_api_requires_explicit_execution_v2_enablement() -> None:
    with pytest.raises(ValidationError, match="requires_execution_v2_enabled"):
        Settings(EXECUTION_V2_WORKER_API_ENABLED=True)


def test_paper_resume_input_requires_paper_worker_api_enablement() -> None:
    with pytest.raises(
        ValidationError,
        match="paper_resume_input_requires_paper_worker_api_enablement",
    ):
        Settings(EXECUTION_V2_PAPER_RESUME_INPUT_ENABLED=True)

    with pytest.raises(
        ValidationError,
        match="paper_resume_input_requires_paper_worker_api_enablement",
    ):
        Settings(
            EXECUTION_V2_ENABLED=True,
            EXECUTION_V2_WORKER_API_ENABLED=True,
            EXECUTION_V2_PAPER_RESUME_INPUT_ENABLED=True,
            EXECUTION_V2_ENVIRONMENT="contract_test",
            EXECUTION_V2_WORKER_ID="00000000-0000-4000-8000-000000000001",
        )


def test_paper_source_input_requires_paper_worker_api_enablement() -> None:
    with pytest.raises(
        ValidationError,
        match="paper_source_input_requires_paper_worker_api_enablement",
    ):
        Settings(EXECUTION_V2_PAPER_SOURCE_INPUT_ENABLED=True)

    with pytest.raises(
        ValidationError,
        match="paper_source_input_requires_paper_worker_api_enablement",
    ):
        Settings(
            EXECUTION_V2_ENABLED=True,
            EXECUTION_V2_WORKER_API_ENABLED=True,
            EXECUTION_V2_PAPER_SOURCE_INPUT_ENABLED=True,
            EXECUTION_V2_ENVIRONMENT="contract_test",
            EXECUTION_V2_WORKER_ID="00000000-0000-4000-8000-000000000001",
        )


def test_worker_api_requires_stable_uuid_worker_identity() -> None:
    with pytest.raises(ValidationError, match="worker_id_is_required"):
        Settings(
            EXECUTION_V2_ENABLED=True,
            EXECUTION_V2_WORKER_API_ENABLED=True,
        )

    settings = Settings(
        EXECUTION_V2_ENABLED=True,
        EXECUTION_V2_WORKER_API_ENABLED=True,
        EXECUTION_V2_WORKER_ID="00000000-0000-4000-8000-000000000001",
        EXECUTION_V2_ACCOUNT_ID="paper-primary",
    )
    assert settings.execution_v2_worker_id == ("00000000-0000-4000-8000-000000000001")
    assert settings.execution_v2_account_id == "paper-primary"


def test_worker_api_requires_fixed_internal_account_and_renewal_margin() -> None:
    common: dict[str, object] = {
        "EXECUTION_V2_ENABLED": True,
        "EXECUTION_V2_WORKER_API_ENABLED": True,
        "EXECUTION_V2_WORKER_ID": "00000000-0000-4000-8000-000000000001",
    }
    with pytest.raises(ValidationError, match="account_id_is_invalid"):
        Settings.model_validate(common | {"EXECUTION_V2_ACCOUNT_ID": "other-account"})
    with pytest.raises(ValidationError, match="renewal_window_is_invalid"):
        Settings.model_validate(
            common
            | {
                "EXECUTION_V2_ACCOUNT_ID": "paper-primary",
                "WORKER_LEASE_TTL_SEC": 30,
                "WORKER_LEASE_RENEW_INTERVAL_SEC": 15,
            }
        )


def test_legacy_live_default_and_write_flags_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"BOT_DEFAULT_MODE": "live"})
    with pytest.raises(ValidationError, match="live_order_write_is_quarantined"):
        Settings(LIVE_ORDER_EXECUTION_ENABLED=True)
    with pytest.raises(ValidationError, match="live_order_write_is_quarantined"):
        Settings(TOSS_ORDER_ENDPOINT_ENABLED=True)


def test_toss_credentials_must_be_explicitly_read_only() -> None:
    with pytest.raises(ValidationError, match="explicitly_read_only"):
        Settings(TOSS_CLIENT_ID=SecretStr("client-id"))
    with pytest.raises(ValidationError, match="order_capable_toss_credentials_are_forbidden"):
        Settings(TOSS_ORDER_CAPABLE_CREDENTIALS=True)

    settings = Settings(
        TOSS_CLIENT_ID=SecretStr("client-id"),
        TOSS_CREDENTIAL_SCOPE="read_only",
        TOSS_ORDER_CAPABLE_CREDENTIALS=False,
    )
    assert settings.toss_credential_scope == "read_only"
    assert settings.toss_order_capable_credentials is False


def _manual_calendar_settings(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "KR_CALENDAR_COLLECTION_ASSESSMENT_ENABLED": True,
        "KR_CALENDAR_COLLECTION_MANUAL_EXECUTION_ENABLED": True,
        "MOCK_PROVIDERS": False,
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SECRET_KEY": SecretStr("dummy-test-token"),
        "TOSS_CLIENT_ID": SecretStr("dummy-client-id"),
        "TOSS_CLIENT_SECRET": SecretStr("dummy-client-secret"),
        "TOSS_CREDENTIAL_SCOPE": "read_only",
        "TOSS_ORDER_CAPABLE_CREDENTIALS": False,
        "KR_CALENDAR_COLLECTION_HOLDER_ID": ("00000000-0000-4000-8000-000000000099"),
    }
    return values | overrides


def test_manual_calendar_collection_is_disabled_by_default() -> None:
    settings = Settings()

    assert settings.kr_calendar_collection_assessment_enabled is False
    assert settings.kr_calendar_collection_manual_execution_enabled is False
    with pytest.raises(
        ValueError,
        match="kr_calendar_collection_assessment_disabled",
    ):
        settings.require_kr_calendar_collection_assessment()
    with pytest.raises(
        ValueError,
        match="kr_calendar_collection_manual_execution_disabled",
    ):
        settings.require_kr_calendar_collection_manual_execution()


def test_calendar_collection_assessment_accepts_only_hosted_worker_read() -> None:
    settings = Settings(
        KR_CALENDAR_COLLECTION_ASSESSMENT_ENABLED=True,
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SECRET_KEY=SecretStr("dummy-test-token"),
    )

    settings.require_kr_calendar_collection_assessment()
    assert settings.kr_calendar_collection_manual_execution_enabled is False


def test_manual_calendar_collection_accepts_only_complete_read_only_runtime() -> None:
    settings = Settings.model_validate(_manual_calendar_settings())

    settings.require_kr_calendar_collection_manual_execution()
    assert settings.mock_providers is False
    assert settings.toss_credential_scope == "read_only"
    assert settings.toss_order_capable_credentials is False


@pytest.mark.parametrize(
    ("override", "safe_message"),
    [
        (
            {"MOCK_PROVIDERS": True},
            "kr_calendar_collection_manual_execution_requires_real_providers",
        ),
        (
            {"KR_CALENDAR_COLLECTION_ASSESSMENT_ENABLED": False},
            "kr_calendar_collection_assessment_disabled",
        ),
        (
            {"SUPABASE_URL": "   "},
            "kr_calendar_collection_assessment_requires_supabase_credentials",
        ),
        (
            {"SUPABASE_SECRET_KEY": SecretStr("   ")},
            "kr_calendar_collection_assessment_requires_supabase_credentials",
        ),
        (
            {"SUPABASE_URL": "http://example.supabase.co"},
            "assessment_requires_hosted_supabase_https_origin",
        ),
        (
            {"SUPABASE_URL": "https://example.invalid"},
            "assessment_requires_hosted_supabase_https_origin",
        ),
        (
            {"SUPABASE_URL": "https://user:pass@example.supabase.co"},
            "assessment_requires_hosted_supabase_https_origin",
        ),
        (
            {"SUPABASE_URL": "https://example.supabase.co/rest/v1"},
            "assessment_requires_hosted_supabase_https_origin",
        ),
        (
            {"SUPABASE_URL": "https://example.supabase.co:444"},
            "assessment_requires_hosted_supabase_https_origin",
        ),
        (
            {"SUPABASE_URL": "https://example.supabase.co?test=value"},
            "assessment_requires_hosted_supabase_https_origin",
        ),
        (
            {"SUPABASE_URL": " https://example.supabase.co"},
            "assessment_requires_hosted_supabase_https_origin",
        ),
        (
            {"TOSS_CLIENT_ID": SecretStr("   ")},
            "kr_calendar_collection_manual_execution_requires_toss_credentials",
        ),
        (
            {"TOSS_CLIENT_SECRET": SecretStr("   ")},
            "kr_calendar_collection_manual_execution_requires_toss_credentials",
        ),
        (
            {"TOSS_CREDENTIAL_SCOPE": "unknown"},
            "toss_credential_scope_must_be_explicitly_read_only",
        ),
        (
            {"TOSS_ORDER_CAPABLE_CREDENTIALS": True},
            "order_capable_toss_credentials_are_forbidden",
        ),
        (
            {"KR_CALENDAR_COLLECTION_HOLDER_ID": None},
            "kr_calendar_collection_manual_execution_requires_uuid4_holder_id",
        ),
        (
            {"KR_CALENDAR_COLLECTION_HOLDER_ID": ("00000000-0000-1000-8000-000000000099")},
            "kr_calendar_collection_manual_execution_requires_uuid4_holder_id",
        ),
    ],
)
def test_manual_calendar_collection_rejects_unsafe_runtime_configuration(
    override: dict[str, object],
    safe_message: str,
) -> None:
    with pytest.raises(ValidationError, match=safe_message):
        Settings.model_validate(_manual_calendar_settings(**override))


def test_manual_calendar_collection_runtime_rechecks_mutated_settings() -> None:
    settings = Settings.model_validate(_manual_calendar_settings())
    settings.mock_providers = True

    with pytest.raises(
        ValueError,
        match="kr_calendar_collection_manual_execution_requires_real_providers",
    ):
        settings.require_kr_calendar_collection_manual_execution()


def test_alert_webhook_exact_empty_optional_values_remain_disabled() -> None:
    settings = Settings.model_validate(
        {
            "ALERT_WEBHOOK_URL": "",
            "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID": "",
            "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64": "",
            "ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID": "",
            "ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64": "",
        }
    )

    assert settings.alert_webhook_url is None
    assert settings.alert_webhook_receiver_key_ring() is None


def test_alert_webhook_requires_https_url_and_current_receiver_key() -> None:
    with pytest.raises(
        ValidationError,
        match="alert_webhook_url_requires_receiver_ack_current_key",
    ):
        Settings.model_validate({"ALERT_WEBHOOK_URL": "https:" + "//alerts.example.test/events"})

    insecure_url = "http:" + "//alerts.example.test/events"
    with pytest.raises(ValidationError, match="webhook_url_is_invalid"):
        Settings.model_validate(_alert_webhook_settings(ALERT_WEBHOOK_URL=insecure_url))


@pytest.mark.parametrize("environment", ["production", "prod"])
def test_production_requires_authenticated_alert_receiver(environment: str) -> None:
    with pytest.raises(
        ValidationError,
        match="production_alert_webhook_is_required",
    ):
        Settings.model_validate({"ENV": environment})

    settings = Settings.model_validate(_alert_webhook_settings(ENV=environment))

    assert settings.alert_webhook_receiver_key_ring() is not None


@pytest.mark.parametrize(
    "name",
    [
        "DEAD_MAN_ALERT_WEBHOOK_URL",
        "DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID",
        "DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64",
        "DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID",
        "DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64",
    ],
)
def test_main_worker_rejects_dead_man_receiver_namespace(name: str) -> None:
    forbidden_marker = "dead-man-namespace-input-marker"

    with pytest.raises(
        ValidationError,
        match="dead_man_receiver_namespace_is_forbidden_on_main_worker",
    ) as error:
        Settings.model_validate(_alert_webhook_settings(**{name: forbidden_marker}))

    assert forbidden_marker not in str(error.value)


@pytest.mark.parametrize(
    "auth_value",
    [
        {"ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID": "current"},
        {"ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64": CURRENT_ACK_KEY_B64},
        {"ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID": "previous"},
        {"ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64": PREVIOUS_ACK_KEY_B64},
    ],
)
def test_alert_webhook_authentication_material_requires_url(
    auth_value: dict[str, str],
) -> None:
    with pytest.raises(ValidationError, match="receiver_ack_requires_url"):
        Settings.model_validate(auth_value)


def test_alert_webhook_accepts_current_and_previous_receiver_keys() -> None:
    current_key_id = "main-receiver-sensitive-current-id"
    previous_key_id = "main-receiver-sensitive-previous-id"
    settings = Settings.model_validate(
        _alert_webhook_settings(
            ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID=current_key_id,
            ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID=previous_key_id,
            ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64=(PREVIOUS_ACK_KEY_B64),
        )
    )

    ring = settings.alert_webhook_receiver_key_ring()

    assert ring is not None
    assert ring.accepted_key_ids == (current_key_id, previous_key_id)
    assert current_key_id not in repr(settings)
    assert previous_key_id not in repr(settings)
    assert "alert_webhook_receiver_ack_current_key_id" not in settings.model_dump()
    assert "alert_webhook_receiver_ack_previous_key_id" not in settings.model_dump()
    assert CURRENT_ACK_KEY_B64 not in repr(settings)
    assert PREVIOUS_ACK_KEY_B64 not in repr(settings)


def test_alert_webhook_blank_previous_pair_is_not_treated_as_a_key() -> None:
    settings = Settings.model_validate(
        _alert_webhook_settings(
            ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID="",
            ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64="",
        )
    )

    ring = settings.alert_webhook_receiver_key_ring()

    assert ring is not None
    assert ring.accepted_key_ids == ("current",)


@pytest.mark.parametrize(
    "overrides",
    [
        {"ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID": "previous"},
        {"ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64": PREVIOUS_ACK_KEY_B64},
        {"ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64": base64.b64encode(b"short").decode("ascii")},
        {"ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64": f" {CURRENT_ACK_KEY_B64}"},
    ],
)
def test_alert_webhook_rejects_incomplete_or_noncanonical_receiver_keys(
    overrides: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(_alert_webhook_settings(**overrides))


def test_alert_webhook_validation_errors_hide_url_and_key_inputs() -> None:
    url_marker = "receiver-url-input-marker"
    with pytest.raises(ValidationError) as url_error:
        Settings.model_validate(
            _alert_webhook_settings(ALERT_WEBHOOK_URL=f"http://alerts.example.test/{url_marker}")
        )
    assert url_marker not in str(url_error.value)

    key_marker = "receiver-key-input-marker"
    with pytest.raises(ValidationError) as key_error:
        Settings.model_validate(
            _alert_webhook_settings(ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64=key_marker)
        )
    assert key_marker not in str(key_error.value)


def _alert_webhook_settings(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "ALERT_WEBHOOK_URL": "https:" + "//alerts.example.test/events",
        "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID": "current",
        "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64": CURRENT_ACK_KEY_B64,
    }
    return values | overrides
