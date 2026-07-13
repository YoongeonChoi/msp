from __future__ import annotations


def supabase_api_headers(api_key: str) -> dict[str, str]:
    headers = {"apikey": api_key}
    if not _is_new_api_key(api_key):
        headers["authorization"] = "Bearer " + api_key
    return headers


def _is_new_api_key(api_key: str) -> bool:
    return api_key.startswith(("sb_publishable_", "sb_secret_"))
