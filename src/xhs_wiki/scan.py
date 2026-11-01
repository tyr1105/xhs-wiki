"""扫描模块：用 Playwright 驱动本机 Google Chrome，识别主页、扫码登录、扫收藏列表。

在整个项目里的位置（architecture.md §3）：这是每次运行的"扫描"步骤。
它只"看"页面、收集带签名的收藏卡片链接，不做下载、不做反检测；
扫到的结果作为返回值交给调用方（runner 再交给台账 ledger），不写扫描文件。

页面选择器和判断逻辑已在真实网站上校准过。几处设计：
1. 不写死主页地址：detect_profile_url 从登录状态里自动识别；
2. 扫描结果直接作为返回值交给调用方，不写扫描文件；
3. 浏览器资料夹路径和二维码截图路径都由参数传入。

红线：不读取/导出 Cookie 和本地存储；不改浏览器指纹或 UA；遇到验证码立即返回
blocked，不尝试任何绕过；除 items 里的 signed_url 外，返回值和"日志"（repr）
都不带任何链接或签名参数。
"""
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl, urlencode

# ---------------- 页面选择器与提示文字（集中定义） ----------------

SELECTORS = {
    # 顶部标签栏里当前选中的标签；它的文字等于"收藏"才说明收藏页已选中。
    'fav_tab': '.xhs-user-page-primary-tabs .active',
    # 收藏列表容器：多个 tab-content-item 里取不带 static-layout 的那一个。
    'card_container': '.tab-content-item:not(.static-layout)',
    # 单张收藏卡片。
    'card': '.note-item',
    # 卡片标题（取不到时退回整卡文字）。
    'title': '.title',
    # 风控/验证容器：页面上一旦出现它，说明被小红书拦下了。
    'verify_box': '.fe-verify-box',
    # 登录界面容器（未登录时页面上可见的登录弹窗）。
    'login_ui': '.login-container',
    # 扫码登录的二维码元素：.login-container 里 128×128 的 img 标签。
    # 直接截这张 img 本身，配合就绪检查和白图检测，能拿到清晰的二维码。
    'qr': '.login-container .qrcode-img',
    # 已登录的"肯定信号"：侧栏频道列表里指向自己主页、文字为"我"的链接。
    'me_link': '.channel-list a.link-wrapper[href^="/user/profile/"]',
    # 未登录的"肯定信号"：侧栏的"登录"按钮。小红书未登录时会先渲染出收藏标签
    # 和几张不带签名的预览卡片，过几秒才弹登录框——光看弹窗会把"未登录"
    # 误判成"扫描成功、0 条"。
    'login_btn': '.side-bar-component.login-btn',
}

MARKERS = [
    # 正文出现这些文字说明小红书在要求安全验证或拒绝访问 → 返回 blocked，立即停下（不绕过）。
    # 注意：不能放"验证码"——未登录页的手机号登录弹窗里本来就有"获取验证码"按钮，
    # 放了会把正常登录页误判成被拦截。
    '安全限制',
    'IP存在风险',
    '安全验证',
    '请完成验证',
    '访问异常',
]

# 在页面里执行的 JS：只返回当前可见卡片的"原始数据"[{hrefs:[…], title}]。
# 故意不做任何筛选——挑带签名链接的逻辑放在 Python 的 normalize_cards 里，方便单元测试。
COLLECT_JS = """
(() => {
  const container = Array.from(document.querySelectorAll('.tab-content-item'))
    .find(element => !element.classList.contains('static-layout'));
  if (!container) return [];
  const cards = [];
  for (const card of container.querySelectorAll('.note-item')) {
    cards.push({
      hrefs: Array.from(card.querySelectorAll('a[href]')).map(a => a.href),
      title: card.querySelector('.title')?.textContent?.trim() || card.textContent?.trim() || '',
    });
  }
  return cards;
})()
"""

# 滚动到页面底部，触发下一批收藏加载。
# 用 document.scrollingElement 而不是 document.body：这个页面的滚动元素是前者，
# body 的高度不一定会跟着变。
SCROLL_JS = 'window.scrollTo(0, document.scrollingElement.scrollHeight)'

# 卡住时"往回拉一下"的滚动：先向上滚 1200 像素、稍等片刻再滚到底，
# 模拟正常人"往回翻一下再往下翻"，能重新触发页面的滚动加载。
# 注意这不是反检测（不改 UA、不注入脚本），只是普通的窗口滚动操作。
SCROLL_BACK_JS = 'window.scrollBy(0, -1200)'

# 读页面高度，用来判断"滚了但页面没变长"（说明到底了）。同样用 scrollingElement。
HEIGHT_JS = 'document.scrollingElement.scrollHeight'

# 数一数收藏列表里当前渲染了多少张卡片（不管带不带签名）。
# 用途：开始滚动前先等列表"稳定"——页面刚打开时前端还在初始化，
# 如果只有首屏 10 张就开始滚到底，"加载更多"的请求可能发不出去，
# 之后再怎么滚页面高度也不变（偶发的"卡 10 条"问题）。
CARD_COUNT_JS = """
(() => {
  const container = Array.from(document.querySelectorAll('.tab-content-item'))
    .find(element => !element.classList.contains('static-layout'));
  return container ? container.querySelectorAll('.note-item').length : 0;
})()
"""

# 读正文文字，用来匹配 MARKERS（安全验证/访问异常等提示）。
TEXT_JS = 'document.body.innerText'

# 读登录弹窗里的文字，用来发现"二维码已过期/已失效"。
LOGIN_TEXT_JS = "document.querySelector('.login-container')?.textContent || ''"

