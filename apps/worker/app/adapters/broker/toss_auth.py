from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

import httpx
from pydantic import ValidationError

from app.adapters.broker.toss_models import TossOAuthToken
from app.config import Settings
from app.domain.common.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderUnknownError,
)
from app.infrastructure.bounded_json import (
    BoundedJsonError,
    bounded_json_response,
)

TOSS_OPENAPI_BASE_URL = "https://openapi.tossinvest.com"
TOKEN_REFRESH_SKEW_SEC = 60.0
TOSS_AUTH_MAX_RESPONSE_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class TossCredentials:
    client_id: str
    client_secret: str


class TossAuth:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        base_url: str = TOSS_OPENAPI_BASE_URL,
    ) -> None:
        self.credentials = _credentials_from_settings(settings)
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = client is None
        self._cached_token: TossOAuthToken | None = None
        self._expires_at_monotonic = 0.0

    async def access_token(self) -> str:
        if self._cached_token is not None and monotonic() < self._expires_at_monotonic:
            return self._cached_token.access_token
        token = await self._issue_token()
        self._cached_token = token
        self._expires_at_monotonic = monotonic() + max(
            0.0, float(token.expires_in) - TOKEN_REFRESH_SKEW_SEC
        )
        return token.access_token

    async def _issue_token(self) -> TossOAuthToken:
        credentials = self.credentials
        if credentials is None:
            raise ProviderAuthError("toss", "toss_credentials_missing")
        token: TossOAuthToken | None = None
        transport_failure: str | None = None
        try:
            async with self.client.stream(
                "POST",
                f"{self.base_url}/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": credentials.client_id,
                    "client_secret": credentials.client_secret,
                },
                headers={
                    "accept-encoding": "identity",
                    "content-type": "application/x-www-form-urlencoded",
                },
            ) as response:
                payload: object = None
                body_invalid = False
                try:
                    payload = await bounded_json_response(
                        response,
                        max_bytes=TOSS_AUTH_MAX_RESPONSE_BYTES,
                    )
                except BoundedJsonError:
                    body_invalid = True
                if body_invalid:
                    if response.is_error:
                        raise _provider_error_from_status(response.status_code)
                    raise ProviderSchemaError(
                        "toss",
                        "toss_auth_schema_invalid",
                    )
                if response.is_error:
                    raise _provider_error_from_status(response.status_code)
                validation_failed = False
                try:
                    token = TossOAuthToken.model_validate(payload)
                except ValidationError:
                    validation_failed = True
                if validation_failed:
                    raise ProviderSchemaError(
                        "toss",
                        "toss_auth_schema_invalid",
                    )
        except httpx.TimeoutException:
            transport_failure = "timeout"
        except httpx.RequestError:
            transport_failure = "request"
        if transport_failure == "timeout":
            raise ProviderTimeoutError("toss", "toss_auth_timeout")
        if transport_failure == "request":
            raise ProviderUnavailableError("toss", "toss_auth_request_failed")
        if token is None:
            raise ProviderSchemaError("toss", "toss_auth_schema_invalid")
        return token

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()


def _credentials_from_settings(settings: Settings) -> TossCredentials | None:
    if settings.toss_client_id is None or settings.toss_client_secret is None:
        return None
    return TossCredentials(
        client_id=settings.toss_client_id.get_secret_value(),
        client_secret=settings.toss_client_secret.get_secret_value(),
    )


def _provider_error_from_status(status_code: int) -> ProviderError:
    safe_code = f"toss_http_{status_code}"
    match status_code:
        case 400 | 401 | 403:
            return ProviderAuthError("toss", safe_code)
        case 429:
            return ProviderRateLimitError("toss", safe_code)
        case 500 | 502 | 503 | 504:
            return ProviderUnavailableError("toss", safe_code)
        case _:
            return ProviderUnknownError("toss", safe_code)
