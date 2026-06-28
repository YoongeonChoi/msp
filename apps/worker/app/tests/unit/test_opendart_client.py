from __future__ import annotations

from io import BytesIO
from urllib.parse import parse_qs
from zipfile import ZipFile

import httpx
import pytest

from app.adapters.fundamentals import opendart_client
from app.adapters.fundamentals.opendart_client import OpenDartClient, parse_corp_code_zip
from app.domain.common.errors import ProviderAuthError, ProviderSchemaError


async def test_opendart_client_maps_corp_code_and_financial_statement_accounts() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        query = parse_qs(request.url.query.decode())
        if request.url.path == "/api/corpCode.xml":
            assert query == {"crtfc_key": ["dart-key"]}
            return httpx.Response(200, content=_corp_code_zip(), request=request)
        if request.url.path == "/api/fnlttSinglAcntAll.json":
            assert query == {
                "crtfc_key": ["dart-key"],
                "corp_code": ["00126380"],
                "bsns_year": ["2025"],
                "reprt_code": ["11011"],
                "fs_div": ["CFS"],
            }
            return httpx.Response(
                200,
                json={
                    "status": "000",
                    "message": "정상",
                    "list": [
                        {"account_nm": "매출액", "thstrm_amount": "10,000"},
                        {"account_nm": "영업이익", "thstrm_amount": "1,500"},
                        {"account_nm": "당기순이익", "thstrm_amount": "1,000"},
                        {"account_nm": "자산총계", "thstrm_amount": "20,000"},
                        {"account_nm": "부채총계", "thstrm_amount": "8,000"},
                        {"account_nm": "자본총계", "thstrm_amount": "12,000"},
                    ],
                },
                request=request,
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenDartClient(api_key="dart-key", client=http_client, business_year=2025)

    fundamentals = await client.get_latest("005930")

    assert fundamentals is not None
    assert fundamentals.per is None
    assert fundamentals.pbr is None
    assert fundamentals.operating_margin == 0.15
    assert fundamentals.roe == pytest.approx(1_000 / 12_000)
    assert fundamentals.debt_ratio == pytest.approx(8_000 / 12_000)
    assert [request.url.path for request in requests] == [
        "/api/corpCode.xml",
        "/api/fnlttSinglAcntAll.json",
    ]
    await http_client.aclose()


async def test_opendart_client_fails_closed_without_api_key() -> None:
    client = OpenDartClient()

    assert await client.provider_health() is False
    with pytest.raises(ProviderAuthError) as exc_info:
        await client.get_latest("005930")

    assert exc_info.value.safe_message == "opendart_api_key_missing"
    await client.aclose()


def test_parse_corp_code_zip_uses_official_corpcode_xml_shape() -> None:
    corp_codes = parse_corp_code_zip(_corp_code_zip())

    assert corp_codes["005930"].corp_code == "00126380"
    assert corp_codes["005930"].corp_name == "삼성전자"
    assert corp_codes["005930"].modify_date == "20250630"


def test_parse_corp_code_zip_rejects_oversized_xml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(opendart_client, "MAX_CORP_CODE_XML_BYTES", 32)

    with pytest.raises(ProviderSchemaError) as exc_info:
        parse_corp_code_zip(_corp_code_zip())

    assert exc_info.value.safe_message == "opendart_corp_code_xml_too_large"


def test_parse_corp_code_zip_rejects_too_many_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(opendart_client, "MAX_CORP_CODE_ENTRIES", 0)

    with pytest.raises(ProviderSchemaError) as exc_info:
        parse_corp_code_zip(_corp_code_zip())

    assert exc_info.value.safe_message == "opendart_corp_code_xml_too_many_entries"


def test_parse_corp_code_zip_rejects_dtd_entity_declarations() -> None:
    with pytest.raises(ProviderSchemaError) as exc_info:
        parse_corp_code_zip(
            _corp_code_zip_with_xml(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                "<!DOCTYPE result [\n"
                '  <!ENTITY repeated "entity-expansion-blocked">\n'
                "]>\n"
                "<result>\n"
                "  <list>\n"
                "    <corp_code>00126380</corp_code>\n"
                "    <corp_name>&repeated;</corp_name>\n"
                "    <stock_code>005930</stock_code>\n"
                "    <modify_date>20250630</modify_date>\n"
                "  </list>\n"
                "</result>\n"
            )
        )

    assert exc_info.value.safe_message == "opendart_corp_code_xml_dtd_forbidden"


def _corp_code_zip() -> bytes:
    return _corp_code_zip_with_xml(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<result>\n"
        "  <list>\n"
        "    <corp_code>00126380</corp_code>\n"
        "    <corp_name>삼성전자</corp_name>\n"
        "    <stock_code>005930</stock_code>\n"
        "    <modify_date>20250630</modify_date>\n"
        "  </list>\n"
        "</result>\n"
    )


def _corp_code_zip_with_xml(xml: str) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("CORPCODE.xml", xml)
    return buffer.getvalue()
