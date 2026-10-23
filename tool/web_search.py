"""固定 Brave 端点、密钥隔离且 DNS 固定的只读网页搜索工具。"""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import ssl
from urllib.parse import urlencode

from dotenv import dotenv_values

from app.schemas import WebSearchArguments, WebSearchOutput, WebSearchResult
from tool.file_access import ToolAccessDenied, ToolExecutionError
from tool.policy import PolicyLoadError, load_tool_policy
from tool.web_fetch import _PinnedHTTPSConnection, _resolve_public_addresses


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"
_BRAVE_HOST = "api.search.brave.com"
_BRAVE_PORT = 443
_BRAVE_PATH = "/res/v1/web/search"


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-standard JSON number")


def load_brave_api_key() -> str:
    """系统环境优先；只读取单个密钥且不修改进程环境。"""
    try:
        if "BRAVE_SEARCH_API_KEY" in os.environ:
            candidate = os.environ.get("BRAVE_SEARCH_API_KEY")
        else:
            values = dotenv_values(_ENV_FILE, encoding="utf-8-sig", interpolate=False)
            candidate = values.get("BRAVE_SEARCH_API_KEY")
    except (OSError, UnicodeError, ValueError, TypeError):
        raise ToolAccessDenied("Brave Search API 密钥无法安全读取。") from None
    if (
        not isinstance(candidate, str)
        or not candidate.strip()
        or len(candidate) > 4096
        or any(character.isspace() or ord(character) < 33 or ord(character) == 127 for character in candidate)
    ):
        raise ToolAccessDenied("Brave Search API 密钥未配置或格式无效。")
    return candidate.strip()


def _parse_search_response(body: bytes, count: int) -> WebSearchOutput:
    try:
        payload = json.loads(
            body.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise ToolExecutionError("搜索服务返回了无效的 JSON。") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("web"), dict):
        raise ToolExecutionError("搜索服务返回的数据结构无效。")
    web = payload["web"]
    raw_results = web.get("results")
    query_metadata = payload.get("query", {})
    if not isinstance(query_metadata, dict):
        raise ToolExecutionError("搜索服务返回的数据结构无效。")
    reported_more = query_metadata.get("more_results_available", False)
    if not isinstance(raw_results, list) or type(reported_more) is not bool:
        raise ToolExecutionError("搜索服务返回的数据结构无效。")
    parsed_results: list[WebSearchResult] = []
    try:
        for item in raw_results[:count]:
            if not isinstance(item, dict):
                raise ValueError
            title = item.get("title")
            url = item.get("url")
            description = item.get("description", "")
            if not isinstance(title, str) or not isinstance(url, str) or not isinstance(description, str):
                raise ValueError
            parsed_results.append(
                WebSearchResult(title=title, url=url, snippet=description)
            )
    except (ValueError, TypeError):
        raise ToolExecutionError("搜索服务返回了不安全或无效的结果字段。") from None
    return WebSearchOutput(
        results=parsed_results,
        more_results=reported_more or len(raw_results) > count,
    )


def web_search(arguments: WebSearchArguments) -> WebSearchOutput:
    try:
        policy = load_tool_policy().network
    except PolicyLoadError:
        raise ToolAccessDenied("网络工具策略不可用。") from None
    if not policy.search_enabled:
        raise ToolAccessDenied("网页搜索工具已被策略禁用。")
    api_key = load_brave_api_key()
    query: dict[str, object] = {
        "q": arguments.query,
        "count": arguments.count,
        "safesearch": arguments.safesearch,
    }
    if arguments.freshness is not None:
        query["freshness"] = arguments.freshness
    if arguments.country is not None:
        query["country"] = arguments.country
    if arguments.search_lang is not None:
        query["search_lang"] = arguments.search_lang
    target = _BRAVE_PATH + "?" + urlencode(query)

    pinned_ip = _resolve_public_addresses(_BRAVE_HOST, _BRAVE_PORT)[0]
    connection = None
    try:
        connection = _PinnedHTTPSConnection(
            _BRAVE_HOST,
            _BRAVE_PORT,
            pinned_ip,
            float(policy.search_max_timeout_seconds),
        )
        connection.request(
            "GET",
            target,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "X-Subscription-Token": api_key,
                "User-Agent": "agnet-study-web-search/1",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        if response.status in {401, 403}:
            raise ToolAccessDenied("Brave Search 认证失败，请检查 API 密钥。")
        if response.status == 429:
            raise ToolExecutionError("Brave Search 当前请求受限，请稍后重试。")
        if response.status != 200:
            raise ToolExecutionError("Brave Search 请求失败。")
        if (response.getheader("Content-Encoding") or "identity").casefold() != "identity":
            raise ToolExecutionError("搜索服务返回了不支持的压缩编码。")
        content_type = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().casefold()
        if content_type != "application/json":
            raise ToolExecutionError("搜索服务没有返回 JSON。")
        body = response.read(policy.search_max_response_bytes + 1)
        if len(body) > policy.search_max_response_bytes:
            raise ToolExecutionError("搜索服务响应超过策略上限。")
        return _parse_search_response(body, arguments.count)
    except (ToolAccessDenied, ToolExecutionError):
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError):
        raise ToolExecutionError("网页搜索失败或超时。") from None
    finally:
        if connection is not None:
            connection.close()