# 读标称总数（如"笔记・1495"）：在收藏页的二级标签 .reds-tab-item 里找
# 文字恰好是"笔记・<数字>"的那一个，返回整段"笔记・1495"（交给 Python 的
# parse_nominal 解析；只返回数字会让 parse_nominal 认不出来、总数恒为空）。
NOMINAL_JS = """
(() => {
  for (const tab of document.querySelectorAll('.reds-tab-item')) {
    const match = (tab.textContent || '').trim().match(/^笔记・(\\d+)$/);
    if (match) return match[0];
  }
  return '';
})()
"""

# 读侧栏"我"链接的 href：登录成功后用它自动识别用户的主页地址。
# 返回相对链接（如 /user/profile/<24位十六进制>），交给 normalize_profile_url 规范。
ME_HREF_JS = ('document.querySelector(\'.channel-list a.link-wrapper'
              '[href^="/user/profile/"]\')?.getAttribute("href") || ""')

# 检查二维码 img 是否已经加载完：complete 为真且 naturalWidth 大于 0，
# 说明 data:image/png;base64 的图片真的解码出来了（而不是还在画或画失败）。
# 没就绪就这一轮先不截图，下一轮再看。
QR_READY_JS = """
(() => {
  const img = document.querySelector('.login-container .qrcode-img');
  return !!(img && img.complete && img.naturalWidth > 0);
})()
"""

# 判断截图是不是"白图"（还没画出二维码的空白弹窗）的门槛：
# 统计整张图里"深色像素"（灰度 < 128）的占比，低于 5% 就算白图。
# 二维码的深色像素通常占 30%–60%，所以清晰的码一定远高于这个门槛。
QR_DARK_RATIO_MIN = 0.05

# 打开小红书首页（登录/探测都用它）：二维码过期就整页刷新换新码。
EXPLORE_URL = 'https://www.xiaohongshu.com/explore'


# 判断某个选择器在页面上是否存在（返回布尔值）。
def _exists_js(selector):
    return f'!!document.querySelector({json.dumps(selector)})'


# 选中元素的文字（用来确认选中的标签是不是"收藏"、侧栏链接是不是"我"）。
# 选择器用 json.dumps 拼进去，和 _exists_js 一致：自动加引号、转义特殊字符。
_TAB_TEXT_JS = "document.querySelector({0})?.textContent?.trim() || ''"

# ---------------- 纯函数（全部有单元测试） ----------------

# 笔记编号在路径末尾：24 位十六进制（如 /explore/<id>）。
_NOTE_PATH_RE = re.compile(r'/([0-9a-f]{24})$')

# 主页编号在路径里：/user/profile/<24 位十六进制>（后面可以还带查询参数）。
_PROFILE_PATH_RE = re.compile(r'/user/profile/([0-9a-f]{24})')


def looks_blank(png_path):
    """判断一张 PNG 截图是不是"白图"（弹窗里二维码还没画出来的空白图）。

    做法：解码 PNG 得到灰度直方图（每个亮度值 0–255 的像素个数），统计亮度
    < 128 的"深色像素"占比；低于 QR_DARK_RATIO_MIN（5%）就算白图。
    二维码的深色像素通常占 30%–60%，清晰的码不会误判；而空白弹窗几乎
    全是浅色背景，占比接近 0%。

    本项目的依赖里没有 Pillow，也不为这一个用途新增依赖，所以
    _png_luminance_histogram 用标准库 zlib 手写了一个只支持"8 位、
    非隔行、灰度/真彩"的最小解码器——Playwright 截图正好都是这种格式。

    打不开图片（文件损坏 / 不是 PNG / 已被删）也算白图：调用方
    （login_flow）会丢掉这张截图、下一轮重截，宁可多截一次也不把坏图
    当成可用的二维码显示给用户扫。
    """
    try:
        histogram = _png_luminance_histogram(png_path)
    except Exception:
        return True
    dark = sum(histogram[:128])   # 亮度 0–127 的都是深色像素
    total = sum(histogram)
    # total 为 0（理论上不会发生，防御一下）也按白图处理。
    return total == 0 or dark / total < QR_DARK_RATIO_MIN


