from __future__ import annotations


class TradingError(Exception):
    pass


class KnownFailClosedError(TradingError):
    def __init__(self, component: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.component = component
        self.safe_message = safe_message


class ProviderError(KnownFailClosedError):
    provider: str

    def __init__(self, provider: str, safe_message: str) -> None:
        super().__init__(component=provider, safe_message=safe_message)
        self.provider = provider


class ProviderAuthError(ProviderError):
    pass


class ProviderRateLimitError(ProviderError):
    pass


class ProviderTimeoutError(ProviderError):
    pass


class ProviderSchemaError(ProviderError):
    pass


class ProviderUnavailableError(ProviderError):
    pass


class ProviderUnknownError(ProviderError):
    pass
