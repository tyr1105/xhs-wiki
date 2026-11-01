# -*- coding: utf-8 -*-
"""engines/llmwiki/client.py —— LLM Wiki 本地 HTTP 接口的客户端。

这个文件做什么：封装对无窗口后台（backend.py 启动的进程）的 HTTP 访问。
本地接口的访问规则：

- 只连 127.0.0.1:<port>，Host 头因此固定是 127.0.0.1:<port>；
- 不带 Origin 头（后台会把任何浏览器来源的请求当成攻击拒掉）；
- 不走系统代理：urllib 默认会读 http_proxy 等环境变量，这里用空的
  ProxyHandler 显式关掉，否则代理软件会把本机回环请求也劫走；
- Authorization: Bearer <令牌>；令牌由调用方通过 token_provider 提供
  （backend.py 把令牌只放在内存里，每次请求现取）；
- POST 必须带 Idempotency-Key，格式 [A-Za-z0-9_-]{1,128}；
- 请求体不超过 2 MiB（后台的传输上限）；
- 非 2xx 的响应统一解析成 ApiError(status, code, retryable)。

在整体里的位置：backend.py 的健康检查、project.py 的打开项目、引擎
的导入/整理都通过这个客户端发请求。它是本包里唯一碰网络（且只碰
127.0.0.1）的模块。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

# 后台允许的幂等键格式：A-Za-z0-9_-，1–128 个字符。
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# 请求体上限：2 MiB，与后台的传输限制保持一致。
MAX_BODY_BYTES = 2 * 1024 * 1024

# 这些错误码表示"稍后重试有可能成功"（后台忙 / 重试回执缓存满）。
_RETRYABLE_CODES = {"API_BUSY", "API_RETRY_CACHE_FULL"}


class ApiError(Exception):
    """一次失败的后台请求。

    - status：HTTP 状态码；0 表示请求根本没到达后台（连接被拒绝、超时）；
    - code：后台错误码（如 API_RETRY_CACHE_FULL）；没到后台或后台没给
      错误体时是 "transport_error" / "http_<状态码>"；
    - retryable：调用方是否可以原样重试（429/5xx/408/传输错误为 True）。

    注意：异常文本里只有状态码和错误码，绝不含令牌或请求体内容。
    """

    def __init__(self, status: int, code: str, retryable: bool) -> None:
        self.status = int(status)
        self.code = str(code)
        self.retryable = bool(retryable)
        super().__init__(f"llmwiki_api_{self.status}_{self.code}")


def _is_retryable(status: int, code: str) -> bool:
    """判断一个失败是否值得原样重试。

    依据 local-api.md 和 retry.rs：429（API_BUSY / API_RETRY_CACHE_FULL）
    和 5xx 是暂时性的；409 幂等冲突、400 参数错误、401 未授权等重试
    也只会得到同样的结果。
    """

    if status in (408, 429) or status >= 500:
        return True
    return code in _RETRYABLE_CODES


class Client:
    """LLM Wiki 本地 API 客户端。

    token_provider 是"每次请求时现取令牌"的回调（无参数，返回字符串）。
    这样令牌始终只存在 backend.py 的内存里，客户端自己不保存副本，
    被意外打印（repr / 日志）也不会泄漏。
    """

    def __init__(self, port: int, token_provider) -> None:
        if not 1 <= int(port) <= 65535:
            raise ValueError("port_invalid")
        self.port = int(port)
        self._token_provider = token_provider
        # ProxyHandler({}) 显式禁用一切代理；urllib 默认会用环境变量里的
        # http_proxy/https_proxy，本机回环请求不该经过代理。
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def __repr__(self) -> str:  # 令牌不进 repr
        return f"Client(port={self.port})"

    def request(
        self,
        method: str,
        path: str,
        body=None,
        key: str | None = None,
        timeout: float = 90,
    ) -> tuple[int, dict]:
        """发一次请求，返回 (状态码, 解析后的 JSON)。

        - 2xx：正常返回；
        - 4xx/5xx：抛 ApiError；
        - 连不上 / 超时：抛 ApiError(0, "transport_error", True)；
        - POST 没带或不合法的幂等键、请求体超 2 MiB：抛 ValueError
          （这是调用方的编程错误，重试没有意义）。
        """

        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("api_path_invalid")
        method = method.upper()
        # 请求头只放三样：令牌、JSON 类型、（POST 时的）幂等键。
        # 故意不放 Origin——后台会拒绝任何带浏览器来源的请求；也不放
        # Host——urllib 会根据 URL 自动填 127.0.0.1:<port>，正好是后台
        # 期望的唯一 Host 权限。
        headers = {
            "Authorization": "Bearer " + str(self._token_provider()),
            "Content-Type": "application/json",
        }
        if method == "POST":
            if key is None or not _IDEMPOTENCY_KEY_RE.fullmatch(str(key)):
                raise ValueError("idempotency_key_invalid")
            headers["Idempotency-Key"] = str(key)
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            if len(payload) > MAX_BODY_BYTES:
                raise ValueError("body_too_large")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/v1" + path,
            data=payload,
            headers=headers,
            method=method,
        )
        try:
            response = self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            # HTTPError 本身就是响应对象，可以继续 read() 拿错误体。
            response = error
        except (urllib.error.URLError, TimeoutError, OSError):
            # 连接被拒绝（后台还没起来 / 已退出）、socket 超时等。
            raise ApiError(0, "transport_error", True) from None
        with response:
            raw = response.read()
            status = int(response.code)
        parsed = json.loads(raw) if raw else {}
        if status >= 400:
            error_body = parsed.get("error") if isinstance(parsed, dict) else None
            code = (
                error_body.get("code")
                if isinstance(error_body, dict) and error_body.get("code")
                else f"http_{status}"
            )
            raise ApiError(status, code, _is_retryable(status, code))
        return status, parsed
