"""使用 DNS 固定连接的受控公网 GET 工具。"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from email.message import Message
from urllib.parse import urljoin, urlsplit, urlunsplit

from app.schemas import WebFetchArguments, WebFetchOutput
from tool.file_access import ToolAccessDenied, ToolExecutionError
from tool.policy import PolicyLoadError, load_tool_policy


_TEXT_TYPES = frozenset({"application/json", "application/xml", "application/xhtml+xml"})
_REDIRECTS = frozenset({301, 302, 303, 307, 308})


def _is_text_type(media_type: str) -> bool:
    return (
        media_type.startswith("text/")
        or media_type in _TEXT_TYPES
        or media_type.endswith("+json")
        or media_type.endswith("+xml")
    )


def _resolve_public_addresses(hostname: str, port: int) -> tuple[str, ...]:
    try:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise ToolExecutionError("域名解析失败。") from None
    addresses: list[str] = []
    for record in records:
        raw = record[4][0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            raise ToolAccessDenied("域名解析结果不安全。") from None
        if not address.is_global:
            raise ToolAccessDenied("拒绝访问本机、内网或保留网络地址。")
        normalized = address.compressed
        if normalized not in addresses:
            addresses.append(normalized)
    if not addresses:
        raise ToolExecutionError("域名没有可用的公网地址。")
    return tuple(addresses)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, pinned_ip: str, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, pinned_ip: str, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        raw = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _validated_target(url: str, *, allow_http: bool) -> tuple[str, str, int, str]:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ToolAccessDenied("URL 格式无效。") from None
    if port is not None and port < 1:
        raise ToolAccessDenied("URL 端口无效。")
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or (scheme == "http" and not allow_http):
        raise ToolAccessDenied("只允许策略启用的 HTTP/HTTPS 公网地址。")
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not url.isascii()
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in url)
    ):
        raise ToolAccessDenied("URL 不得包含凭据、片段或控制字符。")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        raise ToolAccessDenied("URL 不得直接使用 IP 地址。")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii")
    except UnicodeError:
        raise ToolAccessDenied("URL 主机名无效。") from None
    port = port or (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    canonical = urlunsplit((scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
    if len(canonical) > 4096:
        raise ToolAccessDenied("URL 过长。")
    return scheme, hostname, port, path


def _content_metadata(raw_content_type: str) -> tuple[str, str]:
    message = Message()
    message["content-type"] = raw_content_type
    media_type = message.get_content_type().casefold()
    charset = message.get_content_charset() or "utf-8"
    if not _is_text_type(media_type):
        raise ToolExecutionError("远程资源不是可解释的文本类型。")
    return media_type, charset


def web_fetch(arguments: WebFetchArguments) -> WebFetchOutput:
    try:
        policy = load_tool_policy().network
    except PolicyLoadError:
        raise ToolAccessDenied("网络工具策略不可用。") from None
    if not policy.fetch_enabled:
        raise ToolAccessDenied("公网读取工具已被策略禁用。")
    if arguments.max_bytes > policy.max_response_bytes:
        raise ToolAccessDenied("请求的响应上限超过策略。")
    if arguments.timeout_seconds > policy.max_timeout_seconds:
        raise ToolAccessDenied("请求的超时上限超过策略。")

    current_url = arguments.url
    for redirect_count in range(policy.max_redirects + 1):
        scheme, hostname, port, target = _validated_target(
            current_url, allow_http=policy.allow_http
        )
        pinned_ip = _resolve_public_addresses(hostname, port)[0]
        connection_type = _PinnedHTTPSConnection if scheme == "https" else _PinnedHTTPConnection
        connection = None
        try:
            connection = connection_type(
                hostname, port, pinned_ip, float(arguments.timeout_seconds)
            )
            connection.request(
                "GET",
                target,
                headers={
                    "Accept": "text/*, application/json, application/xml, application/*+json, application/*+xml",
                    "Accept-Encoding": "identity",
                    "User-Agent": "agnet-study-web-fetch/1",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            if response.status in _REDIRECTS:
                location = response.getheader("Location")
                if not location or redirect_count >= policy.max_redirects:
                    raise ToolExecutionError("远程资源重定向无效或超过上限。")
                next_url = urljoin(current_url, location)
                _validated_target(next_url, allow_http=policy.allow_http)
                current_url = next_url
                continue
            encoding = (response.getheader("Content-Encoding") or "identity").casefold()
            if encoding != "identity":
                raise ToolExecutionError("远程资源返回了不支持的压缩编码。")
            raw_type = response.getheader("Content-Type") or "text/plain; charset=utf-8"
            media_type, charset = _content_metadata(raw_type)
            body = response.read(arguments.max_bytes + 1)
            truncated = len(body) > arguments.max_bytes
            if truncated:
                body = body[: arguments.max_bytes]
            try:
                text = body.decode(charset, errors="strict")
            except (LookupError, UnicodeError):
                raise ToolExecutionError("远程文本无法按声明的字符集解码。") from None
            if len(text) > 4 * 1024 * 1024:
                raise ToolExecutionError("远程文本超过输出上限。")
            return WebFetchOutput(
                final_url=current_url,
                status=response.status,
                content_type=media_type,
                text=text,
                bytes_read=len(body),
                truncated=truncated,
                redirect_count=redirect_count,
            )
        except (ToolAccessDenied, ToolExecutionError):
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError):
            raise ToolExecutionError("公网读取失败或超时。") from None
        finally:
            if connection is not None:
                connection.close()
    raise ToolExecutionError("远程资源重定向超过上限。")
