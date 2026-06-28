from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.domain.common.errors import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderUnknownError,
)
from app.domain.common.time import now_kst
from app.domain.fundamentals.entities import QuarterlyFundamentals

OPENDART_CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
OPENDART_SINGLE_ACCOUNT_ALL_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
ANNUAL_REPORT_CODE = "11011"
CONSOLIDATED_FS = "CFS"
MAX_CORP_CODE_ZIP_BYTES = 50 * 1024 * 1024
MAX_CORP_CODE_XML_BYTES = 100 * 1024 * 1024
MAX_CORP_CODE_ENTRIES = 200_000


class OpenDartFinancialRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    account_nm: str
    thstrm_amount: str = ""


class OpenDartFinancialResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    status: str
    message: str = ""
    items: list[OpenDartFinancialRow] = Field(default_factory=list, alias="list")


@dataclass(frozen=True, slots=True)
class OpenDartCorpCode:
    corp_code: str
    corp_name: str
    stock_code: str
    modify_date: str


class OpenDartClient:
    def __init__(
        self,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        business_year: int | None = None,
    ) -> None:
        self.api_key = api_key
        self.client = client or httpx.AsyncClient(timeout=20.0)
        self.business_year = business_year
        self._corp_codes: dict[str, OpenDartCorpCode] | None = None

    async def provider_health(self) -> bool:
        return bool(self.api_key)

    async def get_latest(self, symbol: str) -> QuarterlyFundamentals | None:
        if not self.api_key:
            raise ProviderAuthError("opendart", "opendart_api_key_missing")
        corp_code = await self._corp_code_for_symbol(symbol)
        if corp_code is None:
            return None
        rows = await self._load_financial_rows(corp_code.corp_code)
        if rows is None:
            return None
        return _fundamentals_from_rows(symbol, rows)

    async def _corp_code_for_symbol(self, symbol: str) -> OpenDartCorpCode | None:
        if self._corp_codes is None:
            self._corp_codes = await self._load_corp_codes()
        return self._corp_codes.get(symbol)

    async def _load_corp_codes(self) -> dict[str, OpenDartCorpCode]:
        assert self.api_key is not None
        try:
            response = await self.client.get(
                OPENDART_CORP_CODE_URL,
                params={"crtfc_key": self.api_key},
            )
            if response.is_error:
                _raise_for_http_status("opendart", response)
            return parse_corp_code_zip(response.content)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("opendart", "opendart_timeout") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError("opendart", "opendart_http_error") from exc

    async def _load_financial_rows(self, corp_code: str) -> list[OpenDartFinancialRow] | None:
        assert self.api_key is not None
        try:
            response = await self.client.get(
                OPENDART_SINGLE_ACCOUNT_ALL_URL,
                params={
                    "crtfc_key": self.api_key,
                    "corp_code": corp_code,
                    "bsns_year": str(self.business_year or now_kst().year - 1),
                    "reprt_code": ANNUAL_REPORT_CODE,
                    "fs_div": CONSOLIDATED_FS,
                },
            )
            if response.is_error:
                _raise_for_http_status("opendart", response)
            payload = OpenDartFinancialResponse.model_validate_json(response.text)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("opendart", "opendart_timeout") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError("opendart", "opendart_http_error") from exc
        except ValidationError as exc:
            raise ProviderSchemaError("opendart", "opendart_financial_schema_mismatch") from exc
        if payload.status == "013":
            return None
        if payload.status != "000":
            raise ProviderUnavailableError("opendart", f"opendart_status_{payload.status}")
        return payload.items

    async def aclose(self) -> None:
        await self.client.aclose()


