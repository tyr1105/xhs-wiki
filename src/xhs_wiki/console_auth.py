# -*- coding: utf-8 -*-
"""console_auth.py —— 控制台的访问令牌、跳转页和"打开控制台"流程。

这个文件做什么：控制台（daemon 的 HTTP 服务）不再"裸奔"。本机其他
macOS 账户和沙盒 App 都能连上 127.0.0.1 的回环端口，所以控制台改用
一个只有当前用户能拿到的**访问令牌**做认证（照 Jupyter 的做法）：

1. ``ensure_token``：确保 ``APP_HOME/console-token`` 存在——32 字节随机
   数的 urlsafe 编码，权限 0600，重启**沿用**同一个令牌（浏览器里
   1 年有效期的 Cookie 不至于每重启一次服务就失效）。APP_HOME 本身是
   0700，同机其他账户读不到这个文件；
2. ``write_open_page``：生成 ``APP_HOME/open-console.html``（0600），
   页面里是一个自动提交的表单，用 POST 把一次性登录码（见下文）提交到
   ``http://127.0.0.1:<端口>/auth``。**凭据绝不能出现在 URL 或任何
   子进程参数里**——macOS 上其他账户能用 ps 看到任何进程的参数，
   ``open "http://…?t=<令牌>"`` 这种做法会泄露。放在 0600 的本地
   文件里再用 ``/usr/bin/open`` 打开这个文件，别的账户看不到内容；
3. ``open_console``：``xhs-wiki open`` 的完整流程——先确认端口上真的是
   **本机这份** xhs-wiki 服务（见下面"身份核对"），再写跳转页、打开它。

身份核对（为什么要有）：跳转页会把登录凭据交给"端口上正在监听的程序"。
如果服务没在运行、端口被同机其他账户的程序抢先占了，凭据就会被
送到对方手里。所以打开之前，先向 ``/healthz?challenge=<随机数>`` 发一个
随机挑战，服务端用令牌对它算一个 HMAC（一种"带密钥的指纹"）回给我们：
只有手里有同一个令牌的服务才算得出正确答案，而这个答案本身推不出
令牌。答案不对就拒绝打开，凭据一个字节都不会发出去。

另外两道保险：
- **答案里绑定端口**：以前答案只跟挑战值有关。占着目标端口的程序可以
  把挑战原样转发给在**另一个端口**上运行的真服务（/healthz 不需要
  任何凭据），拿到正确答案交回来，骗过核对。现在服务端用"自己实际
  监听的端口"参与计算，open 用"要打开的端口"计算——从别的端口转发
  来的答案对不上；
- **跳转页里不再放主令牌**：放的是一次性的登录码（HMAC(令牌, 端口 +
  随机数 + 过期时间)），2 分钟内有效、只能用一次、只在签发时的那个
  端口有效。跳转页文件以后被人（或从浏览器历史）再次打开，交出去的
  也只是一个作废的码；主令牌从不离开 0600 的令牌文件。

在整体里的位置：被 daemon（serve 启动时读令牌、校验 Cookie、回答挑战）
和 cli（``xhs-wiki open`` 子命令）调用。

术语说明（给非程序员）：
- "urlsafe 随机数"：``secrets.token_urlsafe`` 生成的只含字母、数字、
  ``-``、``_`` 的随机字符串，可以直接放进 HTML/表单里；
- "恒定时间比较"（``hmac.compare_digest``）：比较耗时和内容无关，
  攻击者无法靠"比较快慢"逐位猜出令牌；
- "SameSite=Strict"：浏览器只在"从本站自己发起"的请求里带这个 Cookie，
  从别的网站、本地文件跳过来的第一次请求不带（防跨站伪造请求）。
  这也是为什么 /auth 之后要经过一个中转页（见 ``AUTH_BOUNCE_PAGE``）。
"""

from __future__ import annotations

import hashlib
import hmac
import html
import http.client
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path