def _png_luminance_histogram(png_path):
    """用标准库解码一张 PNG，返回长度 256 的亮度直方图（供 looks_blank 用）。

    只支持最常见的子集：8 位/样本、非隔行（interlace=0）、颜色类型为
    0（灰度）、2（RGB）、4（灰度+alpha）、6（RGBA）——Chrome 的
    element.screenshot 输出的就是 RGBA PNG，测试里构造的也是这一类。

    PNG 结构（为什么这样读）：文件头 8 字节签名，之后是一连串"长度 +
    类型 + 数据 + CRC"的块；像素数据在 IDAT 块里（zlib 压缩），每行
    像素前有 1 字节"过滤器类型"，要先按 PNG 规则反过滤还原原始像素。
    """
    import struct
    import zlib
    data = Path(png_path).read_bytes()
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('not a png')
    width = height = None
    bit_depth = color_type = interlace = None
    idat = bytearray()
    pos = 8
    while pos + 8 <= len(data):
        length, ctype = struct.unpack('>I4s', data[pos:pos + 8])
        pos += 8
        chunk = data[pos:pos + length]
        pos += length + 4   # 跳过数据 + 4 字节 CRC
        if ctype == b'IHDR':
            width, height, bit_depth, color_type, _, _, interlace = \
                struct.unpack('>IIBBBBB', chunk[:13])
        elif ctype == b'IDAT':
            idat += chunk
        elif ctype == b'IEND':
            break
    # 只支持上述子集；遇到别的（调色板、16 位、隔行）就抛错，调用方按白图处理。
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
    if (width in (None, 0) or height in (None, 0) or channels is None
            or bit_depth != 8 or interlace != 0):
        raise ValueError('unsupported png variant')
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    if len(raw) < (stride + 1) * height:
        raise ValueError('png data truncated')
    histogram = [0] * 256
    # paeth_predictor：PNG 的"Paeth"反过滤要用到的邻近像素预测（白话：猜当前
    # 像素最可能的值，再拿滤波残差加回去）。仅过滤器 4 用到。
    def paeth(a, b, c):
        p = a + b - c
        pa = abs(p - a)
        pb = abs(p - b)
        pc = abs(p - c)
        if pa <= pb and pa <= pc:
            return a
        if pb <= pc:
            return b
        return c
    prev = bytearray(stride)
    offset = 0
    for _y in range(height):
        ftype = raw[offset]
        offset += 1
        line = bytearray(raw[offset:offset + stride])
        offset += stride
        # 按过滤器类型逐行还原像素（标准规定按字节递推，x 从 1 个样本起有左邻）。
        bpp = channels
        for x in range(stride):
            left = line[x - bpp] if x >= bpp else 0
            up = prev[x]
            upleft = prev[x - bpp] if x >= bpp else 0
            if ftype == 1:
                line[x] = (line[x] + left) & 0xFF
            elif ftype == 2:
                line[x] = (line[x] + up) & 0xFF
            elif ftype == 3:
                line[x] = (line[x] + (left + up) // 2) & 0xFF
            elif ftype == 4:
                line[x] = (line[x] + paeth(left, up, upleft)) & 0xFF
            # ftype == 0（None）：不加任何预测。
        prev = line
        # 累加这一行的亮度直方图。灰度直接取值；彩色按 ITU-R BT.601 的
        # 亮度公式（和 PIL convert('L') 一致）；带 alpha 的忽略 alpha。
        for x in range(0, stride, channels):
            if channels in (1, 2):
                luminance = line[x]
            else:
                luminance = (line[x] * 299 + line[x + 1] * 587
                             + line[x + 2] * 114) // 1000
            histogram[luminance] += 1
    return histogram


def normalize_cards(raw_cards):
    """把页面 JS 返回的原始卡片数据整理成 [{"note_id","url","signed_url","title"}]。

    规则（照已校准的 browser-export 做法）：
    - 一张卡片里可能有多个链接：只取"路径以 24 位十六进制结尾、且带非空
      xsec_token 参数"的那一个；其他链接（封面图、第三方域名等）一律不要；
    - signed_url 统一改写成 https://www.xiaohongshu.com/explore/<id>?<原查询参数>，
      只保留原链接的查询参数；
    - url 是去掉签名的规范链接（https://www.xiaohongshu.com/explore/<id>），
      签名参数只出现在 signed_url 里——这是全项目"签名链接不外泄"的关键约定；
    - 没有带签名链接的卡片直接丢弃（没有签名就没法下载，登记了也没用）；
    - 标题截断到 500 字。

    术语：xsec_token 是小红书链接里的短时签名参数，不带它打不开笔记详情页。
    """
    items = []
    for card in raw_cards or []:
        signed = None
        for href in card.get('hrefs') or []:
            parts = urlsplit(href)
            # 第三方域名的链接直接丢弃（只认小红书本站）。
            if parts.netloc != 'www.xiaohongshu.com':
                continue
            match = _NOTE_PATH_RE.search(parts.path)
            if not match:
                continue
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            if not query.get('xsec_token'):
                continue
            signed = (match.group(1), urlencode(query))
            break
        if not signed:
            continue
        nid, query = signed
        signed_url = f'https://www.xiaohongshu.com/explore/{nid}'
        if query:
            signed_url += '?' + query
        items.append({
            'note_id': nid,
            'url': f'https://www.xiaohongshu.com/explore/{nid}',
            'signed_url': signed_url,
            'title': str(card.get('title', ''))[:500],
        })
    return items


def merge_items(ordered, new_items):
    """把新扫到的卡片合并进有序字典（键是笔记编号），按编号去重、保持首次出现的顺序。

    ordered 是 dict（Python 3.7+ 的 dict 保持插入顺序）；返回本轮真正新增的条数，
    "连续没有新增"的轮数就是靠它来判断页面是否已经滚到底。
    """
    added = 0
    for item in new_items:
        nid = item['note_id']
        if nid in ordered:
            continue
        ordered[nid] = item
        added += 1
    return added


def build_items(ordered):
    """把收集结果变成最终返回的 items 列表：按收藏列表顺序编号。

    ordered 是"编号 → 条目"的有序字典（插入顺序即页面上的先后顺序）；
    list_position 从 0 开始，上层（台账）用它记录每条收藏在列表里的位置。
    """
    return [dict(item, list_position=index)
            for index, item in enumerate(ordered.values())]


def is_blocked(url, text, has_verify_box):
    """判断页面是否被小红书风控拦下（纯函数）。

    满足任一条件就算被拦（实测的三种表现）：
    1. 地址路径以 /website-login/ 开头（如 /website-login/error、/website-login/captcha）；
    2. 页面上有风控/验证容器 .fe-verify-box；
    3. 正文含 MARKERS 里任一提示文字（"安全限制""IP存在风险"等）。
    """
    path = urlsplit(url or '').path
    if path.startswith('/website-login/'):
        return True
    if has_verify_box:
        return True
    return any(marker in (text or '') for marker in MARKERS)


def blocked_reason(url, text):
    """被拦截时给上层的 reason（纯函数），格式：blocked:<路径最后一段>:<6 位代码或 none>。

    例如地址是 /website-login/error?x=1、正文含"300012" → "blocked:error:300012"。
    只取路径最后一段，绝不带查询参数（查询参数里可能有签名信息，红线不允许外泄）。
    """
    path = urlsplit(url or '').path.rstrip('/')
    last = path.rsplit('/', 1)[-1] if path else 'none'
    match = re.search(r'\d{6}', text or '')
    return 'blocked:{}:{}'.format(last, match.group() if match else 'none')


def is_logged_in(has_me_link, me_text, has_login_ui):
    """用"肯定信号"判断是否已登录（纯函数）。

    三个条件必须同时满足：
    1. 侧栏存在指向自己主页的"我"链接；
    2. 该链接文字去掉空白后恰好是"我"；
    3. 页面上没有登录弹窗（有弹窗说明至少界面还停在登录状态）。
    以前只要"没看到登录弹窗"就算已登录，页面没渲染完时会误判。
    """
    return bool(has_me_link) and str(me_text or '').strip() == '我' and not has_login_ui


def normalize_profile_url(href):
    """把"我"链接的 href 规范成 https://www.xiaohongshu.com/user/profile/<24位十六进制>。

    页面上读到的是相对链接（/user/profile/<id>），也可能带查询参数；
    只认 24 位十六进制的主页编号，其他任何形状都返回 None（宁可让上层
    重新识别，也不把一个不认识的主页地址存进设置里）。
    """
    match = _PROFILE_PATH_RE.search(urlsplit(href or '').path)
    return f'https://www.xiaohongshu.com/user/profile/{match.group(1)}' if match else None


def parse_nominal(text):
    """解析标称总数（纯函数）："笔记・1495" → 1495；其他任何文字 → None。

    标称总数在收藏页的二级标签上，不是以前以为的主标签 .count。
    """
    match = re.fullmatch(r'笔记・(\d+)', str(text or '').strip())
    return int(match.group(1)) if match else None


def classify_page(state):
    """判断页面处于什么状态，返回 ok / login_required / blocked / unknown。

    参数 state 是 _read_page_state 读到的页面状态字典（键的含义见它自己的注释）。

    判断顺序（被拦截的优先级最高，避免在风控页上还去拉起扫码）：
    1. 命中 is_blocked（专用登录错误页 / 风控容器 / 提示文字）→ blocked（立即停，不绕过）；
    2. 出现登录弹窗，或侧栏有"登录"按钮 → login_required（没登录，需要扫码）。
       小红书未登录时会先渲染出收藏标签和几张不带签名的预览卡片，几秒后才
       弹登录框；以前只看弹窗，会把"没登录"误判成 ok。
    3. "收藏"标签已选中，并且 is_logged_in 为真（侧栏有"我"链接等肯定信号）
       → ok。也就是说：**必须同时看到"收藏页"和"已登录"才算扫描可以开始**；
    4. 其他情况 → unknown（页面结构不认识或还没渲染完，继续等）。
    """
    if is_blocked(state.get('url'), state.get('text'), state.get('has_verify_box', False)):
        return 'blocked'
    if state.get('has_login_ui') or state.get('has_login_btn'):
        return 'login_required'
    if state.get('has_fav_tab') and is_logged_in(
            state.get('has_me_link'), state.get('me_text'), state.get('has_login_ui')):
        return 'ok'
    return 'unknown'


def should_stop(no_new_rounds, height_changed, elapsed_s, count, max_minutes):
    """滚动收集的停止条件（纯函数）。

    满足任意一条就停：
    - 已经收满 5000 条（防异常页面无限加载）；
    - 超过 max_minutes 分钟（时间上限）；
    - 连续 8 轮没有新增、且页面高度没变（说明真的滚到底了）。
      阈值是 8 而不是 6：现在卡住时会先"回滚一下再到底"（见 _scroll_collect），
      每次自救多花两轮左右，停止阈值也要跟着放宽，避免页面只是加载慢
      就被误判成"滚完了"。
    """
    if count >= 5000:
        return True
    if elapsed_s >= max_minutes * 60:
        return True
    if no_new_rounds >= 8 and not height_changed:
        return True
    return False


class ScanResult(dict):
    """scan_favorites 的返回值：一个能当字典用的对象，但 repr 只显示条数。

    为什么要专门做一个类：items 里的 signed_url 带签名参数（红线：不能进
    日志），如果直接 print 一个普通 dict，签名链接就会被意外打印出来。
    所以覆盖 __repr__，只显示状态、数字和 items 的个数。
    """

    def __repr__(self):
        return ('ScanResult(status={!r}, visible={!r}, nominal={!r}, '
                'reason={!r}, items={} items)'.format(
                    self.get('status'), self.get('visible'), self.get('nominal'),
                    self.get('reason'), len(self.get('items') or [])))


# ---------------- 浏览器驱动函数（测试时注入假 Playwright） ----------------

def _now_iso():
    """当前时间的 ISO 字符串（带 Z，表示 UTC）。"""
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _read_page_state(page):
    """从页面上读出判断状态需要的全部输入，装进一个字典。

    小红书是前端渲染的，页面状态随时在变，所以每次判断都重新完整读一遍，
    不在函数之间传递"旧值"，避免拿到渲染一半的混合状态。
    """
    url = page.url
    text = page.evaluate(TEXT_JS) or ''
    tab_text = page.evaluate(_TAB_TEXT_JS.format(json.dumps(SELECTORS['fav_tab']))) or ''
    me_text = page.evaluate(_TAB_TEXT_JS.format(json.dumps(SELECTORS['me_link']))) or ''
    return {
        'url': url,
        'text': text,
        'has_fav_tab': tab_text == '收藏',
        'has_cards': bool(page.evaluate(_exists_js(SELECTORS['card']))),
        'has_login_ui': bool(page.evaluate(_exists_js(SELECTORS['login_ui']))),
        'has_verify_box': bool(page.evaluate(_exists_js(SELECTORS['verify_box']))),
        'has_me_link': me_text != '',
        'me_text': me_text,
        # 侧栏有没有"登录"按钮：有它就肯定没登录，详见 SELECTORS['login_btn']。
        'has_login_btn': bool(page.evaluate(_exists_js(SELECTORS['login_btn']))),
    }


def _read_profile_url(page):
    """读侧栏"我"链接的 href 并规范成主页地址；读不到或形状不对返回 None。"""
    try:
        href = page.evaluate(ME_HREF_JS) or ''
    except Exception:
        return None
    return normalize_profile_url(href)


def _wait_for_state(page, clock, sleep, timeout_s=60, done=None):
    """等页面渲染完再判断状态：每秒读一次页面状态并分类，满足条件就返回。

    为什么需要它：goto 之后立刻读页面，收藏标签和登录弹窗经常都还没渲染出来，
    会被误判成 unknown。所以每秒重读一次，直到分类结果满足 done 条件或超时。

    - done(status, state)：满足什么条件才算"渲染完了"，默认是"结果不是 unknown"；
      login_flow / detect_profile_url 会换成"已登录 / 有登录弹窗 / 被拦截 三者之一"；
    - 超时就返回最后一次的结果（可能是 unknown，由调用方决定怎么处理）。
    """
    if done is None:
        def done(status, state):
            return status != 'unknown'
    deadline = clock() + timeout_s
    state = _read_page_state(page)
    status = classify_page(state)
    while not done(status, state) and clock() < deadline:
        sleep(1)
        state = _read_page_state(page)
        status = classify_page(state)
    return status, state


def _read_nominal_total(page):
    """用 NOMINAL_JS 读收藏的标称总数（如 1495）；读不到或文字不像"笔记・1495"就返回 None。"""
    try:
        raw = page.evaluate(NOMINAL_JS) or ''
    except Exception:
        return None
    return parse_nominal(raw)


def _wait_for_cards(page, clock, sleep):
    """开始滚动前先等收藏列表"稳定"，再固定多等 3 秒。

    背景（开机自启后实测发现的偶发问题）：页面刚打开时前端还在初始化，
    如果首屏只有 10 张卡就开始滚到底，"加载更多"的请求可能发不出去；
    此后页面高度不再变化，滚到同一个位置不会再触发滚动事件，扫描就
    永远卡在 10 条。

    做法：每秒用 CARD_COUNT_JS 数一次列表里的卡片数，等到"卡片数 ≥ 20，
    或者已经过了 8 秒"为止（两个条件先到先算）；然后不管怎样都固定再等
    3 秒，让前端的初始化彻底跑完，才开始第一轮收集。
    等待全部通过注入的 sleep/clock 进行（测试时可以换成瞬时的）。
    """
    deadline = clock() + 8
    while True:
        try:
            count = page.evaluate(CARD_COUNT_JS)
        except Exception:
            count = 0    # 读不出来（列表还没渲染）就当作 0，继续等
        if count >= 20:
            break
        if clock() >= deadline:
            break
        sleep(1)
    sleep(3)


def _scroll_collect(page, ordered, started, clock, sleep, max_minutes):
    """滚动收集主循环：反复"收卡片 → 滚到底 → 等加载"，直到满足停止条件。

    自救动作：某一轮收卡片没有新增（说明页面可能卡住了），下一轮滚动改成先
    window.scrollBy(0, -1200) 往回拉 1200 像素、等 0.8 秒，再滚到底部。
    这是正常人"往回拉一下再往下翻"的动作——浏览器发现滚动位置变了，会
    重新触发滚动事件，页面就能继续加载下一批。不改 UA、不注入脚本，
    只是普通的窗口滚动操作。

    参数 ordered 是"编号 → 条目"的有序字典，收到的卡片直接并进去（按编号
    去重）；started 是整个扫描的开始时间（clock 值），时间上限按它算。
    """
    no_new_rounds = 0
    while True:
        height_before = page.evaluate(HEIGHT_JS)
        added = merge_items(ordered, normalize_cards(page.evaluate(COLLECT_JS)))
        no_new_rounds = 0 if added else no_new_rounds + 1
        if should_stop(no_new_rounds, False, clock() - started,
                       len(ordered), max_minutes):
            break
        # 上一轮没有新增：先"往回拉一下"、等 0.8 秒再滚到底，重新触发加载。
        if no_new_rounds:
            page.evaluate(SCROLL_BACK_JS)
            sleep(0.8)
        page.evaluate(SCROLL_JS)
        # 每轮滚动后等 2 秒：给下一批卡片足够的加载时间。
        sleep(2)
        height_after = page.evaluate(HEIGHT_JS)
        if should_stop(no_new_rounds, height_after != height_before,
                       clock() - started, len(ordered), max_minutes):
            break


def chrome_launch_args():
    """生成启动本机 Google Chrome 时的命令行参数（三处启动共用）。

    只放 Chromium 官方文档收录的参数，绝不放任何反检测参数（不改 UA、
    不去掉 navigator.webdriver、不伪装指纹）：

    - ``--disable-component-update``：禁用组件更新器（Chrome 的组件
      自动更新）。出处：Chromium 源码 chrome/common/chrome_switches.cc
      中的 kDisableComponentUpdate（"Disable component updates with
      the components updater"）。
    - ``--disable-background-networking``：禁用一批在后台发网络请求的
      子系统（包括更新探测）。出处：同文件中的
      kDisableBackgroundNetworking（"Disable several subsystems which
      run network requests in the background"）。

    为什么加这两条：实测发现启动专用浏览器会顺带
    拉起 Chrome 的自动更新程序，macOS 弹出"已阻止 python3.12 修改 Mac 上
    的 App"；关掉组件更新和后台网络请求后不再触发。
    """
    return ['--disable-component-update', '--disable-background-networking']


def _launch_context(playwright, user_data_dir, headless):
    """用 Playwright 打开本机 Google Chrome 的持久化浏览器上下文（共用参数）。

    chromium_sandbox=True：不传时 Playwright 默认带
    --no-sandbox 启动，页面顶部会出现"不受支持的命令行标记"黄条，也更不
    安全；显式开启 Chromium 自带的沙箱后黄条消失。启动参数统一由
    chrome_launch_args() 生成，scan_favorites / detect_profile_url /
    login_flow 三处共用。
    """
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir), channel='chrome', headless=headless,
        chromium_sandbox=True, args=chrome_launch_args(),
        locale='zh-CN', viewport={'width': 1280, 'height': 900})