def parse_corp_code_zip(content: bytes) -> dict[str, OpenDartCorpCode]:
    if len(content) > MAX_CORP_CODE_ZIP_BYTES:
        raise ProviderSchemaError("opendart", "opendart_corp_code_zip_too_large")
    try:
        with ZipFile(BytesIO(content)) as archive:
            info = archive.getinfo("CORPCODE.xml")
            if info.file_size > MAX_CORP_CODE_XML_BYTES:
                raise ProviderSchemaError("opendart", "opendart_corp_code_xml_too_large")
            with archive.open(info) as xml_file:
                xml_bytes = xml_file.read(MAX_CORP_CODE_XML_BYTES + 1)
    except (BadZipFile, KeyError) as exc:
        raise ProviderSchemaError("opendart", "opendart_corp_code_zip_invalid") from exc
    if len(xml_bytes) > MAX_CORP_CODE_XML_BYTES:
        raise ProviderSchemaError("opendart", "opendart_corp_code_xml_too_large")
    _reject_unsafe_xml_declarations(xml_bytes)
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as exc:
        raise ProviderSchemaError("opendart", "opendart_corp_code_xml_invalid") from exc
    corp_codes: dict[str, OpenDartCorpCode] = {}
    for entry_count, entry in enumerate(root.findall("list"), start=1):
        if entry_count > MAX_CORP_CODE_ENTRIES:
            raise ProviderSchemaError("opendart", "opendart_corp_code_xml_too_many_entries")
        corp_code = _xml_text(entry, "corp_code")
        corp_name = _xml_text(entry, "corp_name")
        stock_code = _xml_text(entry, "stock_code")
        modify_date = _xml_text(entry, "modify_date")
        if not corp_code or not stock_code:
            continue
        corp_codes[stock_code] = OpenDartCorpCode(
            corp_code=corp_code,
            corp_name=corp_name,
            stock_code=stock_code,
            modify_date=modify_date,
        )
    return corp_codes


def _reject_unsafe_xml_declarations(xml_bytes: bytes) -> None:
    normalized = xml_bytes.lower()
    if b"<!doctype" in normalized or b"<!entity" in normalized:
        raise ProviderSchemaError("opendart", "opendart_corp_code_xml_dtd_forbidden")


def _fundamentals_from_rows(symbol: str, rows: list[OpenDartFinancialRow]) -> QuarterlyFundamentals:
    amounts = {
        _normalize_account_name(row.account_nm): _parse_amount(row.thstrm_amount) for row in rows
    }
    revenue = _first_amount(amounts, ("매출액", "수익매출액", "영업수익"))
    operating_income = _first_amount(amounts, ("영업이익",))
    net_income = _first_amount(amounts, ("당기순이익", "분기순이익", "반기순이익"))
    assets = _first_amount(amounts, ("자산총계",))
    liabilities = _first_amount(amounts, ("부채총계",))
    equity = _first_amount(amounts, ("자본총계",))
    if equity is None and assets is not None and liabilities is not None:
        equity = assets - liabilities
    return QuarterlyFundamentals(
        symbol=symbol,
        per=None,
        pbr=None,
        roe=_ratio(net_income, equity),
        operating_margin=_ratio(operating_income, revenue),
        debt_ratio=_ratio(liabilities, equity),
    )


def _first_amount(amounts: dict[str, int | None], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = amounts.get(key)
        if value is not None:
            return value
    return None


def _parse_amount(value: str) -> int | None:
    normalized = value.replace(",", "").strip()
    if not normalized or normalized == "-":
        return None
    try:
        return int(normalized)
    except ValueError:
        return None


def _ratio(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _normalize_account_name(value: str) -> str:
    return "".join(char for char in value if char.isalnum())


def _xml_text(entry: ElementTree.Element, tag: str) -> str:
    child = entry.find(tag)
    return "" if child is None or child.text is None else child.text.strip()


def _raise_for_http_status(provider: str, response: httpx.Response) -> None:
    match response.status_code:
        case 401 | 403:
            raise ProviderAuthError(provider, f"{provider}_http_{response.status_code}")
        case 429:
            raise ProviderRateLimitError(provider, f"{provider}_http_429")
        case 500 | 502 | 503 | 504:
            raise ProviderUnavailableError(provider, f"{provider}_http_{response.status_code}")
        case _:
            raise ProviderUnknownError(provider, f"{provider}_http_{response.status_code}")