# 令牌文件名（APP_HOME 下，权限 0600）。
TOKEN_FILENAME = "console-token"
# 跳转页文件名（APP_HOME 下，权限 0600）。
OPEN_PAGE_FILENAME = "open-console.html"
# 访问 Cookie 名字的前缀；实际名字带上端口（见 cookie_name）。
COOKIE_NAME = "xhs_console"
# Cookie 有效期：1 年（秒）。令牌文件本身重启沿用，所以 Cookie 可以长寿。
COOKIE_MAX_AGE = 365 * 24 * 3600
# 令牌最短长度：token_urlsafe(32) 约 43 个字符；文件内容短于 32 视为损坏。
MIN_TOKEN_LENGTH = 32
# /healthz 挑战值的格式：32–128 位小写十六进制（xhs-wiki open 生成 64 位）。
CHALLENGE_RE = re.compile(r"[0-9a-f]{32,128}")
# 系统自带的 open 命令（系统命令一律写绝对路径，不按 PATH 查找）。
OPEN_BIN = "/usr/bin/open"
# 探活请求的超时秒数：服务就在本机，3 秒没回应就当没在运行。
PROBE_TIMEOUT_SECONDS = 3
# 一次性登录码的有效期（秒）。跳转页由 open 写好后立刻交给浏览器打开，
# 正常一两秒内就会提交；留 2 分钟余量给冷启动较慢的浏览器。
LOGIN_CODE_TTL_SECONDS = 120
# 登录码各字段的格式：随机数 32 位十六进制、过期时间是整数秒、码本身是
# HMAC-SHA256 的 64 位十六进制。格式不对的一律当成"不匹配"。
LOGIN_NONCE_RE = re.compile(r"[0-9a-f]{32}")
LOGIN_CODE_RE = re.compile(r"[0-9a-f]{64}")
LOGIN_EXPIRES_RE = re.compile(r"[0-9]{1,12}")

# POST /auth 成功后 303 跳到的中转页（GET /auth 返回它，不含任何数据）。
# 为什么需要中转：跳转页是本地文件，表单提交属于"跨站"请求；按浏览器的
# SameSite=Strict 规则，紧跟着的 303 跳转请求也算跨站，不会带上刚设好的
# Cookie——直接 303 到 / 的话，用户第一次打开看到的会是"请运行 xhs-wiki
# open"的提示页（已在 Chromium 里实测确认）。由本站自己的中转页再跳一次
# 就是"本站发起"的请求，Cookie 会正常带上。location.replace 不留历史
# 记录；meta refresh 和链接是关掉 JS 时的兜底。
AUTH_BOUNCE_PAGE = (
    "<!doctype html>\n"
    '<html lang="zh-CN">\n<head><meta charset="utf-8">\n'
    '<meta http-equiv="refresh" content="0;url=/">\n'
    "<title>xhs-wiki 控制台</title></head>\n"
    "<body>\n"
    '<p>正在进入控制台……如果没有自动跳转，请<a href="/">点这里</a>。</p>\n'
    "<script>location.replace('/');</script>\n"
    "</body>\n</html>\n"
).encode("utf-8")


def token_path(app_home: Path) -> Path:
    """令牌文件的路径（只算路径，不读不写）。"""
    return Path(app_home) / TOKEN_FILENAME


def cookie_name(port: int) -> str:
    """访问 Cookie 的名字，带上端口，例如 ``xhs_console_18765``。

    浏览器的 Cookie 只按主机名区分、**不区分端口**：临时用
    ``xhs-wiki serve --port 18767`` 再起一份服务时，两份服务的 Cookie
    会互相覆盖。名字里带端口，两份各用各的。
    """
    return f"{COOKIE_NAME}_{int(port)}"


def tokens_match(given, expected) -> bool:
    """恒定时间比较两个令牌（Cookie、表单字段、CSRF 头都用它）。

    先编码成字节再比较：``hmac.compare_digest`` 对含非 ASCII 字符的字符串
    会直接抛 TypeError——请求方随便塞一个中文字符就能让请求处理崩掉。
    任何一边为空或不是字符串都按"不匹配"处理。
    """
    if not isinstance(given, str) or not isinstance(expected, str):
        return False
    if not given or not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8", "surrogatepass"),
                               expected.encode("utf-8", "surrogatepass"))


def read_token(app_home: Path) -> "str | None":
    """只读不写地取令牌：文件不存在、读不了或内容明显不对时返回 None。

    ``xhs-wiki open`` 用它——服务启动时已经建好令牌，open 这边不应该
    凭空再造一个（造出来的和正在运行的服务对不上）。
    """
    try:
        token = token_path(app_home).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token if len(token) >= MIN_TOKEN_LENGTH else None


def ensure_token(app_home: Path) -> str:
    """返回控制台访问令牌；文件不存在时生成并写入（0600，原子写）。

    - 32 字节随机数的 urlsafe 编码（约 43 个字符）；
    - 文件已存在就直接读回来：服务重启、多次运行 ``xhs-wiki open``
      都用同一个令牌，浏览器里已登录的 Cookie 不会反复失效；
    - 写盘遵循项目规范：先写唯一命名的临时文件（0600）再
      ``os.replace`` 顶替，中途崩溃不会留下半个文件。
    """
    token = read_token(app_home)
    # 之前写入失败的空文件 / 内容明显不对（长度过短）都视为没有令牌。
    if token is not None:
        return token
    token = secrets.token_urlsafe(32)
    _write_private(Path(app_home), TOKEN_FILENAME, token + "\n")
    return token


