from __future__ import annotations

import re
from hashlib import sha256
from typing import TypeGuard
from urllib.parse import urlsplit

PersistenceAuthority = str

_NAMESPACE_RE = re.compile(r"[a-z][a-z0-9-]{0,47}")
_PROFILE_RE = re.compile(r"[a-z][a-z0-9_]{0,47}")
_AUTHORITY_RE = re.compile(r"[a-z][a-z0-9-]{0,47}:[0-9a-f]{64}")


def persistence_authority_fingerprint(
    *,
    namespace: str,
    origin: str,
    profile: str,
) -> PersistenceAuthority:
    """Return a non-secret binding for one persistence authority.

    The digest binds only the canonical service origin and its public access
    profile. Credentials must never be supplied to this function.
    """

    if type(namespace) is not str or _NAMESPACE_RE.fullmatch(namespace) is None:
        raise ValueError("persistence_authority_namespace_invalid")
    if type(profile) is not str or _PROFILE_RE.fullmatch(profile) is None:
        raise ValueError("persistence_authority_profile_invalid")
    canonical_origin = _canonical_origin(origin)
    material = f"v1\n{namespace}\n{canonical_origin}\n{profile}".encode()
    return f"{namespace}:{sha256(material).hexdigest()}"


def is_persistence_authority(value: object) -> TypeGuard[PersistenceAuthority]:
    return type(value) is str and _AUTHORITY_RE.fullmatch(value) is not None


def _canonical_origin(value: object) -> str:
    if type(value) is not str or value != value.strip():
        raise ValueError("persistence_authority_origin_invalid")
    parts = None
    hostname: str | None = None
    port: int | None = None
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        port = parts.port
    except (AttributeError, TypeError, ValueError):
        parts = None
        hostname = None
        port = None
    if parts is None:
        raise ValueError("persistence_authority_origin_invalid")
    scheme = parts.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or hostname is None
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise ValueError("persistence_authority_origin_invalid")
    canonical_hostname = hostname.lower()
    if ":" in canonical_hostname:
        canonical_hostname = f"[{canonical_hostname}]"
    default_port = 80 if scheme == "http" else 443
    port_suffix = "" if port in {None, default_port} else f":{port}"
    return f"{scheme}://{canonical_hostname}{port_suffix}"
