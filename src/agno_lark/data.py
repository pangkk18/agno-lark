"""Read-only Lark/Feishu Sheets and Base access.

The client deliberately exposes query operations only.  It accepts an injected
SDK client, which keeps tests and applications that already own a Channel SDK
client free from another connection or credential store.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from agno.tools import Toolkit

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,127}$")
_TABLE_RE = re.compile(r"^tbl[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class SheetReadResult:
    spreadsheet_token: str
    cell_range: str
    values: list[list[Any]]
    truncated: bool = False


@dataclass(frozen=True)
class BaseTableListResult:
    app_token: str
    tables: list[dict[str, Any]]
    page_token: str | None = None
    has_more: bool = False
    truncated: bool = False


@dataclass(frozen=True)
class BaseRecordListResult:
    app_token: str
    table_id: str
    records: list[dict[str, Any]]
    page_token: str | None = None
    has_more: bool = False
    truncated: bool = False


def _resource_token(value: str, kind: str) -> str:
    value = value.strip()
    if "://" in value:
        parsed = urlparse(value)
        parts = [p for p in parsed.path.split("/") if p]
        markers = {"sheets"} if kind == "sheet" else {"base", "bitable"}
        for index, part in enumerate(parts[:-1]):
            if part in markers:
                value = parts[index + 1]
                break
        else:
            raise ValueError(f"invalid {kind} URL")
    value = value.split("?", 1)[0].split("#", 1)[0].strip("/")
    if not _TOKEN_RE.fullmatch(value):
        raise ValueError(f"invalid {kind} token")
    return value


def parse_sheet_token(value: str) -> str:
    return _resource_token(value, "sheet")


def parse_base_token(value: str) -> str:
    return _resource_token(value, "base")


def validate_cell_range(value: str) -> str:
    value = value.strip()
    if (
        not value
        or len(value) > 200
        or not re.fullmatch(r"(?:[^!]+!)?[A-Z]{1,3}[1-9][0-9]*(?::[A-Z]{1,3}[1-9][0-9]*)?", value)
    ):
        raise ValueError("a bounded A1 range is required, for example Sheet1!A1:D20")
    return value


def validate_table_id(value: str) -> str:
    value = value.strip()
    if not _TABLE_RE.fullmatch(value):
        raise ValueError("a valid Base table_id (tbl...) is required")
    return value


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "to_dict"):
        return _plain(value.to_dict())
    if hasattr(value, "__dict__"):
        return {k: _plain(v) for k, v in vars(value).items() if not k.startswith("_")}
    return str(value)


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _ensure_sdk_success(response: Any) -> None:
    code = _field(response, "code")
    if code not in (None, 0):
        raise RuntimeError("Lark API returned an error")


class LarkDataClient:
    """Async, bounded, read-only client for Sheets and Base."""

    def __init__(
        self,
        settings: Any,
        *,
        client: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
        max_rows: int | None = None,
        max_cells: int | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or self._build_client()
        self.http_client = http_client
        self.max_rows = max_rows or int(getattr(settings, "lark_data_max_rows", 100))
        self.max_cells = max_cells or int(getattr(settings, "lark_data_max_cells", 2000))
        self._token: str | None = None

    def _build_client(self) -> Any:
        from lark_oapi import Client  # type: ignore[import-untyped]

        return (
            Client.builder()
            .app_id(self.settings.feishu_app_id)
            .app_secret(self.settings.feishu_app_secret.get_secret_value())
            .domain(str(self.settings.lark_api_base_url).rstrip("/"))
            .build()
        )

    async def _access_token(self) -> str:
        if self._token:
            return self._token
        auth = getattr(
            getattr(getattr(self.client, "auth", None), "v3", None), "tenant_access_token", None
        )
        internal = getattr(auth, "internal", None)
        if internal is None:
            raise RuntimeError("Lark client does not provide tenant token authentication")
        from lark_oapi.api.auth.v3.model.internal_tenant_access_token_request import (  # type: ignore[import-untyped]
            InternalTenantAccessTokenRequest,
        )
        from lark_oapi.api.auth.v3.model.internal_tenant_access_token_request_body import (  # type: ignore[import-untyped]
            InternalTenantAccessTokenRequestBody,
        )

        body = (
            InternalTenantAccessTokenRequestBody.builder()
            .app_id(self.settings.feishu_app_id)
            .app_secret(self.settings.feishu_app_secret.get_secret_value())
            .build()
        )
        result = internal(InternalTenantAccessTokenRequest.builder().request_body(body).build())
        if inspect.isawaitable(result):
            result = await result
        token = _field(_field(result, "data"), "tenant_access_token") or _field(
            result, "tenant_access_token"
        )
        if not token:
            raise RuntimeError("Lark tenant token response was empty")
        self._token = str(token)
        return self._token

    async def read_sheet_values(
        self, spreadsheet_url_or_token: str, cell_range: str
    ) -> SheetReadResult:
        token = parse_sheet_token(spreadsheet_url_or_token)
        cell_range = validate_cell_range(cell_range)
        own_client = self.http_client is None
        http = self.http_client or httpx.AsyncClient(timeout=float(self.settings.lark_api_timeout))
        try:
            base = str(self.settings.lark_api_base_url).rstrip("/")
            encoded_range = quote(cell_range, safe="!:")
            response = await http.get(
                f"{base}/open-apis/sheets/v2/spreadsheets/{token}/values/{encoded_range}",
                headers={"Authorization": f"Bearer {await self._access_token()}"},
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") not in (None, 0):
                raise RuntimeError("Lark Sheets API returned an error")
        finally:
            if own_client:
                await http.aclose()
        values = _field(_field(payload, "data", {}), "valueRange", {})
        values = _field(values, "values", []) or []
        rows = [list(row) if isinstance(row, (list, tuple)) else [row] for row in values]
        truncated = len(rows) > self.max_rows or sum(len(row) for row in rows) > self.max_cells
        rows = rows[: self.max_rows]
        count = 0
        bounded: list[list[Any]] = []
        for row in rows:
            remaining = self.max_cells - count
            if remaining <= 0:
                truncated = True
                break
            bounded.append(row[:remaining])
            count += len(bounded[-1])
            if len(bounded[-1]) < len(row):
                truncated = True
        return SheetReadResult(token, cell_range, bounded, truncated)

    async def list_base_tables(self, base_url_or_token: str) -> BaseTableListResult:
        app = parse_base_token(base_url_or_token)
        page_size = min(int(getattr(self.settings, "lark_base_page_size", 50)), self.max_rows)
        from lark_oapi.api.bitable.v1 import ListAppTableRequest  # type: ignore[import-untyped]

        request = ListAppTableRequest.builder().app_token(app).page_size(page_size).build()
        response = await self.client.bitable.v1.app_table.alist(request)
        _ensure_sdk_success(response)
        data = _field(response, "data", response)
        items = _field(data, "items", []) or []
        return BaseTableListResult(
            app,
            [_plain(item) for item in items[: self.max_rows]],
            _field(data, "page_token"),
            bool(_field(data, "has_more", False)),
            len(items) > self.max_rows,
        )

    async def list_base_records(
        self,
        base_url_or_token: str,
        table_id: str,
        *,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> BaseRecordListResult:
        return await self._base_records(
            base_url_or_token, table_id, page_token=page_token, page_size=page_size
        )

    async def _base_records(
        self,
        base_url_or_token: str,
        table_id: str,
        *,
        page_token: str | None,
        page_size: int | None,
        keyword: str | None = None,
    ) -> BaseRecordListResult:
        app, table = parse_base_token(base_url_or_token), validate_table_id(table_id)
        size = min(
            page_size or int(getattr(self.settings, "lark_base_page_size", 50)), self.max_rows
        )
        if keyword is None:
            from lark_oapi.api.bitable.v1 import ListAppTableRecordRequest

            builder = (
                ListAppTableRecordRequest.builder().app_token(app).table_id(table).page_size(size)
            )
            request = builder.page_token(page_token).build() if page_token else builder.build()
            response = await self.client.bitable.v1.app_table_record.alist(request)
        else:
            from lark_oapi.api.bitable.v1 import (
                Condition,
                FilterInfo,
                ListAppTableRecordRequest,
                SearchAppTableRecordRequest,
                SearchAppTableRecordRequestBody,
            )
            condition = (
                Condition.builder()
                .field_name("关键词")
                .operator("contains")
                .value([keyword])
                .build()
            )
            body = (
                SearchAppTableRecordRequestBody.builder()
                .filter(FilterInfo.builder().conjunction("or").conditions([condition]).build())
                .build()
            )
            builder = (
                SearchAppTableRecordRequest.builder().app_token(app).table_id(table).page_size(size)
            )
            if body is not None:
                builder = builder.request_body(body)
            if page_token:
                builder = builder.page_token(page_token)
            try:
                response = await self.client.bitable.v1.app_table_record.asearch(builder.build())
                _ensure_sdk_success(response)
            except Exception:
                # The search API requires a field name.  If a Base does not
                # have the conventional ``关键词`` field, fall back to a
                # bounded record read and match all returned field values.
                list_request = (
                    ListAppTableRecordRequest.builder()
                    .app_token(app)
                    .table_id(table)
                    .page_size(size)
                    .build()
                )
                response = await self.client.bitable.v1.app_table_record.alist(list_request)
                data = _field(response, "data", response)
                items = _field(data, "items", []) or []
                matched = [
                    item
                    for item in items
                    if keyword.lower() in str(_plain(item)).lower()
                ]
                return BaseRecordListResult(
                    app,
                    table,
                    [_plain(item) for item in matched[: self.max_rows]],
                    _field(data, "page_token"),
                    bool(_field(data, "has_more", False)),
                    len(matched) > self.max_rows,
                )
        _ensure_sdk_success(response)
        data = _field(response, "data", response)
        items = _field(data, "items", []) or []
        return BaseRecordListResult(
            app,
            table,
            [_plain(item) for item in items[: self.max_rows]],
            _field(data, "page_token"),
            bool(_field(data, "has_more", False)),
            len(items) > self.max_rows,
        )

    async def search_base_records(
        self,
        base_url_or_token: str,
        table_id: str,
        keyword: str,
        *,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> BaseRecordListResult:
        if not keyword.strip():
            raise ValueError("keyword is required")
        return await self._base_records(
            base_url_or_token,
            table_id,
            page_token=page_token,
            page_size=page_size,
            keyword=keyword.strip(),
        )


class LarkSheetsTools(Toolkit):
    def __init__(self, client: LarkDataClient, **kwargs: Any) -> None:
        self.client = client
        super().__init__(
            name="lark_sheets", async_tools=[(self.read_sheet, "read_sheet")], **kwargs
        )

    async def read_sheet(self, spreadsheet_url_or_token: str, cell_range: str) -> dict[str, Any]:
        result = await self.client.read_sheet_values(spreadsheet_url_or_token, cell_range)
        return {
            "spreadsheet_token": result.spreadsheet_token,
            "range": result.cell_range,
            "values": result.values,
            "truncated": result.truncated,
        }


class LarkBaseTools(Toolkit):
    def __init__(self, client: LarkDataClient, **kwargs: Any) -> None:
        self.client = client
        super().__init__(
            name="lark_base",
            async_tools=[
                (self.list_tables, "list_base_tables"),
                (self.list_records, "list_base_records"),
                (self.search_records, "search_base_records"),
            ],
            **kwargs,
        )

    async def list_tables(self, base_url_or_token: str) -> dict[str, Any]:
        return vars(await self.client.list_base_tables(base_url_or_token))

    async def list_records(self, base_url_or_token: str, table_id: str) -> dict[str, Any]:
        return vars(await self.client.list_base_records(base_url_or_token, table_id))

    async def search_records(
        self, base_url_or_token: str, table_id: str, keyword: str
    ) -> dict[str, Any]:
        return vars(await self.client.search_base_records(base_url_or_token, table_id, keyword))


class LarkDataTools(LarkSheetsTools, LarkBaseTools):
    def __init__(self, client: LarkDataClient, **kwargs: Any) -> None:
        Toolkit.__init__(
            self,
            name="lark_data",
            async_tools=[
                (self.read_sheet, "read_sheet"),
                (self.list_tables, "list_base_tables"),
                (self.list_records, "list_base_records"),
                (self.search_records, "search_base_records"),
            ],
            **kwargs,
        )
        self.client = client


def build_lark_data_client(settings: Any, channel: Any | None = None) -> LarkDataClient:
    return LarkDataClient(settings, client=getattr(channel, "client", None))


__all__ = [
    "LarkDataClient",
    "LarkSheetsTools",
    "LarkBaseTools",
    "LarkDataTools",
    "SheetReadResult",
    "BaseTableListResult",
    "BaseRecordListResult",
    "build_lark_data_client",
    "parse_sheet_token",
    "parse_base_token",
    "validate_cell_range",
    "validate_table_id",
]