def healthz_proof(token: str, challenge: str, port: int) -> str:
    """服务端对挑战值的回答：HMAC-SHA256(令牌, 固定前缀 + 端口 + 挑战值)。

    固定前缀（"xhs-wiki-healthz:"）把这个用途和别的用途隔开：就算有人
    反复拿不同挑战值来问，得到的答案也只能用来"证明身份"，既推不出
    令牌，也换不来 Cookie（Cookie 的值是令牌本身，不是这个答案）。

    端口参与计算。服务端填"自己实际监听的端口"，open 填
    "要打开的端口"。占着端口 A 的程序把挑战转发给端口 B 上的真服务，
    拿回来的是按 B 算的答案，open 按 A 核对必然对不上，于是拒绝打开。
    """
    message = f"xhs-wiki-healthz:{int(port)}:{challenge}".encode("utf-8")
    return hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def login_code(token: str, port: int, nonce: str, expires: int) -> str:
    """一次性登录码：HMAC-SHA256(令牌, 固定前缀 + 端口 + 随机数 + 过期时间)。

    只有持有令牌的人（open 命令、服务本身）算得出来；码本身推不出令牌。
    端口参与计算，所以发给端口 A 的码拿到端口 B 去用是无效的——就算被
    占着端口 A 的程序截到，也没法转交给真服务换 Cookie。
    """
    message = (f"xhs-wiki-login:{int(port)}:{nonce}:{int(expires)}"
               .encode("utf-8"))
    return hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def mint_login(token: str, port: int, *, now=None) -> dict:
    """签发一张一次性登录码，返回跳转页表单要提交的三个字段。

    ``now`` 是当前时间（秒，可注入测试）；open 命令和服务在同一台机器上，
    用同一个系统时钟，所以用墙上时间（time.time）判断过期即可。
    """
    current = int(time.time() if now is None else now)
    nonce = secrets.token_hex(16)
    expires = current + LOGIN_CODE_TTL_SECONDS
    return {"nonce": nonce, "expires": str(expires),
            "code": login_code(token, port, nonce, expires)}


class LoginCodeBook:
    """服务端的"已用登录码"登记簿：保证每张码只能换一次 Cookie。

    只记随机数（不记码本身），过期的条目随用随清，内存占用很小。服务
    重启后登记簿清空，但码 2 分钟就过期，重放窗口有上限；而且跳转页
    文件本身是 0600，别的账户读不到。多线程的 HTTP 服务会并发调用，
    用一把锁保护。
    """

    def __init__(self):
        self._used = {}            # 随机数 → 过期时间
        self._lock = threading.Lock()

    def redeem(self, token: str, port: int, fields: dict, *, now=None) -> bool:
        """核对并"用掉"一张登录码；合法且第一次使用才返回 True。

        fields 是表单里的 nonce / expires / code（字符串）。规则：格式
        合法、没过期、过期时间不离谱（不超过有效期 + 5 秒的时钟误差）、
        HMAC 对得上（恒定时间比较）、随机数没用过。
        """
        nonce = fields.get("nonce") or ""
        expires_text = fields.get("expires") or ""
        code = fields.get("code") or ""
        if not (LOGIN_NONCE_RE.fullmatch(nonce)
                and LOGIN_EXPIRES_RE.fullmatch(expires_text)
                and LOGIN_CODE_RE.fullmatch(code)):
            return False
        current = int(time.time() if now is None else now)
        expires = int(expires_text)
        if expires < current or expires > current + LOGIN_CODE_TTL_SECONDS + 5:
            return False
        if not tokens_match(code, login_code(token, port, nonce, expires)):
            return False
        with self._lock:
            for old_nonce, old_expires in list(self._used.items()):
                if old_expires < current:
                    del self._used[old_nonce]
            if nonce in self._used:
                return False
            self._used[nonce] = expires
        return True


def build_open_page(port: int, fields: dict) -> str:
    """构造跳转页的 HTML：一个自动提交的表单，POST 一次性登录码到 /auth。

    - 表单的 action 是绝对地址（``file://`` 打开的页面没有同源可言，
      必须写全 ``http://127.0.0.1:<端口>/auth``）；
    - ``<script>`` 立即提交；JS 被禁用时显示一个手动按钮，页面仍然
      可用；
    - 页面里只有端口和登录码三个字段（nonce / expires / code，见
      mint_login），**没有主令牌**；全部经过 HTML 转义（防万一文件被改坏）。
    """
    action = html.escape(f"http://127.0.0.1:{int(port)}/auth", quote=True)
    inputs = "".join(
        f'<input type="hidden" name="{name}" '
        f'value="{html.escape(str(fields[name]), quote=True)}">\n'
        for name in ("nonce", "expires", "code"))
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n<head><meta charset="utf-8">\n'
        '<meta name="referrer" content="no-referrer">\n'
        "<title>xhs-wiki 控制台</title></head>\n"
        "<body>\n"
        "<p>正在打开 xhs-wiki 控制台……</p>\n"
        f'<form id="f" method="POST" action="{action}">\n'
        f"{inputs}"
        '<noscript><button type="submit">点击打开控制台</button></noscript>\n'
        "</form>\n"
        "<script>document.getElementById('f').submit();</script>\n"
        "</body>\n</html>\n"
    )


