from __future__ import annotations

from collections.abc import Awaitable, Callable

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter


def retry_safe_read[T](func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    return retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.2, max=2.0),
        retry=retry_if_exception_type(TimeoutError),
        reraise=True,
    )(func)
