from __future__ import annotations

from dataclasses import dataclass
import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError

from app.observability import _input_summary, _output_summary
from app.schemas import (
    ToolMessage,
    WebSearchArguments,
    WebSearchOutput,
    WebSearchResult,
)
from tool.file_access import ToolAccessDenied, ToolExecutionError
from tool.policy import PolicyLoadError
from tool.registry import TOOLSCHEMAS, call_tool
from tool.web_search import load_brave_api_key, web_search
import tool.registry as registry


@dataclass
class FakeResponse:
    status: int = 200
    body: bytes = b"{}"
    content_type: str = "application/json; charset=utf-8"
    content_encoding: str = "identity"

    def getheader(self, name: str):
        if name == "Content-Type":
            return self.content_type
        if name == "Content-Encoding":
            return self.content_encoding
        return None

    def read(self, amount: int) -> bytes:
        return self.body[:amount]


class FakeConnection:
    response = FakeResponse()
    created: list["FakeConnection"] = []

    def __init__(self, host, port, pinned_ip, timeout):
        self.host = host
        self.port = port
        self.pinned_ip = pinned_ip
        self.timeout = timeout
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.closed = False
        self.created.append(self)

    def request(self, method, target, headers):
        self.requests.append((method, target, headers))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def _payload(results=None, *, more=False) -> bytes:
    return json.dumps({
        "query": {"more_results_available": more},
        "web": {
            "results": results if results is not None else [
                {"title": "Example", "url": "https://example.com/a", "description": "Snippet"}
            ],
        }
    }).encode("utf-8")


