"""Read-only Lark/Feishu Sheets, Base, Wiki, and Lingo access.

The client deliberately exposes query operations only.  Data access owns an
OpenAPI SDK client, separate from the messaging channel's client.  Applications
may also inject a compatible OpenAPI client.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from agno.tools import Toolkit

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,127}$")
_TABLE_RE = re.compile(r"^tbl[A-Za-z0-9_-]+$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


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


@dataclass(frozen=True)
class WikiReadResult:
    node: dict[str, Any]
    content: str | None = None
    values: list[list[Any]] | None = None
    records: list[dict[str, Any]] | None = None
    truncated: bool = False
    page_token: str | None = None
    has_more: bool = False


@dataclass(frozen=True)
class WikiNodeListResult:
    nodes: list[dict[str, Any]]
    page_token: str | None = None
    has_more: bool = False
    truncated: bool = False


@dataclass(frozen=True)
class LingoListResult:
    items: list[dict[str, Any]]
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
        return {
            k: _plain(v)
            for k, v in vars(value).items()
            if not k.startswith("_") and v is not None
        }
    return str(value)


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _ensure_sdk_success(response: Any) -> None:
    code = _field(response, "code")
    success = getattr(response, "success", None)
    sdk_failed = False
    if callable(success):
        try:
            sdk_failed = not bool(success())
        except Exception:
            sdk_failed = True
    if code not in (None, 0) or sdk_failed:
        safe_code = code if isinstance(code, int) else "unknown"
        raise RuntimeError(f"Lark API returned an error (code={safe_code})")


def _validate_id(value: str, label: str) -> str:
    value = value.strip()
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _wiki_token(value: str) -> str:
    value = value.strip()
    if "://" in value:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("invalid Wiki URL")
        parts = [part for part in parsed.path.split("/") if part]
        try:
            marker = parts.index("wiki")
            value = parts[marker + 1]
        except (ValueError, IndexError):
            raise ValueError("invalid Wiki URL") from None
    return _validate_id(value.split("?", 1)[0].split("#", 1)[0].strip("/"), "Wiki token")


def _is_wiki_url(value: str) -> bool:
    if "://" not in value:
        return False
    return "/wiki/" in urlparse(value).path


class LarkDataClient:
    """Async, bounded, read-only client for Sheets, Base, Wiki, and Lingo."""

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
        self.client = client if client is not None else self._build_client()
        auth = getattr(
            getattr(getattr(self.client, "auth", None), "v3", None), "tenant_access_token", None
        )
        if (
            not callable(getattr(auth, "ainternal", None))
            or getattr(getattr(self.client, "bitable", None), "v1", None) is None
        ):
            raise TypeError(
                "LarkDataClient requires a lark_oapi.Client with async tenant authentication "
                "and Base APIs; a messaging channel client is not compatible"
            )
        config = getattr(self.client, "_config", None)
        if config is not None and not getattr(config, "enable_set_token", False):
            raise TypeError("Injected lark_oapi.Client must use .enable_set_token(True)")
        self.http_client = http_client
        self.max_rows = max_rows or int(getattr(settings, "lark_data_max_rows", 100))
        self.max_cells = max_cells or int(getattr(settings, "lark_data_max_cells", 2000))
        self.max_chars = int(getattr(settings, "lark_data_max_chars", 12000))
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    def _build_client(self) -> Any:
        from lark_oapi import Client  # type: ignore[import-untyped]

        return (
            Client.builder()
            .app_id(self.settings.feishu_app_id)
            .app_secret(self.settings.feishu_app_secret.get_secret_value())
            .domain(str(self.settings.lark_api_base_url).rstrip("/"))
            .timeout(float(self.settings.lark_api_timeout))
            .enable_set_token(True)
            .build()
        )

    async def _access_token(self) -> str:
        from lark_oapi.api.auth.v3.model.internal_tenant_access_token_request import (  # type: ignore[import-untyped]
            InternalTenantAccessTokenRequest,
        )
        from lark_oapi.api.auth.v3.model.internal_tenant_access_token_request_body import (  # type: ignore[import-untyped]
            InternalTenantAccessTokenRequestBody,
        )

        async with self._token_lock:
            now = time.monotonic()
            if self._token and now < self._token_expires_at:
                return self._token
            body = (
                InternalTenantAccessTokenRequestBody.builder()
                .app_id(self.settings.feishu_app_id)
                .app_secret(self.settings.feishu_app_secret.get_secret_value())
                .build()
            )
            result = await self.client.auth.v3.tenant_access_token.ainternal(
                InternalTenantAccessTokenRequest.builder().request_body(body).build()
            )
            code = _field(result, "code")
            if code not in (None, 0):
                safe_code = code if isinstance(code, int) else "unknown"
                raise RuntimeError(f"Lark tenant authentication failed (code={safe_code})")
            payload = _field(result, "data") or result
            token = _field(payload, "tenant_access_token")
            expire = _field(payload, "expire")
            # Some SDK versions discard top-level token fields in their generated
            # response model.  The actual tenant-token endpoint returns them there.
            if not token:
                raw = _field(_field(result, "raw"), "content")
                if raw:
                    try:
                        payload = json.loads(raw)
                    except (ValueError, TypeError):
                        raise RuntimeError("Lark tenant token response was invalid") from None
                    token = _field(payload, "tenant_access_token")
                    expire = _field(payload, "expire")
            if not isinstance(token, str) or not token:
                raise RuntimeError("Lark tenant token response was empty")
            try:
                ttl = float(expire)
            except (TypeError, ValueError):
                raise RuntimeError("Lark tenant token response has invalid expiry") from None
            if not math.isfinite(ttl) or ttl <= 0:
                raise RuntimeError("Lark tenant token response has invalid expiry")
            self._token = token
            self._token_expires_at = now + ttl - min(60.0, ttl / 2)
            return self._token

    async def _request_options(self) -> Any:
        from lark_oapi import RequestOption

        # Passing the token explicitly avoids the SDK's blocking auto-auth path.
        return RequestOption.builder().tenant_access_token(await self._access_token()).build()

    async def read_sheet_values(
        self, spreadsheet_url_or_token: str, cell_range: str
    ) -> SheetReadResult:
        token = await self._resource_object_token(spreadsheet_url_or_token, "sheet")
        return await self._read_sheet_values(token, cell_range)

    async def _read_sheet_values(self, token: str, cell_range: str) -> SheetReadResult:
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
        app = await self._resource_object_token(base_url_or_token, "bitable")
        page_size = min(int(getattr(self.settings, "lark_base_page_size", 50)), self.max_rows)
        from lark_oapi.api.bitable.v1 import ListAppTableRequest  # type: ignore[import-untyped]

        request = ListAppTableRequest.builder().app_token(app).page_size(page_size).build()
        response = await self.client.bitable.v1.app_table.alist(
            request, await self._request_options()
        )
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
        app = await self._resource_object_token(base_url_or_token, "bitable")
        table = validate_table_id(table_id)
        size = min(
            page_size or int(getattr(self.settings, "lark_base_page_size", 50)), self.max_rows
        )
        if keyword is None:
            from lark_oapi.api.bitable.v1 import ListAppTableRecordRequest

            builder = (
                ListAppTableRecordRequest.builder().app_token(app).table_id(table).page_size(size)
            )
            request = builder.page_token(page_token).build() if page_token else builder.build()
            response = await self.client.bitable.v1.app_table_record.alist(
                request, await self._request_options()
            )
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
                response = await self.client.bitable.v1.app_table_record.asearch(
                    builder.build(), await self._request_options()
                )
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
                response = await self.client.bitable.v1.app_table_record.alist(
                    list_request, await self._request_options()
                )
                data = _field(response, "data", response)
                items = _field(data, "items", []) or []
                matched = [item for item in items if keyword.lower() in str(_plain(item)).lower()]
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

    async def _resource_object_token(self, value: str, expected: str) -> str:
        if not _is_wiki_url(value):
            return parse_sheet_token(value) if expected == "sheet" else parse_base_token(value)
        node = await self.resolve_wiki_node(value)
        obj_type = str(node.get("obj_type") or "").lower()
        accepted = {"sheet"} if expected == "sheet" else {"bitable", "base"}
        if obj_type not in accepted:
            label = "sheet" if expected == "sheet" else "Base"
            raise ValueError(f"Wiki node does not contain a {label}")
        token = node.get("obj_token")
        if not isinstance(token, str):
            raise RuntimeError("Lark Wiki response did not include an object token")
        return parse_sheet_token(token) if expected == "sheet" else parse_base_token(token)

    def _page_size(self, page_size: int | None, *, api_max: int) -> int:
        if page_size is not None and page_size <= 0:
            raise ValueError("page_size must be positive")
        return min(
            page_size or int(getattr(self.settings, "lark_base_page_size", 50)),
            self.max_rows,
            api_max,
        )

    @staticmethod
    def _page_token(page_token: str | None) -> str | None:
        if page_token is None:
            return None
        if not page_token or len(page_token) > 1024 or any(ord(char) < 32 for char in page_token):
            raise ValueError("invalid page token")
        return page_token

    def _bounded_items(self, items: list[Any]) -> tuple[list[dict[str, Any]], bool]:
        truncated = len(items) > self.max_rows
        remaining = self.max_chars

        def bound(value: Any) -> Any:
            nonlocal remaining, truncated
            value = _plain(value)
            if isinstance(value, str):
                if len(value) > remaining:
                    truncated = True
                    value = value[: max(remaining, 0)]
                remaining -= len(value)
                return value
            if isinstance(value, list):
                return [bound(item) for item in value]
            if isinstance(value, dict):
                identifier_keys = {
                    "id",
                    "repo_id",
                    "entity_id",
                    "node_token",
                    "obj_token",
                    "obj_type",
                    "space_id",
                    "father_id",
                    "record_id",
                    "table_id",
                }
                result: dict[str, Any] = {}
                for key, item in value.items():
                    result[key] = (
                        item
                        if key in identifier_keys and isinstance(item, str)
                        else bound(item)
                    )
                return result
            return value

        bounded = [bound(item) for item in items[: self.max_rows]]
        return [item if isinstance(item, dict) else {"value": item} for item in bounded], truncated

    async def resolve_wiki_node(self, url_or_token: str) -> dict[str, Any]:
        """Resolve a Wiki node to its underlying Docx, Sheet, or Base object."""
        from lark_oapi.api.wiki.v2 import GetNodeSpaceRequest  # type: ignore[import-untyped]

        request = GetNodeSpaceRequest.builder().token(_wiki_token(url_or_token)).build()
        response = await self.client.wiki.v2.space.aget_node(
            request, await self._request_options()
        )
        _ensure_sdk_success(response)
        node = _field(_field(response, "data", response), "node")
        plain = _plain(node)
        if not isinstance(plain, dict) or not plain.get("obj_type") or not plain.get("obj_token"):
            raise RuntimeError("Lark Wiki response was incomplete")
        bounded, _ = self._bounded_items([plain])
        return bounded[0]

    async def list_wiki_nodes(
        self,
        space_id: str,
        *,
        parent_node_token: str | None = None,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> WikiNodeListResult:
        """List one bounded page of nodes from a Wiki space."""
        from lark_oapi.api.wiki.v2 import ListSpaceNodeRequest

        builder = (
            ListSpaceNodeRequest.builder()
            .space_id(_validate_id(space_id, "Wiki space_id"))
            .page_size(self._page_size(page_size, api_max=50))
        )
        if parent_node_token:
            builder = builder.parent_node_token(
                _validate_id(parent_node_token, "parent node token")
            )
        if token := self._page_token(page_token):
            builder = builder.page_token(token)
        response = await self.client.wiki.v2.space_node.alist(
            builder.build(), await self._request_options()
        )
        _ensure_sdk_success(response)
        data = _field(response, "data", response)
        items = list(_field(data, "items", []) or [])
        nodes, truncated = self._bounded_items(items)
        return WikiNodeListResult(
            nodes,
            _field(data, "page_token"),
            bool(_field(data, "has_more", False)),
            truncated,
        )

    async def read_wiki(
        self,
        url_or_token: str,
        *,
        cell_range: str | None = None,
        table_id: str | None = None,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> WikiReadResult:
        """Read a Wiki-backed Docx, Sheet range, or Base table page."""
        node = await self.resolve_wiki_node(url_or_token)
        obj_type = str(node.get("obj_type") or "").lower()
        token = node.get("obj_token")
        if not isinstance(token, str):
            raise RuntimeError("Lark Wiki response did not include an object token")
        if obj_type == "docx":
            from lark_oapi.api.docx.v1 import RawContentDocumentRequest  # type: ignore[import-untyped]

            request = (
                RawContentDocumentRequest.builder()
                .document_id(_validate_id(token, "document token"))
                .build()
            )
            response = await self.client.docx.v1.document.araw_content(
                request, await self._request_options()
            )
            _ensure_sdk_success(response)
            content = _field(_field(response, "data", response), "content", "") or ""
            if not isinstance(content, str):
                raise RuntimeError("Lark Docx response was invalid")
            truncated = len(content) > self.max_chars
            return WikiReadResult(node=node, content=content[: self.max_chars], truncated=truncated)
        if obj_type == "sheet":
            if cell_range is None:
                raise ValueError("cell_range is required for a Wiki Sheet")
            sheet_result = await self._read_sheet_values(parse_sheet_token(token), cell_range)
            return WikiReadResult(
                node=node,
                values=sheet_result.values,
                truncated=sheet_result.truncated,
            )
        if obj_type in {"bitable", "base"}:
            if table_id is None:
                raise ValueError("table_id is required for a Wiki Base")
            base_result = await self._base_records(
                token,
                table_id,
                page_token=page_token,
                page_size=page_size,
            )
            return WikiReadResult(
                node=node,
                records=base_result.records,
                truncated=base_result.truncated,
                page_token=base_result.page_token,
                has_more=base_result.has_more,
            )
        raise ValueError(f"unsupported Wiki object type: {obj_type or 'unknown'}")

    async def list_lingo_repos(
        self, *, page_token: str | None = None, page_size: int | None = None
    ) -> LingoListResult:
        """List Lingo repositories visible to the application."""
        from lark_oapi.api.lingo.v1 import ListRepoRequest  # type: ignore[import-untyped]

        if page_token is not None:
            raise ValueError("Lingo repository listing does not support page_token")
        if page_size is not None and page_size <= 0:
            raise ValueError("page_size must be positive")
        response = await self.client.lingo.v1.repo.alist(
            ListRepoRequest.builder().build(), await self._request_options()
        )
        _ensure_sdk_success(response)
        items = list(_field(_field(response, "data", response), "items", []) or [])
        requested_limit = min(page_size or self.max_rows, self.max_rows)
        page_truncated = len(items) > requested_limit
        items = items[:requested_limit]
        bounded, truncated = self._bounded_items(items)
        return LingoListResult(bounded, truncated=truncated or page_truncated)

    @staticmethod
    def _repo_id(repo_id: str | int) -> int:
        value = str(repo_id).strip()
        if not value.isdigit() or int(value) <= 0:
            raise ValueError("invalid Lingo repo_id")
        return int(value)

    async def list_lingo_classifications(
        self,
        repo_id: str | int,
        *,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> LingoListResult:
        """List one bounded page of Lingo classifications."""
        from lark_oapi.api.lingo.v1 import ListClassificationRequest

        builder = (
            ListClassificationRequest.builder()
            .repo_id(self._repo_id(repo_id))
            .page_size(self._page_size(page_size, api_max=500))
        )
        if token := self._page_token(page_token):
            builder = builder.page_token(token)
        response = await self.client.lingo.v1.classification.alist(
            builder.build(), await self._request_options()
        )
        return self._lingo_result(response, "items")

    async def list_lingo_entities(
        self,
        repo_id: str | int | None = None,
        *,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> LingoListResult:
        """List one bounded page of visible Lingo entities."""
        from lark_oapi.api.lingo.v1 import ListEntityRequest

        builder = ListEntityRequest.builder().page_size(
            self._page_size(page_size, api_max=100)
        )
        if repo_id is not None:
            builder = builder.repo_id(self._repo_id(repo_id))
        if token := self._page_token(page_token):
            builder = builder.page_token(token)
        response = await self.client.lingo.v1.entity.alist(
            builder.build(), await self._request_options()
        )
        return self._lingo_result(response, "entities")

    async def get_lingo_entity(self, entity_id: str) -> dict[str, Any]:
        """Get an entity by ID, adding ``truncated=True`` if its text was capped."""
        from lark_oapi.api.lingo.v1 import GetEntityRequest

        request = GetEntityRequest.builder().entity_id(_validate_id(entity_id, "entity_id")).build()
        response = await self.client.lingo.v1.entity.aget(
            request, await self._request_options()
        )
        _ensure_sdk_success(response)
        entity = _plain(_field(_field(response, "data", response), "entity"))
        if not isinstance(entity, dict) or not entity.get("id"):
            raise RuntimeError("Lark Lingo response was incomplete")
        bounded, truncated = self._bounded_items([entity])
        if truncated:
            bounded[0]["truncated"] = True
        return bounded[0]

    async def search_lingo_entities(
        self,
        keyword: str,
        *,
        repo_id: str | int | None = None,
        classification_ids: list[str] | None = None,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> LingoListResult:
        """Search Lingo entities with optional repository and category filters."""
        keyword = keyword.strip()
        if not keyword or len(keyword) > 500:
            raise ValueError("a bounded Lingo search keyword is required")
        return await self._search_lingo_entities(
            keyword,
            repo_id=repo_id,
            classification_ids=classification_ids,
            page_token=page_token,
            page_size=page_size,
        )

    async def _search_lingo_entities(
        self,
        keyword: str,
        *,
        repo_id: str | int | None,
        classification_ids: list[str] | None,
        page_token: str | None,
        page_size: int | None,
    ) -> LingoListResult:
        from lark_oapi.api.lingo.v1 import (
            ClassificationFilter,
            SearchEntityRequest,
            SearchEntityRequestBody,
        )

        body_builder = SearchEntityRequestBody.builder()
        if keyword:
            body_builder = body_builder.query(keyword)
        if classification_ids:
            ids = [_validate_id(item, "classification ID") for item in classification_ids]
            body_builder = body_builder.classification_filter(
                ClassificationFilter.builder().include(ids).build()
            )
        builder = (
            SearchEntityRequest.builder()
            .page_size(self._page_size(page_size, api_max=100))
            .request_body(body_builder.build())
        )
        if repo_id is not None:
            builder = builder.repo_id(self._repo_id(repo_id))
        if token := self._page_token(page_token):
            builder = builder.page_token(token)
        response = await self.client.lingo.v1.entity.asearch(
            builder.build(), await self._request_options()
        )
        return self._lingo_result(response, "entities")

    def _lingo_result(self, response: Any, field: str) -> LingoListResult:
        _ensure_sdk_success(response)
        data = _field(response, "data", response)
        items, truncated = self._bounded_items(list(_field(data, field, []) or []))
        return LingoListResult(
            items,
            _field(data, "page_token"),
            bool(_field(data, "has_more", False)),
            truncated,
        )

    @staticmethod
    def _parse_lingo_category(category_url: str) -> tuple[int, str]:
        parsed = urlparse(category_url.strip())
        if parsed.scheme not in {"http", "https"} or parsed.path.rstrip("/") != "/lingo/category":
            raise ValueError("a valid Lingo category URL is required")
        from urllib.parse import parse_qs

        query = parse_qs(parsed.query)
        unit = query.get("unit_id", [""])[0]
        primary = query.get("primary_class", [""])[0]
        return LarkDataClient._repo_id(unit), _validate_id(primary, "Lingo classification ID")

    async def read_lingo_category(
        self,
        category_url: str,
        *,
        keyword: str | None = None,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> LingoListResult:
        """Verify and read the repository/category encoded in a Lingo web URL."""
        repo_id, category_id = self._parse_lingo_category(category_url)
        repos = await self.list_lingo_repos()
        if not any(str(item.get("id")) == str(repo_id) for item in repos.items):
            if repos.truncated:
                raise ValueError("Lingo repository resolution is incomplete")
            raise ValueError("category unit_id is not an accessible Lingo repository")
        classifications = await self.list_lingo_classifications(repo_id, page_size=self.max_rows)
        if classifications.has_more or classifications.truncated:
            raise ValueError(
                "Lingo category resolution is incomplete; narrow or paginate classifications"
            )
        ids = {str(item.get("id")) for item in classifications.items}
        children = [
            str(item.get("id"))
            for item in classifications.items
            if str(item.get("father_id")) == category_id and item.get("id")
        ]
        if children:
            category_ids = children
        elif category_id in ids:
            category_ids = [category_id]
        else:
            raise ValueError("Lingo category is not available in the selected repository")
        search_keyword = "" if keyword is None else keyword.strip()
        if len(search_keyword) > 500:
            raise ValueError("Lingo search keyword is too long")
        return await self._search_lingo_entities(
            search_keyword,
            repo_id=repo_id,
            classification_ids=category_ids,
            page_token=page_token,
            page_size=page_size,
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


class LarkWikiTools(Toolkit):
    """Read-only tools for resolving and reading Lark/Feishu Wiki nodes."""

    def __init__(self, client: LarkDataClient, **kwargs: Any) -> None:
        self.client = client
        super().__init__(
            name="lark_wiki",
            async_tools=[
                (self.resolve_node, "resolve_wiki_node"),
                (self.list_nodes, "list_wiki_nodes"),
                (self.read, "read_wiki"),
            ],
            **kwargs,
        )

    async def resolve_node(self, url_or_token: str) -> dict[str, Any]:
        """Resolve a Wiki URL or node token to its safe node metadata."""
        return await self.client.resolve_wiki_node(url_or_token)

    async def list_nodes(
        self,
        space_id: str,
        parent_node_token: str | None = None,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        """List a bounded page of nodes in a Wiki space."""
        return asdict(
            await self.client.list_wiki_nodes(
                space_id,
                parent_node_token=parent_node_token,
                page_token=page_token,
                page_size=page_size,
            )
        )

    async def read(
        self,
        url_or_token: str,
        cell_range: str | None = None,
        table_id: str | None = None,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        """Read a Wiki Docx, a bounded Sheet range, or a Base table page."""
        return asdict(
            await self.client.read_wiki(
                url_or_token,
                cell_range=cell_range,
                table_id=table_id,
                page_token=page_token,
                page_size=page_size,
            )
        )


class LarkLingoTools(Toolkit):
    """Read-only tools for Lark/Feishu Lingo repositories and entities."""

    def __init__(self, client: LarkDataClient, **kwargs: Any) -> None:
        self.client = client
        super().__init__(
            name="lark_lingo",
            async_tools=[
                (self.list_repos, "list_lingo_repos"),
                (self.list_classifications, "list_lingo_classifications"),
                (self.list_entities, "list_lingo_entities"),
                (self.get_entity, "get_lingo_entity"),
                (self.search_entities, "search_lingo_entities"),
                (self.read_category, "read_lingo_category"),
            ],
            **kwargs,
        )

    async def list_repos(self, page_size: int | None = None) -> dict[str, Any]:
        """List Lingo repositories accessible to the application."""
        return asdict(await self.client.list_lingo_repos(page_size=page_size))

    async def list_classifications(
        self,
        repo_id: str,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        """List a bounded page of classifications for a Lingo repository."""
        return asdict(
            await self.client.list_lingo_classifications(
                repo_id, page_token=page_token, page_size=page_size
            )
        )

    async def list_entities(
        self,
        repo_id: str | None = None,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        """List a bounded page of visible Lingo entities."""
        return asdict(
            await self.client.list_lingo_entities(
                repo_id, page_token=page_token, page_size=page_size
            )
        )

    async def get_entity(self, entity_id: str) -> dict[str, Any]:
        """Read a Lingo entity by ID."""
        return await self.client.get_lingo_entity(entity_id)

    async def search_entities(
        self,
        keyword: str,
        repo_id: str | None = None,
        classification_ids: list[str] | None = None,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        """Search Lingo entities with optional repository and category filters."""
        return asdict(
            await self.client.search_lingo_entities(
                keyword,
                repo_id=repo_id,
                classification_ids=classification_ids,
                page_token=page_token,
                page_size=page_size,
            )
        )

    async def read_category(
        self,
        category_url: str,
        keyword: str | None = None,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        """Read verified search results for a Lingo category URL."""
        return asdict(
            await self.client.read_lingo_category(
                category_url,
                keyword=keyword,
                page_token=page_token,
                page_size=page_size,
            )
        )


class LarkDataTools(LarkSheetsTools, LarkBaseTools, LarkWikiTools, LarkLingoTools):
    def __init__(self, client: LarkDataClient, **kwargs: Any) -> None:
        Toolkit.__init__(
            self,
            name="lark_data",
            async_tools=[
                (self.read_sheet, "read_sheet"),
                (self.list_tables, "list_base_tables"),
                (self.list_records, "list_base_records"),
                (self.search_records, "search_base_records"),
                (self.resolve_node, "resolve_wiki_node"),
                (self.list_nodes, "list_wiki_nodes"),
                (self.read, "read_wiki"),
                (self.list_repos, "list_lingo_repos"),
                (self.list_classifications, "list_lingo_classifications"),
                (self.list_entities, "list_lingo_entities"),
                (self.get_entity, "get_lingo_entity"),
                (self.search_entities, "search_lingo_entities"),
                (self.read_category, "read_lingo_category"),
            ],
            **kwargs,
        )
        self.client = client


def build_lark_data_client(settings: Any, channel: Any | None = None) -> LarkDataClient:
    """Build a dedicated OpenAPI client; ``channel`` is kept for API compatibility."""
    return LarkDataClient(settings)


__all__ = [
    "LarkDataClient",
    "LarkSheetsTools",
    "LarkBaseTools",
    "LarkWikiTools",
    "LarkLingoTools",
    "LarkDataTools",
    "SheetReadResult",
    "BaseTableListResult",
    "BaseRecordListResult",
    "WikiReadResult",
    "WikiNodeListResult",
    "LingoListResult",
    "build_lark_data_client",
    "parse_sheet_token",
    "parse_base_token",
    "validate_cell_range",
    "validate_table_id",
]