# 登录说明页的完整 HTML。为什么要有它：扫码时会弹出一个完整的小红书
# 网页窗口，用户分不清该扫控制台里的二维码还是窗口里的。曾经试过用 CDP 把窗口
# 最小化，但在真机（macOS）上实测发现 Browser.setWindowBounds 的
# windowState="minimized" 在 macOS 上根本不生效（调用不报错，窗口却一直可见），
# 把窗口挪到屏幕外也会被系统拉回来。可行做法（本文件采用）：小红书页面照常
# 打开、留在后台标签，另开一个前台标签显示这张说明页，再把窗口缩小成小窗口。
#
# 内容约束：纯本地 HTML（set_content 直接写入，不访问任何网址）、不放图片和
# 外链、浅色背景居中排版；三句话告诉用户"扫控制台上的码、别关这个窗口、
# 旁边的小红书标签不用管"。
LOGIN_GUIDE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>XHS Wiki · 登录专用窗口</title>
<style>
  /* 说明页样式：浅色背景、中文无衬线字体、整体居中，只求清楚不求好看。 */
  body {
    margin: 0;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #f7f7f5;
    color: #333;
    font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei",
                 "Helvetica Neue", Arial, sans-serif;
  }
  main { max-width: 420px; padding: 24px; text-align: center; }
  h1 { font-size: 18px; margin: 0 0 16px; }
  p { font-size: 14px; line-height: 1.8; margin: 8px 0; }
