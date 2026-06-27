from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

import httpx
from pydantic import ValidationError

from app.adapters.broker.toss_models import TossOAuthError, TossOAuthToken
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

TOSS_OPENAPI_BASE_URL = "https://openapi.tossinvest.com"
TOKEN_REFRESH_SKEW_SEC = 60.0


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
        try:
            response = await self.client.post(
                f"{self.base_url}/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.credentials.client_id,
                    "client_secret": self.credentials.client_secret,
                },
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            _raise_for_toss_status(response)
            return TossOAuthToken.model_validate_json(response.text)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("toss", "toss_auth_timeout") from exc
        except httpx.HTTPStatusError as exc:
            raise _provider_error_from_response(exc.response) from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailableError("toss", "toss_auth_request_failed") from exc
        except ValidationError as exc:
            raise ProviderSchemaError("toss", "toss_auth_schema_invalid") from exc

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()


def _credentials_from_settings(settings: Settings) -> TossCredentials:
    if settings.toss_client_id is None or settings.toss_client_secret is None:
        raise ProviderAuthError("toss", "toss_credentials_missing")
    return TossCredentials(
        client_id=settings.toss_client_id.get_secret_value(),
        client_secret=settings.toss_client_secret.get_secret_value(),
    )


def _raise_for_toss_status(response: httpx.Response) -> None:
    if response.is_error:
        response.raise_for_status()


def _provider_error_from_response(response: httpx.Response) -> ProviderError:
    safe_code = _safe_error_code(response)
    match response.status_code:
        case 400 | 401 | 403:
            return ProviderAuthError("toss", safe_code)
        case 429:
            return ProviderRateLimitError("toss", safe_code)
        case 500 | 502 | 503 | 504:
            return ProviderUnavailableError("toss", safe_code)
        case _:
            return ProviderUnknownError("toss", safe_code)


def _safe_error_code(response: httpx.Response) -> str:
    try:
        oauth_error = TossOAuthError.model_validate_json(response.text)
    except ValidationError:
        return f"toss_http_{response.status_code}"
    return f"toss_{oauth_error.error}"