def write_open_page(app_home: Path, port: int, fields: dict) -> Path:
    """把跳转页写到 ``APP_HOME/open-console.html``（0600，原子写）。

    文件里虽然只有一次性登录码，仍按私有文件处理：0600（APP_HOME 本身
    0700，双重保护）。返回文件路径，调用方再用 ``/usr/bin/open <文件>`` 打开。
    """
    return _write_private(Path(app_home), OPEN_PAGE_FILENAME,
                          build_open_page(port, fields))


def remove_open_page(app_home: Path) -> None:
    """登录成功后删掉跳转页：用过的页面没必要留着。

    文件不存在或删不掉都忽略——里面只是一张已经用掉的一次性登录码。
    """
    try:
        (Path(app_home) / OPEN_PAGE_FILENAME).unlink()
    except OSError:
        pass


def _write_private(directory: Path, filename: str, content: str) -> Path:
    """在 directory 下原子写一个 0600 的私有文件，返回它的路径。

    先 mkstemp（创建出来就是 0600、名字唯一）写完并 fsync，再
    os.replace 顶替正式文件；出错时删掉临时文件、原样抛出异常。
    """
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / filename
    descriptor, name = tempfile.mkstemp(prefix="." + filename + ".",
                                        dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


# ---------------------------------------------------------------------------
# xhs-wiki open 的完整流程
# ---------------------------------------------------------------------------

def probe_service(port: int, challenge: str) -> "dict | None":
    """向本机服务的 /healthz 发挑战，返回解析后的 JSON；没在运行返回 None。

    直接用 http.client 连 127.0.0.1：不经过 urllib，从根上避开用户设置的
    http_proxy 之类代理（探活请求被代理转走会误判）。
    请求里只有随机挑战值，不带令牌。
    """
    connection = http.client.HTTPConnection("127.0.0.1", int(port),
                                            timeout=PROBE_TIMEOUT_SECONDS)
    try:
        connection.request("GET", "/healthz?challenge=" + challenge)
        response = connection.getresponse()
        body = response.read(4096)
        if response.status != 200:
            return {}
        data = json.loads(body.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, http.client.HTTPException):
        # 连不上（ConnectionRefused）、超时：端口上没有服务在运行。
        return None
    except ValueError:
        # 回了非 JSON：端口上是别的程序。
        return {}
    finally:
        connection.close()


def _default_opener(args: list) -> int:
    """真实的打开动作：参数列表启动 /usr/bin/open，返回退出码。"""
    import subprocess
    result = subprocess.run(args, check=False, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    return result.returncode


def open_console(app_home: Path, port: int, *, opener=None, probe=None,
                 now=None) -> str:
    """``xhs-wiki open`` 的完整流程，返回结果代码（cli 按代码打印说明）。

    步骤：
    1. 生成随机挑战值，探活（probe 可注入）：没回应 → ``service_not_running``；
    2. 读令牌（只读不建）：没有 → ``token_missing``；
    3. 核对服务的回答（按**要打开的端口**算）：不对 →
       ``service_identity_mismatch``——端口上不是本机这份服务（或令牌已
       更换、或只是把挑战转发给了别的端口上的真服务），**不写跳转页、
       不打开**，什么凭据都不外流；
    4. 签发一张一次性登录码（mint_login，2 分钟有效、只能用一次、只认
       这个端口），写进 0600 的跳转页，用 ``["/usr/bin/open", <文件>]``
       打开：参数里只有文件路径，主令牌从不写进跳转页；
    5. open 退出码非 0 → ``open_failed``；成功 → ``opened``。

    opener(参数列表) -> 退出码；测试注入记录参数的假实现。
    """
    opener = opener or _default_opener
    probe = probe or probe_service
    challenge = secrets.token_hex(32)
    answer = probe(port, challenge)
    if answer is None:
        return "service_not_running"
    token = read_token(app_home)
    if token is None:
        return "token_missing"
    proof = answer.get("proof") if isinstance(answer, dict) else None
    if not answer.get("ok") or not tokens_match(
            proof, healthz_proof(token, challenge, port)):
        return "service_identity_mismatch"
    page = write_open_page(app_home, port, mint_login(token, port, now=now))
    try:
        code = opener([OPEN_BIN, str(page)])
    except OSError:
        return "open_failed"
    return "opened" if code == 0 else "open_failed"
