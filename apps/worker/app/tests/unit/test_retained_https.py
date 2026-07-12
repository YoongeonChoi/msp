from __future__ import annotations

import socket
from typing import cast

import pytest

import app.infrastructure.retained_https as retained_https
from app.infrastructure.retained_https import RetainedHttpsFetchError, fetch_retained_https


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self, limit: int) -> bytes:
        return self._body[:limit]


class _Connection:
    status = 200
    body = b"artifact"
    instances: list[_Connection] = []
    failing_addresses: set[str] = set()

    def __init__(self, hostname: str, port: int, address: object, timeout: int) -> None:
        self.hostname = hostname
        self.port = port
        self.address = address
        self.timeout = timeout
        self.request_target: str | None = None
        self.closed = False
        self.__class__.instances.append(self)

    def request(self, method: str, target: str, headers: dict[str, str]) -> None:
        assert method == "GET"
        assert headers["User-Agent"] == "verifier-test"
        address = cast(retained_https.ResolvedAddress, self.address)
        if str(address.socket_address[0]) in self.failing_addresses:
            raise OSError("fixture connection failure")
        self.request_target = target

    def getresponse(self) -> _Response:
        return _Response(self.__class__.status, self.__class__.body)

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _fake_public_dns_and_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    _Connection.instances.clear()
    _Connection.status = 200
    _Connection.body = b"artifact"
    _Connection.failing_addresses.clear()
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))
        ],
    )
    monkeypatch.setattr(retained_https, "_PinnedHTTPSConnection", _Connection)


def test_retained_https_pins_a_validated_public_address() -> None:
    body = fetch_retained_https(
        "https://artifacts.example.net/reports/evidence.json",
        3,
        max_bytes=100,
        user_agent="verifier-test",
    )

    assert body == b"artifact"
    connection = _Connection.instances[0]
    assert connection.hostname == "artifacts.example.net"
    address = cast(retained_https.ResolvedAddress, connection.address)
    assert address.socket_address == ("93.184.216.34", 443)
    assert connection.request_target == "/reports/evidence.json"
    assert connection.closed is True


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_retained_https_rejects_redirects_without_following(status: int) -> None:
    _Connection.status = status

    with pytest.raises(RetainedHttpsFetchError, match="http_status_rejected"):
        fetch_retained_https(
            "https://artifacts.example.net/reports/evidence.json",
            3,
            max_bytes=100,
            user_agent="verifier-test",
        )

    assert len(_Connection.instances) == 1
    assert _Connection.instances[0].closed is True


def test_retained_https_rejects_any_non_global_dns_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443)),
        ],
    )

    with pytest.raises(RetainedHttpsFetchError, match="dns_address_must_be_global"):
        fetch_retained_https(
            "https://artifacts.example.net/reports/evidence.json",
            3,
            max_bytes=100,
            user_agent="verifier-test",
        )

    assert _Connection.instances == []


def test_retained_https_falls_back_to_next_validated_public_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Connection.failing_addresses.add("2001:4860:4860::8888")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("2001:4860:4860::8888", 443, 0, 0),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            ),
        ],
    )

    body = fetch_retained_https(
        "https://artifacts.example.net/reports/evidence.json",
        3,
        max_bytes=100,
        user_agent="verifier-test",
    )

    assert body == b"artifact"
    assert len(_Connection.instances) == 2
    assert all(connection.closed for connection in _Connection.instances)