class WebSearchTests(unittest.TestCase):
    def setUp(self):
        FakeConnection.created = []
        FakeConnection.response = FakeResponse(body=_payload())

    def run_search(self, arguments=None):
        with (
            patch("tool.web_search.load_brave_api_key", return_value="super-secret-key"),
            patch("tool.web_search._resolve_public_addresses", return_value=("93.184.216.34",)),
            patch("tool.web_search._PinnedHTTPSConnection", FakeConnection),
        ):
            return web_search(arguments or WebSearchArguments(query="安全 搜索"))

    def test_success_uses_fixed_endpoint_encoded_query_pinned_ip_and_secret_header(self):
        output = self.run_search(WebSearchArguments(
            query="中文 test", count=3, freshness="pw", country="cn",
            search_lang="zh", safesearch="strict",
        ))
        connection = FakeConnection.created[0]
        self.assertEqual(
            (connection.host, connection.port, connection.pinned_ip),
            ("api.search.brave.com", 443, "93.184.216.34"),
        )
        method, target, headers = connection.requests[0]
        self.assertEqual(method, "GET")
        parsed = urlsplit(target)
        self.assertEqual(parsed.path, "/res/v1/web/search")
        self.assertEqual(parse_qs(parsed.query)["q"], ["中文 test"])
        self.assertEqual(parse_qs(parsed.query)["country"], ["CN"])
        self.assertEqual(headers["X-Subscription-Token"], "super-secret-key")
        self.assertEqual(headers["Accept-Encoding"], "identity")
        self.assertTrue(connection.closed)
        self.assertEqual(output.provider, "brave")
        self.assertIn("不可信外部数据", output.external_content_notice)
        self.assertNotIn("super-secret-key", output.model_dump_json())

    def test_schema_rejects_oversize_word_count_and_invalid_filters(self):
        invalid = (
            {"query": "x" * 601},
            {"query": " ".join(["x"] * 76)},
            {"query": "ok", "count": 11},
            {"query": "ok", "freshness": "today"},
            {"query": "ok", "country": "1N"},
            {"query": "ok", "search_lang": "zh_XX"},
            {"query": "ok\nsecret"},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                WebSearchArguments.model_validate(values)

    def test_key_loader_prefers_environment_and_sanitizes_missing_value(self):
        with (
            patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "from-environment"}, clear=True),
            patch("tool.web_search.dotenv_values", return_value={"BRAVE_SEARCH_API_KEY": "from-file"}) as dotenv,
        ):
            self.assertEqual(load_brave_api_key(), "from-environment")
            dotenv.assert_not_called()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("tool.web_search.dotenv_values", return_value={}),
        ):
            with self.assertRaises(ToolAccessDenied) as caught:
                load_brave_api_key()
        self.assertNotIn(".env", str(caught.exception))

    def test_response_bounds_json_type_auth_and_network_errors_are_sanitized(self):
        cases = (
            FakeResponse(status=401, body=b"secret body"),
            FakeResponse(status=429, body=b"secret body"),
            FakeResponse(body=b"not-json"),
            FakeResponse(body=b'{"query":{},"web":{"results":[]},"bad":NaN}'),
            FakeResponse(body=_payload(), content_type="text/plain"),
            FakeResponse(body=_payload(), content_encoding="gzip"),
        )
        for response in cases:
            FakeConnection.response = response
            with self.subTest(response=response), self.assertRaises((ToolAccessDenied, ToolExecutionError)) as caught:
                self.run_search()
            self.assertNotIn("secret", str(caught.exception))
        FakeConnection.response = FakeResponse(body=b"x" * (1024 * 1024 + 2))
        with self.assertRaises(ToolExecutionError):
            self.run_search()

    def test_malicious_result_fields_and_urls_are_rejected(self):
        bad_results = (
            [{"title": "bad\x00title", "url": "https://example.com", "description": "x"}],
            [{"title": "x", "url": "http://127.0.0.1/private", "description": "x"}],
            [{"title": "x", "url": "https://user:pass@example.com/a", "description": "x"}],
            [{"title": ["not text"], "url": "https://example.com", "description": "x"}],
        )
        for results in bad_results:
            FakeConnection.response = FakeResponse(body=_payload(results))
            with self.subTest(results=results), self.assertRaises(ToolExecutionError):
                self.run_search()

    def test_policy_failure_is_closed_before_key_or_network(self):
        with (
            patch("tool.web_search.load_tool_policy", side_effect=PolicyLoadError("x")),
            patch("tool.web_search.load_brave_api_key") as key,
            patch("tool.web_search._resolve_public_addresses") as resolve,
        ):
            with self.assertRaises(ToolAccessDenied):
                web_search(WebSearchArguments(query="test"))
        key.assert_not_called()
        resolve.assert_not_called()

    def test_mixed_public_private_dns_is_rejected_before_connect(self):
        public = (None, None, None, None, ("93.184.216.34", 443))
        private = (None, None, None, None, ("127.0.0.1", 443))
        with (
            patch("tool.web_search.load_brave_api_key", return_value="safe-key"),
            patch("socket.getaddrinfo", return_value=[public, private]),
            patch("tool.web_search._PinnedHTTPSConnection") as connection,
        ):
            with self.assertRaises(ToolAccessDenied):
                web_search(WebSearchArguments(query="test"))
        connection.assert_not_called()

    def test_registry_schema_correlation_and_observability_redact_query_and_key(self):
        schema = next(item for item in TOOLSCHEMAS if item["function"]["name"] == "web_search")
        self.assertEqual(schema["function"]["parameters"], WebSearchArguments.model_json_schema())
        arguments = WebSearchArguments(query="SENSITIVE QUERY", count=1)
        output = WebSearchOutput(
            results=[WebSearchResult(title="one", url="https://example.com", snippet="x")],
            more_results=False,
        )
        registry._validate_web_search_output(arguments, output)
        invalid = output.model_copy(update={"results": output.results * 2})
        with self.assertRaises(ValueError):
            registry._validate_web_search_output(arguments, invalid)
        summary = _input_summary("web_search", {
            "query": "SENSITIVE QUERY", "count": 1, "safesearch": "moderate",
        })
        serialized = json.dumps(summary)
        self.assertNotIn("SENSITIVE", serialized)
        self.assertNotIn("key", serialized.casefold())

        from app.schemas import ToolResult
        output_summary = _output_summary(ToolResult(status="success", output=output))
        self.assertEqual(output_summary["result_count"], 1)
        self.assertNotIn("https://", json.dumps(output_summary))
        self.assertNotIn("one", json.dumps(output_summary))

        with patch("tool.web_search.load_tool_policy", side_effect=PolicyLoadError("SENSITIVE")):
            raw = call_tool({"id": "s1", "name": "web_search", "arguments": {"query": "q"}})
        result = ToolMessage.model_validate_json(raw).result
        self.assertEqual(result.status, "denied")
        self.assertNotIn("SENSITIVE", result.error)


if __name__ == "__main__":
    unittest.main()