</style>
</head>
<body>
  <main>
    <h1>XHS Wiki · 登录专用窗口</h1>
    <p>请回到浏览器里的 XHS Wiki 控制台，用小红书 App 扫控制台上的二维码。</p>
    <p>这个窗口是登录要用的，请不要关闭；扫码成功后它会自动关闭。</p>
    <p>旁边的小红书标签页不用管。</p>
  </main>
</body>
</html>
"""


def _show_login_guide(context, page):
    """扫码登录时的"说明页 + 小窗口"（替代已证实无效的窗口最小化）。

    在小红书页面 goto 之后调用，做三件事：
    1. 用 CDP（Chrome DevTools 协议，浏览器暴露的调试控制协议）把窗口缩小：
       先 ``Browser.getWindowForTarget`` 拿 windowId，再 ``Browser.setWindowBounds``
       设成 left=40/top=40/width=520/height=400。实测 macOS 会把窗口自动放大到
       约 500×375，所以 520×400 就是能设到的接近最小的尺寸；
    2. ``context.new_page()`` 开一个新标签（说明页），用 ``set_content`` 写入
       本地 HTML——**不访问任何网址**，只是显示文字；
    3. ``bring_to_front()`` 把说明页切到前台，小红书页面留在后台标签。
       页面用 viewport 固定成 1280×900，所以缩小窗口不影响它的布局和截图；
       Playwright 的截图不会把后台标签切到前台（已核对驱动源码），后台标签
       也不会被降频（启动参数默认关了后台限速）。

    返回说明页对象（调用方不需要用它做什么，说明页随 context.close() 一起
    关闭）。任何一步失败都只打印一条 ``warning: login_guide_failed`` 并返回
    None——说明页只是体验优化，绝不能因为它让登录走不下去。截图、刷新、
    登录检测始终只针对小红书页面对象，绝不对说明页做截图或刷新。
    """
    try:
        session = context.new_cdp_session(page)
        window = session.send('Browser.getWindowForTarget')
        session.send('Browser.setWindowBounds', {
            'windowId': window['windowId'],
            'bounds': {'left': 40, 'top': 40, 'width': 520, 'height': 400},
        })
        guide = context.new_page()
        guide.set_content(LOGIN_GUIDE_HTML)
        guide.bring_to_front()
        return guide
    except Exception:
        print('warning: login_guide_failed', flush=True)
        return None


def _result(status, visible, nominal, reason, items):
    """组装 ScanResult（集中一处，保证键的形状永远一致）。"""
    return ScanResult({'status': status, 'visible': visible, 'nominal': nominal,
                       'reason': reason, 'items': items})


def detect_profile_url(user_data_dir, *, playwright_entry=None,
                       sleep=time.sleep, clock=time.monotonic):
    """静默打开小红书首页，识别当前浏览器资料夹登录的是谁，返回他的主页地址。

    在整体里的位置：settings.json 里没有 profile_url 时，由 login_flow 成功后
    或运行前调用它自动识别，不再写死任何人的主页地址。

    行为：
    - 无窗口（静默）打开 https://www.xiaohongshu.com/explore，等页面稳定；
    - 已登录：读侧栏"我"链接的 href，规范成
      https://www.xiaohongshu.com/user/profile/<24位十六进制> 返回；
    - 未登录或被拦截：返回 None（被拦截时立即停，不绕过）。

    参数和 scan_favorites 一样可注入（playwright_entry / sleep / clock），
    测试时全部换成假替身，不启动真 Chrome。
    """
    if playwright_entry is None:
        from playwright.sync_api import sync_playwright as playwright_entry
    context = None
    try:
        with playwright_entry() as playwright:
            context = _launch_context(playwright, user_data_dir, headless=True)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(EXPLORE_URL, timeout=60000, wait_until='domcontentloaded')
                # 首页的三种稳定状态：被拦截 / 有登录弹窗 / 已登录。
                # 注意不能用"不是 unknown"：已登录的首页既没有收藏标签也没有
                # 登录弹窗，按页面分类会是 unknown，但它恰恰是正常状态。
                def settled(status, state):
                    return (is_blocked(state['url'], state['text'], state['has_verify_box'])
                            or state['has_login_ui']
                            or is_logged_in(state['has_me_link'], state['me_text'],
                                            state['has_login_ui']))
                _status, state = _wait_for_state(page, clock, sleep, done=settled)
                if not is_logged_in(state['has_me_link'], state['me_text'],
                                    state['has_login_ui']):
                    return None   # 未登录或被拦截：交给上层引导扫码/报告
                return _read_profile_url(page)
            finally:
                # 就算页面操作抛异常，浏览器资料夹句柄也要关掉。
                context.close()
    except Exception as error:
        # 异常信息里可能带页面 URL，所以不拼进返回值，只抛错误类型，由上层记日志。
        raise RuntimeError('detect_failed:' + type(error).__name__) from error


def login_flow(user_data_dir, qr_path, max_minutes=30,
               playwright_entry=None, sleep=time.sleep, clock=time.monotonic):
    """扫码登录流程：有窗口打开小红书首页，未登录就每 15 秒截图二维码，等用户扫码。

    返回 {"result": ..., "profile_url": ...}：
    - result 是 "logged_in"（扫码成功）/ "already_logged_in"（本来就登录着）/
      "timeout"（超时）/ "blocked"（出现验证码或访问受限，立即停，不绕过）；
    - profile_url：登录成功后顺手从侧栏"我"链接里识别出的主页地址
      （让调用方不用再跑一次 detect_profile_url）；timeout/blocked 时为 None。

    已校准的行为（全部保留）：
    - 扫码必须带窗口（用户得看到二维码才能扫），无论设置里 headless 是什么；
    - goto / reload 都只等到 domcontentloaded；
    - 二维码图片就绪（complete 且 naturalWidth>0）才截图；截出白图（深色像素
      低于 5%）就丢弃；二维码文字提示过期/已失效、或同一页停留满 120 秒就刷新；
    - 截图先写 .tmp.png 临时文件（Playwright 按扩展名认格式）、chmod 0600、
      再原子替换到 qr_path；结束时删除二维码截图文件。
    """
    if playwright_entry is None:
        from playwright.sync_api import sync_playwright as playwright_entry
    started = clock()
    context = None
    try:
        with playwright_entry() as playwright:
            # 扫码必须有窗口：登录流程固定 headless=False（不看任何设置）。
            context = _launch_context(playwright, user_data_dir, headless=False)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(EXPLORE_URL, timeout=60000, wait_until='domcontentloaded')
                # 打开小红书页面之后弹出"说明页 + 小窗口"：窗口必须
                # 开（不开会被风控拦），但完整窗口会和控制台里的二维码抢注意
                # 力，所以缩小窗口并让说明页占前台、小红书页面留在后台标签。
                # 说明页失败只当没发生，登录流程照常继续。
                _show_login_guide(context, page)
                page_opened = clock()  # 当前这页是几点打开的（用来算"停留满 120 秒"）

                # 首页的三种稳定状态：被拦截 / 有登录弹窗 / 已登录。
                def settled(status, state):
                    return (is_blocked(state['url'], state['text'], state['has_verify_box'])
                            or state['has_login_ui']
                            or is_logged_in(state['has_me_link'], state['me_text'],
                                            state['has_login_ui']))

                def reload_and_settle():
                    """刷新页面并重新等渲染完（二维码过期/停留太久时用）。"""
                    page.reload(timeout=60000, wait_until='domcontentloaded')
                    _wait_for_state(page, clock, sleep, done=settled)

                _status, state = _wait_for_state(page, clock, sleep, done=settled)
                if is_blocked(state['url'], state['text'], state['has_verify_box']):
                    return {'result': 'blocked', 'profile_url': None}
                if is_logged_in(state['has_me_link'], state['me_text'], state['has_login_ui']):
                    return {'result': 'already_logged_in',
                            'profile_url': _read_profile_url(page)}
                deadline = started + max_minutes * 60
                last_shot = None
                while clock() < deadline:
                    state = _read_page_state(page)
                    if is_blocked(state['url'], state['text'], state['has_verify_box']):
                        return {'result': 'blocked', 'profile_url': None}
                    if is_logged_in(state['has_me_link'], state['me_text'],
                                    state['has_login_ui']):
                        # 扫码成功：顺手识别主页地址返回给上层。
                        return {'result': 'logged_in',
                                'profile_url': _read_profile_url(page)}
                    now = clock()
                    # 同一页停留满 120 秒就刷新一次：小红书的二维码有效期很短，
                    # 就便弹窗文字还没提示过期，超时的码也扫不动了。
                    if now - page_opened >= 120:
                        reload_and_settle()
                        page_opened = clock()
                        last_shot = None
                        continue
                    # 截图前先看登录弹窗文字：提示"过期/已失效"就刷新页面换新码。
                    login_text = page.evaluate(LOGIN_TEXT_JS) or ''
                    if '过期' in login_text or '已失效' in login_text:
                        reload_and_settle()
                        page_opened = clock()
                        last_shot = None
                        continue
                    # 每 15 秒截一次二维码，写到 qr_path（先写临时文件再原子替换，
                    # 避免控制台正好在写文件时读到半张图）。
                    if last_shot is None or now - last_shot >= 15:
                        element = page.query_selector(SELECTORS['qr'])
                        if element is None:
                            reload_and_settle()
                            page_opened = clock()
                            last_shot = None
                        elif not page.evaluate(QR_READY_JS):
                            # 二维码图片还没加载完：这一轮先不截，睡 3 秒下一轮
                            # 再看，避免截到空白。
                            pass
                        else:
                            # 临时文件名以 .tmp.png 结尾：Playwright 按扩展名判断
                            # 图片格式，".tmp" 会报"不支持的类型"；type='png' 再
                            # 显式说一遍。
                            tmp_path = str(Path(qr_path).with_suffix('')) + '.tmp.png'
                            element.screenshot(path=tmp_path, type='png')
                            os.chmod(tmp_path, 0o600)
                            if looks_blank(tmp_path):
                                # 截出来是白图（深色像素占比过低，二维码还没画上）：
                                # 删掉临时文件、不替换 qr_path，让控制台继续显示
                                # 上一张可用的码，下一轮重截。
                                os.unlink(tmp_path)
                            else:
                                os.replace(tmp_path, qr_path)
                                last_shot = now
                    sleep(3)
                return {'result': 'timeout', 'profile_url': None}
            finally:
                context.close()
    except Exception as error:
        raise RuntimeError('login_failed:' + type(error).__name__) from error
    finally:
        # 无论结果如何，二维码截图都是临时文件，结束后删掉（可能已不存在，忽略）。
        try:
            os.unlink(qr_path)
        except OSError:
            pass


def scan_favorites(profile_url, *, headless, user_data_dir, max_minutes=30,
                   playwright_entry=None, sleep=time.sleep, clock=time.monotonic):
    """打开收藏页，滚到底收集所有带签名的收藏卡片，结果直接作为返回值（不写文件）。

    参数：
    - profile_url：收藏页所在用户的个人主页地址（由 detect_profile_url / login_flow
      识别出来，存进设置后传进来）；
    - headless：是否无窗口模式（正常运行静默，即 headless=True）；
    - user_data_dir：Playwright 专用浏览器资料夹（由 config 给出，测试时传临时目录）；
    - max_minutes：最长扫描分钟数；
    - playwright_entry：Playwright 入口，默认 playwright.sync_api.sync_playwright，
      测试时可以换成假的，不启动真 Chrome；
    - sleep / clock：等待函数和计时函数，测试时替换成瞬时的。

    返回 ScanResult（当字典用）：{"status","visible","nominal","reason","items"}。
    - status：ok / login_required / blocked / unknown；
    - items：[{"note_id","title","url"(规范链接，不带签名),"signed_url","list_position"}]，
      按收藏列表顺序排列，list_position 从 0 开始；
    - scan_empty、login_required、blocked、unknown 时 items 是空列表；
    - 除 items 里的 signed_url 外，返回值其余字段绝不含链接或签名参数。
    无论成功、需要登录、被拦截还是异常，浏览器都一定被关闭（try/finally）。
    """
    if playwright_entry is None:
        from playwright.sync_api import sync_playwright as playwright_entry
    started = clock()
    context = None
    try:
        with playwright_entry() as playwright:
            context = _launch_context(playwright, user_data_dir, headless=headless)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                # wait_until='domcontentloaded'：DOM 一出来就继续，不等所有图片/脚本
                # 下载完（默认要等 load 事件，小红书页面要 20 秒左右）。
                # 页面有没有渲染完，后面由 _wait_for_state 负责判断。
                page.goto(profile_url + '?tab=fav&subTab=note', timeout=60000,
                          wait_until='domcontentloaded')
                # 先等页面渲染出可识别的状态（收藏标签/登录弹窗/被拦截），最多 60 秒。
                status, state = _wait_for_state(page, clock, sleep)
                if status != 'ok':
                    # 需要登录/被拦截/页面不认识：不扫，直接报告状态（不含链接）。
                    reason = ('page_state_' + status if status != 'blocked'
                              else blocked_reason(state['url'], state['text']))
                    return _result(status, 0, None, reason, [])
                ordered = {}
                # 先等收藏列表稳定（卡片数 ≥ 20 或满 8 秒，再固定等 3 秒），
                # 然后才开始滚动收集——避免页面没初始化完就滚到底，卡在首屏 10 条。
                _wait_for_cards(page, clock, sleep)
                _scroll_collect(page, ordered, started, clock, sleep, max_minutes)
                # 标称总数只要在滚动结束后读一次（它在页头，滚动过程中不会变）。
                nominal = _read_nominal_total(page)
                # 扫到 0 条不算成功。已登录的账号收藏页一定会扫到带签名的卡片；
                # 一条都没有，多半是页面结构变了或渲染异常，绝不能当成"收藏就是
                # 0 条"往下走（那样会把全量对比搞乱）。按 unknown 上报，
                # reason 记 scan_empty。（这种情况连"扫不全"的重扫都不做：
                # 0 条重扫大概率还是 0 条。）
                if not ordered:
                    return _result('unknown', 0, nominal, 'scan_empty', [])
                # 扫得明显不全时重扫一次。正常扫描远高于标称数的一半；只拿到
                # 首屏那 10 条一定是没扫全。重扫就是刷新页面（domcontentloaded），
                # 重新走一遍"等稳定 → 滚动收集"，结果并入同一个有序字典。最多重扫 1 次。
                if nominal is not None and len(ordered) < nominal * 0.5:
                    page.reload(timeout=60000, wait_until='domcontentloaded')
                    status, _state = _wait_for_state(page, clock, sleep)
                    if status == 'ok':
                        _wait_for_cards(page, clock, sleep)
                        _scroll_collect(page, ordered, started, clock, sleep,
                                        max_minutes)
                    # 重扫后仍然不到一半，也不再按失败停下。关键事实：收藏列表按
                    # 收藏时间倒序，新收藏总在最前面——哪怕只扫到首屏 10 条，
                    # 当天的新收藏也都在里面。所以照常返回扫到的部分，status=ok
                    # 但 reason="scan_incomplete"，让上层照常登记、处理这部分
                    # 新收藏，并稍后自动重扫补齐。
                    if len(ordered) < nominal * 0.5:
                        return _result('ok', len(ordered), nominal,
                                       'scan_incomplete', build_items(ordered))
                return _result('ok', len(ordered), nominal, None, build_items(ordered))
            finally:
                # 内层 finally：就算页面操作抛异常，浏览器资料夹句柄也要关掉。
                context.close()
    except Exception as error:
        # 意外异常：关掉浏览器（上面 finally 已做）后向上抛，由 runner 记 error。
        # 异常信息里可能带页面 URL，所以这里不拼进返回值，只抛异常类型。
        raise RuntimeError('scan_failed:' + type(error).__name__) from error
