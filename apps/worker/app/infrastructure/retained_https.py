from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit


class RetainedHttpsFetchError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedAddress:
    family: int
    socket_type: int
    protocol: int
    socket_address: tuple[object, ...]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        hostname: str,
        port: int,
        address: ResolvedAddress,
        timeout_seconds: int,
    ) -> None:
        tls_context = ssl.create_default_context()
        super().__init__(
            hostname,
            port=port,
            timeout=timeout_seconds,
            context=tls_context,
        )
        self._resolved_address = address
        self._tls_context = tls_context

    def connect(self) -> None:
        address = self._resolved_address
        raw_socket = socket.socket(address.family, address.socket_type, address.protocol)
        try:
            raw_socket.settimeout(self.timeout)
            raw_socket.connect(address.socket_address)
            self.sock = self._tls_context.wrap_socket(raw_socket, server_hostname=self.host)
        except BaseException:
            raw_socket.close()
            raise


def fetch_retained_https(
    uri: str,
    timeout_seconds: int,
    *,
    max_bytes: int,
    user_agent: str,
) -> bytes:
    parts = urlsplit(uri)
    if parts.scheme != "https" or parts.hostname is None:
        raise RetainedHttpsFetchError("retained_uri_must_be_https")
    if parts.username is not None or parts.password is not None:
        raise RetainedHttpsFetchError("retained_uri_credentials_forbidden")
    if parts.query or parts.fragment:
        raise RetainedHttpsFetchError("retained_uri_query_or_fragment_forbidden")
    if not parts.path or parts.path == "/":
        raise RetainedHttpsFetchError("retained_uri_artifact_path_required")
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise RetainedHttpsFetchError("retained_uri_port_invalid") from exc

    addresses = _resolve_global_addresses(parts.hostname, port)
    last_connection_error: BaseException | None = None
    for address in addresses:
        connection = _PinnedHTTPSConnection(
            parts.hostname,
            port,
            address,
            timeout_seconds,
        )
        try:
            connection.request(
                "GET",
                parts.path,
                headers={
                    "Accept": "application/octet-stream",
                    "User-Agent": user_agent,
                },
            )
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise RetainedHttpsFetchError("retained_uri_http_status_rejected")
            return response.read(max_bytes + 1)
        except RetainedHttpsFetchError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            last_connection_error = exc
        finally:
            connection.close()
    raise RetainedHttpsFetchError("retained_uri_connection_failed") from last_connection_error


def _resolve_global_addresses(hostname: str, port: int) -> tuple[ResolvedAddress, ...]:
    try:
        resolved = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise RetainedHttpsFetchError("retained_uri_dns_resolution_failed") from exc
    if not resolved:
        raise RetainedHttpsFetchError("retained_uri_dns_resolution_empty")

    addresses: list[ResolvedAddress] = []
    seen: set[tuple[int, str, int]] = set()
    for family, socket_type, protocol, _canonical_name, socket_address in resolved:
        raw_address = str(socket_address[0])
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise RetainedHttpsFetchError("retained_uri_dns_address_invalid") from exc
        mapped = getattr(address, "ipv4_mapped", None)
        if not address.is_global or (mapped is not None and not mapped.is_global):
            raise RetainedHttpsFetchError("retained_uri_dns_address_must_be_global")
        key = (family, raw_address, int(socket_address[1]))
        if key in seen:
            continue
        seen.add(key)
        addresses.append(
            ResolvedAddress(
                family=family,
                socket_type=socket_type,
                protocol=protocol,
                socket_address=tuple(socket_address),
            )
        )
    if not addresses:
        raise RetainedHttpsFetchError("retained_uri_dns_resolution_empty")
    return tuple(addresses)
