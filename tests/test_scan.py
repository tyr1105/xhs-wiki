"""扫描模块（scan.py）的单元测试：纯函数直接测；scan_favorites / login_flow /
detect_profile_url 用假的 Playwright 跑主要分支。

覆盖的返回形状：结果作返回值、不写扫描文件、
login_flow 返回字典、detect_profile_url 和 items 的顺序/位置。

红线遵守：不启动真实 Chrome、不访问网络、不调用真实模型，所有浏览器对象都是
本地造假的对象；所有笔记编号、主页编号、签名 token 都是代码生成的虚构数据。
"""
import os
import stat
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from xhs_wiki import scan
from xhs_wiki.scan import (CARD_COUNT_JS, COLLECT_JS, EXPLORE_URL, HEIGHT_JS,
                           LOGIN_TEXT_JS, NOMINAL_JS, SCROLL_BACK_JS,
                           SCROLL_JS, SELECTORS, TEXT_JS, blocked_reason,
                           build_items, classify_page, detect_profile_url,
                           is_blocked, is_logged_in, login_flow, merge_items,
                           normalize_cards, normalize_profile_url,
                           parse_nominal, scan_favorites, should_stop)

# ---- 全部虚构的测试数据：24 位十六进制编号由代码拼出来，不指向任何真实账号 ----
PID = 'ab12' + '0' * 20                      # 虚构主页编号
PROFILE = 'https://www.xiaohongshu.com/user/profile/' + PID
NID = 'cd34' + '0' * 20                      # 虚构笔记编号 1
OTHER = 'ef56' + '0' * 20                    # 虚构笔记编号 2
TOKEN = '-FAKE-token-1'                      # 虚构签名 token（不复述真实格式）


def fake_card(nid, token=TOKEN, extra_href=None):
    """造一张原始卡片：hrefs 里可以混入干扰链接（主页、第三方域名等）。

    签名参数用 f-string 插值拼进去，测试源码里不出现"参数=字面值"的写法。
    """
    hrefs = [f'https://www.xiaohongshu.com/explore/{nid}?xsec_token={token}',
             PROFILE]
    if extra_href:
        hrefs.append(extra_href)
    return {'hrefs': hrefs, 'title': '笔记' + nid[:4]}


def page_state(**overrides):
    """造一份 _read_page_state 形状的页面状态字典。

    默认值都是"没有"：没收藏标签、没卡片、没登录弹窗、没"我"链接、没登录按钮。
    测试用关键字参数覆盖关心的那一两个字段。
    """
    state = {'url': PROFILE, 'text': '', 'has_fav_tab': False, 'has_cards': False,
             'has_login_ui': False, 'has_verify_box': False,
             'has_me_link': False, 'me_text': '', 'has_login_btn': False}
    state.update(overrides)
    return state


def png_bytes(width, height, pixel_fn):
    """用标准库构造一张 RGB PNG（8 位、非隔行、过滤器全 0），返回字节。

    本项目依赖里没有 Pillow，也不为测试新增依赖，所以这里
    按 PNG 规范手工编码：每行像素前加 1 字节过滤器类型 0，再 zlib 压缩。
    pixel_fn(x, y) 返回 (r, g, b)。
    """
    raw = bytearray()
    for y in range(height):
        raw.append(0)   # 过滤器类型 None
        for x in range(width):
            raw.extend(pixel_fn(x, y))

    def chunk(ctype, payload):
        return (struct.pack('>I', len(payload)) + ctype + payload
                + struct.pack('>I', zlib.crc32(ctype + payload) & 0xFFFFFFFF))

    header = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header)
            + chunk(b'IDAT', zlib.compress(bytes(raw))) + chunk(b'IEND', b''))


def qr_like_png_bytes():
    """生成一张"像二维码"的 PNG：左半黑右半白（深色像素约 50%）。

    login_flow 用 looks_blank（看深色像素占比）判断白图，所以假截图必须是
    一张真的、深色像素够多的 PNG，才不会被当成白图丢掉。
    """

    def pixel(x, y):
        return (0, 0, 0) if x < 64 else (255, 255, 255)

    return png_bytes(128, 128, pixel)


# ---------------- 假的 Playwright 对象 ----------------

class FakeElement:
    """假的页面元素：screenshot 会真的写出一个文件，模拟二维码截图。

    默认内容是一张深色像素约 50% 的真 PNG（模拟清晰的二维码）；
    测试想模拟"截到白图"时，可以传打不开的假字节（looks_blank 会把
    打不开的图片也判成白图）。
    """

    def __init__(self, content=None):
        self.content = qr_like_png_bytes() if content is None else content
        self.shots = []          # 每次 screenshot 的完整参数，供测试断言
        self.shot_kwargs = []    # 每次 screenshot 收到的关键字参数

    def screenshot(self, path=None, **kwargs):
        # 真实 Playwright 的 element.screenshot 支持 path/type 等关键字参数；
        # 用 **kwargs 接收，测试里断言 type 是不是 'png'。
        with open(path, 'wb') as handle:
            handle.write(self.content)
        self.shots.append(path)
        self.shot_kwargs.append(kwargs)


class FakePage:
    """按"剧本"回应对各种页面 JS 的求值：收卡片、读高度、滚屏、读文字、判断元素存在。

    剧本（script）是可变字典：测试可以在 sleep 回调里改它，模拟"扫着扫着用户
    把码扫了"或"登录后侧栏出现我链接"。
    """

    def __init__(self, script, context):
        self.script = script          # 剧本：见各测试场景
        self.context = context
        self.url = script.get('url', PROFILE + '?tab=fav&subTab=note')
        self.round_index = 0
        self.tab_reads = 0            # 读"收藏"标签文字的次数（用来模拟渲染延迟）
        self.count_reads = 0          # 读卡片数量的次数（模拟列表慢慢渲染）
        self.scroll_backs = 0         # 执行"往回拉一下"滚动的次数
        self.eval_log = []            # 每次 evaluate 的表达式，供测试断言先后顺序
        self.reload_count = 0
        self.goto_kwargs = {}
        self.reload_kwargs = {}
        self.element = FakeElement()
        # 说明页（login_guide）用到的两个方法，记录调用供测试断言。
        self.set_content_html = None   # set_content 写入的 HTML（没调过是 None）
        self.front_count = 0           # bring_to_front 被调用的次数

    def goto(self, url, timeout=None, **kwargs):
        # 剧本里如果指定了 url，就模拟"打开后被网站跳走"（如跳到 /website-login/error）。
        # kwargs 记录下来，供测试断言 wait_until 等参数传对了。
        self.goto_kwargs = kwargs
        self.url = self.script.get('url', url)

    def evaluate(self, expression):
        self.eval_log.append(expression)
        if self.script.get('fail_on') and self.script['fail_on'] in expression:
            raise RuntimeError('page exploded')
        # 数当前列表里的卡片数。剧本可以用 card_counts 给出一串读数
        # （模拟"先 10 张、慢慢变多"）；不指定时默认 20——表示列表已渲染好，
        # 让旧的测试不用关心这个新等待。
        if expression == CARD_COUNT_JS:
            sequence = self.script.get('card_counts')
            if sequence is None:
                return 20
            value = sequence[min(self.count_reads, len(sequence) - 1)]
            if self.count_reads < len(sequence) - 1:
                self.count_reads += 1
            return value
        if expression == COLLECT_JS:
            rounds = self.script['cards']
            value = rounds[min(self.round_index, len(rounds) - 1)]
            if self.round_index < len(rounds) - 1:
                self.round_index += 1
            return value
        if expression == HEIGHT_JS:
            heights = self.script.get('heights', [1000])
            return heights[min(self.round_index, len(heights) - 1)]
        if expression == SCROLL_JS:
            return None
        if expression == SCROLL_BACK_JS:
            # 卡住时的"往回拉一下"滚动。
            self.scroll_backs += 1
            return None
        if expression == TEXT_JS:
            return self.script.get('text', '')
        if expression == LOGIN_TEXT_JS:
            return self.script.get('login_text', '')
        if expression == NOMINAL_JS:
            return self.script.get('nominal_text', '')
        if expression == scan.ME_HREF_JS:
            return self.script.get('me_href', '')
        # 选择器被 json.dumps 拼进表达式时，内部的引号会带反斜杠转义，
        # 所以这里按"特征子串"分派，而不是比对整段选择器原文。
        if '.xhs-user-page-primary-tabs' in expression:
            self.tab_reads += 1
            delay = self.script.get('tab_text_after_reads', 0)
            if delay and self.tab_reads <= delay:
                return ''  # 前几轮还没渲染出来
            return self.script.get('tab_text', '收藏')
        if '.channel-list' in expression:
            if 'getAttribute' in expression:
                # ME_HREF_JS：读"我"链接的 href（detect_profile_url / login_flow 用）。
                return self.script.get('me_href', '')
            return self.script.get('me_text', '')
        if '.side-bar-component' in expression:
            # 侧栏"登录"按钮存在与否（未登录的肯定信号）。
            return bool(self.script.get('has_login_btn', False))
        if '.note-item' in expression:
            return bool(self.script.get('cards'))
        if 'qrcode-img' in expression:
            # QR_READY_JS：二维码 img 是否加载完。
            return bool(self.script.get('qr_ready', True))
        if '.login-container' in expression:
            return self.script.get('has_login_ui', False)
        if '.fe-verify-box' in expression:
            return self.script.get('has_verify_box', False)
        raise AssertionError('unexpected evaluate: ' + expression[:60])

    def reload(self, timeout=None, **kwargs):
        self.reload_kwargs = kwargs
        self.reload_count += 1
        return None

    def set_content(self, html, **kwargs):
        # 说明页用 set_content 写入本地 HTML（不访问网址）。只记录内容。
        self.set_content_html = html

    def bring_to_front(self):
        # 把标签切到前台。只记次数，供测试断言"说明页被切到前台、
        # 小红书页面从没被切到前台"。
        self.front_count += 1

    def query_selector(self, selector):
        if selector == SELECTORS['qr']:
            return self.element if self.script.get('has_qr', True) else None
        return None


class FakeCDPSession:
    """假的 CDP 会话：记录 send 的命令，可按剧本失败。

    真实 Playwright 用 context.new_cdp_session(page) 建立 Chrome DevTools
    协议会话；登录流程用它把窗口缩小成小窗口（说明页方案）。这里记录
    每条命令供测试断言。
    """

    def __init__(self, script):
        self.script = script
        self.commands = []          # [(方法, 参数), …]

    def send(self, method, params=None):
        self.commands.append((method, params))
        if self.script.get('cdp_fail_on') == method:
            raise RuntimeError('cdp exploded')
        if method == 'Browser.getWindowForTarget':
            return {'windowId': 7}
        return {}


class FakeContext:
    def __init__(self, script):
        self.script = script
        self.pages = [FakePage(script, self)]
        self.closed = False
        self.kwargs = None
        self.cdp_sessions = []      # 建立过的 CDP 会话

    def new_page(self):
        # 真实 Playwright 的 new_page 会把标签加进 context.pages，
        # 这里保持一致——登录说明页（第二个标签）要能在 pages 里找到。
        page = FakePage(self.script, self)
        self.pages.append(page)
        return page

    def new_cdp_session(self, page):
        session = FakeCDPSession(self.script)
        self.cdp_sessions.append(session)
        return session

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, context):
        self.context = context

    def launch_persistent_context(self, **kwargs):
        self.context.kwargs = kwargs
        return self.context


class FakePlaywright:
    """长得像 playwright.sync_api.sync_playwright() 的替身。"""

    def __init__(self, context):
        self.chromium = FakeChromium(context)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def instant_sleep(_seconds):
    """测试用的"等待"：立刻返回，不真的等。"""


class FakeClock:
    """测试用的时钟：每次调用加 1 秒，模拟时间流逝但不用真等。"""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        self.now += 1.0
        return self.now


class NormalizeCardsTests(unittest.TestCase):
    def test_picks_signed_link_and_splits_url_and_signed_url(self):
        # 规范链接不带签名；签名只出现在 signed_url 里。
        items = normalize_cards([fake_card(NID)])
        self.assertEqual(items, [{
            'note_id': NID,
            'url': f'https://www.xiaohongshu.com/explore/{NID}',
            'signed_url': f'https://www.xiaohongshu.com/explore/{NID}'
                          f'?xsec_token={TOKEN}',
            'title': '笔记' + NID[:4]}])

    def test_card_without_signature_is_dropped(self):
        raw = {'hrefs': [f'https://www.xiaohongshu.com/explore/{NID}'], 'title': 'no token'}
        self.assertEqual(normalize_cards([raw, fake_card(NID, token='')]), [])

    def test_third_party_domain_link_is_dropped(self):
        raw = {'hrefs': [f'https://evil.example.com/explore/{NID}?xsec_token={TOKEN}'], 'title': 'x'}
        self.assertEqual(normalize_cards([raw]), [])

    def test_title_truncated_to_500(self):
        card = fake_card(NID)
        card['title'] = '长' * 600
        self.assertEqual(len(normalize_cards([card])[0]['title']), 500)


class PureFunctionTests(unittest.TestCase):
    def test_merge_items_dedupes_and_counts_new(self):
        ordered = {}
        self.assertEqual(merge_items(ordered, normalize_cards([fake_card(NID)])), 1)
        self.assertEqual(merge_items(ordered, normalize_cards([fake_card(NID), fake_card(OTHER)])), 1)
        self.assertEqual(list(ordered), [NID, OTHER])

    def test_build_items_orders_and_numbers_positions(self):
        # items 按收藏列表顺序排列，list_position 从 0 开始连续编号。
        ordered = {}
        merge_items(ordered, normalize_cards([fake_card(NID), fake_card(OTHER)]))
        items = build_items(ordered)
        self.assertEqual([item['list_position'] for item in items], [0, 1])
        self.assertEqual([item['note_id'] for item in items], [NID, OTHER])
        for item in items:
            self.assertEqual(set(item),
                             {'note_id', 'url', 'signed_url', 'title', 'list_position'})

    def test_normalize_profile_url_accepts_relative_and_query(self):
        # 相对链接、带查询参数、整段地址都要规范成同一形状。
        expected = 'https://www.xiaohongshu.com/user/profile/' + PID
        self.assertEqual(normalize_profile_url(f'/user/profile/{PID}'), expected)
        self.assertEqual(normalize_profile_url(f'/user/profile/{PID}?x=1'), expected)
        self.assertEqual(normalize_profile_url(expected), expected)

    def test_normalize_profile_url_rejects_bad_shapes(self):
        self.assertIsNone(normalize_profile_url(''))
        self.assertIsNone(normalize_profile_url(None))
        self.assertIsNone(normalize_profile_url('/user/profile/'))
        self.assertIsNone(normalize_profile_url('/user/profile/' + 'z' * 24))  # 不是十六进制
        self.assertIsNone(normalize_profile_url('/user/profile/' + 'a' * 23))  # 位数不够
        self.assertIsNone(normalize_profile_url('/explore/' + NID))            # 不是主页路径

    def test_classify_page_states(self):
        # ok 必须同时满足"收藏标签选中"和"已登录"。
        self.assertEqual(classify_page(page_state(has_fav_tab=True, has_cards=True,
                                                  has_me_link=True, me_text='我')), 'ok')
        self.assertEqual(classify_page(page_state(has_login_ui=True)), 'login_required')
        self.assertEqual(classify_page(page_state(has_login_btn=True)), 'login_required')
        self.assertEqual(classify_page(page_state(text='安全限制 IP存在风险 300012',
                                                  has_fav_tab=True)), 'blocked')
        self.assertEqual(classify_page(page_state()), 'unknown')

    def test_ok_requires_logged_in_even_with_fav_tab(self):
        # 有收藏标签、有卡片，但既没有"我"链接也没有登录按钮（页面还没渲染完）
        # → 不能判 ok，继续等（unknown）。
        self.assertEqual(classify_page(page_state(has_fav_tab=True, has_cards=True,
                                                  has_me_link=True, me_text='登录')), 'unknown')
        self.assertEqual(classify_page(page_state(has_fav_tab=True, has_cards=True)), 'unknown')

    def test_login_button_beats_fav_tab(self):
        # 未登录时小红书会先渲染收藏标签和预览卡片、几秒后才弹登录框。
        # 只要侧栏有"登录"按钮（或登录弹窗），就算收藏标签已选中也是 login_required，
        # 被拦截的优先级仍然最高。
        self.assertEqual(classify_page(page_state(has_fav_tab=True, has_cards=True,
                                                  has_login_btn=True)), 'login_required')
        self.assertEqual(classify_page(page_state(has_fav_tab=True, has_cards=True,
                                                  has_login_ui=True)), 'login_required')
        self.assertEqual(classify_page(page_state(has_fav_tab=True, has_login_btn=True,
                                                  text='安全限制 300012')), 'blocked')

    def test_login_page_with_captcha_button_is_not_blocked(self):
        # 真实踩过的坑：未登录页的手机号登录弹窗里本来就有"获取验证码"按钮，
        # 不能因此判成 blocked；有登录弹窗 → login_required。
        text = '登录即可查看 验证码登录 获取验证码'
        self.assertEqual(classify_page(page_state(text=text, has_login_ui=True)), 'login_required')
        self.assertFalse(is_blocked(PROFILE, text, False))

    def test_website_login_path_is_blocked(self):
        self.assertTrue(is_blocked('https://www.xiaohongshu.com/website-login/error?x=1',
                                   '', False))
        self.assertEqual(classify_page(page_state(
            url='https://www.xiaohongshu.com/website-login/error')), 'blocked')

    def test_verify_box_is_blocked(self):
        self.assertTrue(is_blocked(PROFILE, '', True))
        self.assertEqual(classify_page(page_state(has_verify_box=True)), 'blocked')

    def test_marker_texts_are_blocked(self):
        for marker in ('安全限制', 'IP存在风险', '安全验证', '请完成验证', '访问异常'):
            self.assertTrue(is_blocked(PROFILE, f'前文 {marker} 后文', False), marker)

    def test_blocked_reason_format(self):
        url = f'https://www.xiaohongshu.com/website-login/error?xsec_token={TOKEN}'
        self.assertEqual(blocked_reason(url, '安全限制 IP存在风险 300012'), 'blocked:error:300012')
        # 没有查询参数、没有 6 位代码时也不泄漏任何签名信息。
        self.assertEqual(blocked_reason('https://www.xiaohongshu.com/website-login/captcha',
                                        '请完成验证'), 'blocked:captcha:none')

    def test_is_logged_in_combinations(self):
        # 三个肯定信号齐全才算已登录。
        self.assertTrue(is_logged_in(True, '我', False))
        self.assertTrue(is_logged_in(True, ' 我 ', False))   # 文字允许带空白
        self.assertFalse(is_logged_in(True, '我', True))     # 还有登录弹窗
        self.assertFalse(is_logged_in(True, '登录', False))  # 侧栏文字不是"我"
        self.assertFalse(is_logged_in(False, '我', False))   # 没有"我"链接
        self.assertFalse(is_logged_in(False, '', True))

    def test_parse_nominal_inputs(self):
        self.assertEqual(parse_nominal('笔记・1495'), 1495)
        self.assertEqual(parse_nominal(' 笔记・0 '), 0)
        self.assertIsNone(parse_nominal(''))
        self.assertIsNone(parse_nominal('笔记'))
        self.assertIsNone(parse_nominal('笔记・'))
        self.assertIsNone(parse_nominal('专辑・40'))
        self.assertIsNone(parse_nominal('收藏 1495'))
        self.assertIsNone(parse_nominal('笔记・1495 条'))

    def test_should_stop_conditions(self):
        # 连续无新增的阈值是 8（卡住时会先回滚自救，多给两轮）。
        self.assertTrue(should_stop(8, False, 10, 100, 30))    # 连续 8 轮无新增且高度不变
        self.assertFalse(should_stop(7, False, 10, 100, 30))   # 7 轮还不够，继续等
        self.assertFalse(should_stop(8, True, 10, 100, 30))    # 页面还在变长就继续
        self.assertTrue(should_stop(0, True, 31 * 60, 100, 30))  # 超时
        self.assertTrue(should_stop(0, True, 10, 5000, 30))    # 收满 5000 条
        self.assertFalse(should_stop(2, False, 10, 100, 30))

    def test_scroll_back_js_is_plain_scroll(self):
        # 自救滚动只是普通的窗口滚动（scrollBy），不是反检测手段。
        self.assertEqual(SCROLL_BACK_JS, 'window.scrollBy(0, -1200)')

    def test_scroll_js_uses_scrolling_element(self):
        # 滚动元素是 document.scrollingElement，不是 document.body。
        self.assertEqual(SCROLL_JS, 'window.scrollTo(0, document.scrollingElement.scrollHeight)')
        self.assertEqual(HEIGHT_JS, 'document.scrollingElement.scrollHeight')

    def test_repr_only_shows_counts(self):
        # ScanResult 的 repr 只显示条数，绝不显示 items 内容（签名链接不能进日志）。
        ordered = {}
        merge_items(ordered, normalize_cards([fake_card(NID), fake_card(OTHER)]))
        result = scan._result('ok', 2, 2, None, build_items(ordered))
        text = repr(result)
        self.assertIn('items=2 items', text)
        self.assertNotIn('xsec_token', text)
        self.assertNotIn(NID, text)

    def test_repr_on_empty_items(self):
        result = scan._result('login_required', 0, None, 'page_state_login_required', [])
        self.assertEqual(
            repr(result),
            "ScanResult(status='login_required', visible=0, nominal=None, "
            "reason='page_state_login_required', items=0 items)")


class DetectProfileUrlTests(unittest.TestCase):
    """detect_profile_url：静默打开首页，从登录状态里自动识别主页地址。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_detect(self, script):
        context = FakeContext(script)
        result = detect_profile_url(user_data_dir=self.root / 'browser-profile',
                                    playwright_entry=lambda: FakePlaywright(context),
                                    sleep=instant_sleep, clock=FakeClock())
        return result, context

    def test_logged_in_returns_normalized_profile_url(self):
        script = {'url': EXPLORE_URL, 'me_text': '我', 'me_href': f'/user/profile/{PID}',
                  'cards': []}
        result, context = self.run_detect(script)
        self.assertEqual(result, 'https://www.xiaohongshu.com/user/profile/' + PID)
        self.assertTrue(context.closed)
        # 探测必须静默进行（扫码登录才带窗口）。
        self.assertTrue(context.kwargs['headless'])
        self.assertEqual(context.pages[0].goto_kwargs.get('wait_until'),
                         'domcontentloaded')

    def test_not_logged_in_returns_none(self):
        script = {'url': EXPLORE_URL, 'has_login_ui': True, 'me_text': '',
                  'me_href': '', 'cards': []}
        result, context = self.run_detect(script)
        self.assertIsNone(result)
        self.assertTrue(context.closed)

    def test_login_button_without_popup_returns_none(self):
        # 未登录的早期阶段：还没弹登录框，但侧栏已有"登录"按钮。
        script = {'url': EXPLORE_URL, 'has_login_btn': True, 'me_text': '',
                  'me_href': '', 'cards': []}
        result, context = self.run_detect(script)
        self.assertIsNone(result)
        self.assertTrue(context.closed)

    def test_blocked_returns_none(self):
        script = {'url': 'https://www.xiaohongshu.com/website-login/error',
                  'text': '安全限制 300012', 'me_text': '我', 'cards': []}
        result, context = self.run_detect(script)
        self.assertIsNone(result)
        self.assertTrue(context.closed)

    def test_unrecognizable_href_returns_none(self):
        # 已登录但"我"链接的 href 形状不对：返回 None，不猜。
        script = {'url': EXPLORE_URL, 'me_text': '我', 'me_href': '/explore/' + NID,
                  'cards': []}
        result, context = self.run_detect(script)
        self.assertIsNone(result)
        self.assertTrue(context.closed)

    def test_browser_error_raises_and_closes(self):
        context = FakeContext({'url': EXPLORE_URL, 'me_text': '我', 'cards': [],
                               'fail_on': 'channel-list'})
        with self.assertRaisesRegex(RuntimeError, 'detect_failed'):
            detect_profile_url(user_data_dir=self.root / 'browser-profile',
                               playwright_entry=lambda: FakePlaywright(context),
                               sleep=instant_sleep, clock=FakeClock())
        self.assertTrue(context.closed)


class ScanFavoritesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_scan(self, script, max_minutes=30):
        context = FakeContext(script)
        result = scan_favorites(PROFILE, headless=True,
                                user_data_dir=self.root / 'browser-profile',
                                max_minutes=max_minutes,
                                playwright_entry=lambda: FakePlaywright(context),
                                sleep=instant_sleep, clock=FakeClock())
        return result, context

    def test_ok_scrolls_until_no_new_and_returns_items(self):
        # 标称数要和卡片数一致（否则会触发"明显不全"的重扫规则）。
        script = {'cards': [[fake_card(NID), fake_card(OTHER)], [fake_card(OTHER)]],
                  'nominal_text': '笔记・2', 'me_text': '我'}
        result, context = self.run_scan(script)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['visible'], 2)
        self.assertEqual(result['nominal'], 2)
        self.assertIsNone(result['reason'])
        self.assertEqual([item['note_id'] for item in result['items']], [NID, OTHER])
        self.assertTrue(context.closed)
        # 浏览器资料夹参数要传对，无窗口模式。
        self.assertEqual(context.kwargs['channel'], 'chrome')
        self.assertTrue(context.kwargs['headless'])
        # goto 要带 wait_until='domcontentloaded'，不等全部资源加载完。
        self.assertEqual(context.pages[0].goto_kwargs.get('wait_until'),
                         'domcontentloaded')

    def test_items_shape_order_and_position(self):
        # 断言 items 的顺序、list_position、url 与 signed_url 的分工。
        third = 'ef57' + '0' * 20
        script = {'cards': [[fake_card(NID), fake_card(OTHER), fake_card(third)]],
                  'nominal_text': '笔记・3', 'me_text': '我'}
        result, context = self.run_scan(script)
        items = result['items']
        self.assertEqual(len(items), 3)
        self.assertEqual([item['note_id'] for item in items], [NID, OTHER, third])
        self.assertEqual([item['list_position'] for item in items], [0, 1, 2])
        for item in items:
            # 规范链接不带任何查询参数（签名只在 signed_url 里）。
            self.assertEqual(item['url'],
                             f'https://www.xiaohongshu.com/explore/{item["note_id"]}')
            self.assertNotIn('?', item['url'])
            self.assertIn(f'explore/{item["note_id"]}?', item['signed_url'])
            self.assertIn('xsec_token', item['signed_url'])

    def test_waits_for_render_before_deciding(self):
        # 前 3 次读页面都还没渲染出收藏标签（unknown），第 4 次才有 → 最终是 ok。
        script = {'cards': [[fake_card(NID)]], 'nominal_text': '笔记・1',
                  'me_text': '我', 'tab_text_after_reads': 3}
        result, context = self.run_scan(script)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['visible'], 1)
        self.assertTrue(context.closed)

    def test_login_required(self):
        result, context = self.run_scan({'has_login_ui': True, 'cards': []})
        self.assertEqual(result['status'], 'login_required')
        self.assertEqual(result['reason'], 'page_state_login_required')
        self.assertEqual(result['items'], [])   # 非成功状态 items 为空
        self.assertTrue(context.closed)

    def test_blocked(self):
        script = {'url': 'https://www.xiaohongshu.com/website-login/error?x=1',
                  'text': '安全限制 IP存在风险 300012', 'cards': [fake_card(NID)]}
        result, context = self.run_scan(script)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'blocked:error:300012')
        self.assertEqual(result['items'], [])
        self.assertTrue(context.closed)

    def test_blocked_by_verify_box(self):
        result, context = self.run_scan({'has_verify_box': True, 'cards': [fake_card(NID)]})
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(context.closed)

    def test_login_required_before_popup_via_login_button(self):
        # 首次真实运行的时间线：打开约 2 秒时页面已经有"收藏"标签和 10 张
        # 不带签名的预览卡片、没有登录弹窗，但侧栏有"登录"按钮；约 6 秒后才
        # 弹出登录框。有登录按钮就足以判 login_required——绝不能只看收藏标签
        # 就判 ok，然后在未登录状态下"扫描成功、0 条"。
        ten_unsigned = [{'hrefs': [f'https://www.xiaohongshu.com/explore/{NID}'],
                         'title': '预览'} for _ in range(10)]
        script = {'tab_text': '收藏', 'cards': [ten_unsigned],
                  'has_login_btn': True, 'has_login_ui': False, 'me_text': ''}
        reads = []

        def sleep_and_show_popup(seconds):
            # 模拟时间线后段：几秒后登录弹窗才弹出来（通常等不到这一步就返回了）。
            reads.append(1)
            if len(reads) >= 2:
                script['has_login_ui'] = True

        context = FakeContext(script)
        result = scan_favorites(PROFILE, headless=True,
                                user_data_dir=self.root / 'browser-profile',
                                max_minutes=30,
                                playwright_entry=lambda: FakePlaywright(context),
                                sleep=sleep_and_show_popup, clock=FakeClock())
        self.assertEqual(result['status'], 'login_required')
        self.assertEqual(result['reason'], 'page_state_login_required')
        self.assertEqual(result['items'], [])
        self.assertTrue(context.closed)

    def test_scan_empty_when_logged_in_and_no_signed_cards(self):
        # 已登录（侧栏有"我"）、但滚动结束后一张带签名的卡片都没有
        # → 不算成功：返回 unknown + scan_empty，items 为空。
        ten_unsigned = [{'hrefs': [f'https://www.xiaohongshu.com/explore/{NID}'],
                         'title': '预览'} for _ in range(10)]
        script = {'cards': [ten_unsigned], 'me_text': '我',
                  'nominal_text': '笔记・1495'}
        result, context = self.run_scan(script)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['visible'], 0)
        self.assertEqual(result['nominal'], 1495)
        self.assertEqual(result['reason'], 'scan_empty')
        self.assertEqual(result['items'], [])
        self.assertTrue(context.closed)

    def test_browser_closed_when_page_raises(self):
        # 页面在收卡片时抛异常：scan_favorites 向上抛错误，但浏览器必须已被 finally 关掉。
        context = FakeContext({'cards': [fake_card(NID)], 'me_text': '我',
                               'fail_on': 'tab-content-item'})
        with self.assertRaisesRegex(RuntimeError, 'scan_failed'):
            scan_favorites(PROFILE, headless=True,
                           user_data_dir=self.root / 'browser-profile',
                           playwright_entry=lambda: FakePlaywright(context),
                           sleep=instant_sleep, clock=FakeClock())
        self.assertTrue(context.closed)

    def test_timeout_stops_scan(self):
        # 页面一直有新增、高度一直变，只能靠时间上限停下来。
        rounds = [[fake_card(f'{i:024x}')] for i in range(200)]
        result, context = self.run_scan({'cards': rounds, 'me_text': '我',
                                         'heights': list(range(1000, 1200))},
                                        max_minutes=0)
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(context.closed)
        self.assertLessEqual(result['visible'], 200)

    # ---------------- 滚动加载偶发卡在 10 条 ----------------

    def _ten_cards(self, offset=0):
        """造 10 张不同的卡片（编号从 offset 开始编号），模拟首屏那一批。"""
        return [fake_card(f'{i:024x}') for i in range(offset, offset + 10)]

    def test_scroll_back_recovers_from_stuck_first_screen(self):
        # 主场景：第一轮只有 10 张卡、后面几轮没有新增；"往回拉一下再滚到底"
        # 之后页面恢复加载，最终拿全。标称 40、可见 30 ≥ 一半，不触发重扫。
        first = self._ten_cards(0)
        second = self._ten_cards(10)
        third = self._ten_cards(20)
        rounds = [first, first, first,                 # 前三轮卡在 10 张
                  first + second,                      # 回滚后开始有新增
                  first + second + third,              # 继续加载直到拿全
                  first + second + third]              # 之后真的到底了
        sleeps = []

        def recording_sleep(seconds):
            sleeps.append(seconds)

        context = FakeContext({'cards': rounds, 'me_text': '我',
                               'nominal_text': '笔记・40',
                               'card_counts': [10, 12, 20]})
        result = scan_favorites(PROFILE, headless=True,
                                user_data_dir=self.root / 'browser-profile',
                                max_minutes=30,
                                playwright_entry=lambda: FakePlaywright(context),
                                sleep=recording_sleep, clock=FakeClock())
        page = context.pages[0]
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['visible'], 30)
        self.assertEqual(result['nominal'], 40)
        self.assertIsNone(result['reason'])
        # 确实做过"往回拉一下"的自救滚动，而且后面跟着 0.8 秒的等待。
        self.assertGreaterEqual(page.scroll_backs, 1)
        self.assertIn(0.8, sleeps)
        # 结果直接作为返回值：30 条都在 items 里，顺序和位置完整。
        self.assertEqual(len(result['items']), 30)
        self.assertEqual([item['list_position'] for item in result['items']],
                         list(range(30)))

    def test_rescan_once_when_clearly_incomplete(self):
        # 一直卡在 10 张、标称 1495（10 远不到一半）→ 刷新页面重扫 1 次 →
        # 仍然是 10 张。照常返回扫到的部分（收藏按时间倒序，新收藏总在最前面，
        # 10 条足够覆盖当天新增），status=ok + scan_incomplete，让上层照常处理
        # 并稍后自动重扫补齐。
        script = {'cards': [self._ten_cards()], 'me_text': '我',
                  'nominal_text': '笔记・1495', 'card_counts': [10]}
        result, context = self.run_scan(script)
        page = context.pages[0]
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['visible'], 10)
        self.assertEqual(result['nominal'], 1495)
        self.assertEqual(result['reason'], 'scan_incomplete')
        self.assertEqual(len(result['items']), 10)
        # 最多重扫 1 次（reload 恰好 1 次），重扫用的也是 domcontentloaded。
        self.assertEqual(page.reload_count, 1)
        self.assertEqual(page.reload_kwargs.get('wait_until'), 'domcontentloaded')
        self.assertTrue(context.closed)

    def test_waits_before_first_collect(self):
        # 开始收集前确实等待了——卡片数的读取发生在第一次收卡片之前，
        # sleep 的调用里包含固定 3 秒的那一次。
        sleeps = []

        def recording_sleep(seconds):
            sleeps.append(seconds)

        context = FakeContext({'cards': [[fake_card(NID)]], 'me_text': '我',
                               'nominal_text': '笔记・1',
                               'card_counts': [10, 10, 20]})
        result = scan_favorites(PROFILE, headless=True,
                                user_data_dir=self.root / 'browser-profile',
                                playwright_entry=lambda: FakePlaywright(context),
                                sleep=recording_sleep, clock=FakeClock())
        page = context.pages[0]
        self.assertEqual(result['status'], 'ok')
        self.assertIn(3, sleeps)   # 固定再等 3 秒的那次等待确实发生过
        # 先数卡片（CARD_COUNT_JS）、后收卡片（COLLECT_JS），顺序不能反。
        self.assertLess(page.eval_log.index(CARD_COUNT_JS),
                        page.eval_log.index(COLLECT_JS))

    def test_no_rescan_when_nominal_missing(self):
        # 标称数读不到（None）时不触发重扫判断，10 条也算成功。
        script = {'cards': [self._ten_cards()], 'me_text': '我',
                  'nominal_text': '', 'card_counts': [10]}
        result, context = self.run_scan(script)
        page = context.pages[0]
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['visible'], 10)
        self.assertIsNone(result['nominal'])
        self.assertIsNone(result['reason'])
        self.assertEqual(page.reload_count, 0)   # 从没刷新重扫
        self.assertEqual(len(result['items']), 10)

    def test_result_repr_hides_signed_links(self):
        # repr(结果) 里不出现签名参数（防止被 print 进日志）。
        script = {'cards': [[fake_card(NID), fake_card(OTHER)]],
                  'nominal_text': '笔记・2', 'me_text': '我'}
        result, context = self.run_scan(script)
        self.assertNotIn('xsec_token', repr(result))


class LoginFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.qr = self.root / 'qr.png'

    def tearDown(self):
        self.tmp.cleanup()

    def run_login(self, script, max_minutes=5, sleep=instant_sleep, clock=None):
        context = FakeContext(script)
        result = login_flow(user_data_dir=self.root / 'browser-profile', qr_path=self.qr,
                            max_minutes=max_minutes,
                            playwright_entry=lambda: FakePlaywright(context),
                            sleep=sleep, clock=clock or FakeClock())
        return result, context

    def test_already_logged_in_returns_profile_url(self):
        script = {'url': EXPLORE_URL, 'has_login_ui': False, 'me_text': '我',
                  'me_href': f'/user/profile/{PID}', 'cards': []}
        result, context = self.run_login(script)
        self.assertEqual(result, {'result': 'already_logged_in',
                                  'profile_url': 'https://www.xiaohongshu.com/user/profile/' + PID})
        self.assertTrue(context.closed)
        self.assertFalse(context.kwargs['headless'])  # 扫码必须有窗口

    def test_login_flow_always_windowed_regardless_of_headless_setting(self):
        # 扫描默认静默（headless=True），但扫码登录必须带窗口——用户得看到
        # 二维码才能扫。login_flow 根本不接收 headless 参数，无论设置里
        # headless 是 True 还是 False，启动浏览器都是 headless=False。
        for setting_headless in (True, False):
            settings = {'headless': setting_headless}   # 模拟读到的服务设置
            script = {'url': EXPLORE_URL, 'has_login_ui': False, 'me_text': '我',
                      'me_href': f'/user/profile/{PID}', 'cards': []}
            context = FakeContext(script)
            result = login_flow(user_data_dir=self.root / 'browser-profile',
                                qr_path=self.qr, max_minutes=5,
                                playwright_entry=lambda: FakePlaywright(context),
                                sleep=instant_sleep, clock=FakeClock())
            self.assertEqual(result['result'], 'already_logged_in')
            # 设置里的 headless 不影响登录浏览器：始终带窗口。
            self.assertFalse(context.kwargs['headless'],
                             f'headless={settings["headless"]} 时登录也不能静默')

    def test_logged_in_after_scan(self):
        # 一开始显示登录弹窗；用户扫码后（第 3 轮）侧栏出现"我"链接 → logged_in，
        # 顺便识别出主页地址，截图被删。
        script = {'url': EXPLORE_URL, 'has_login_ui': True, 'me_text': '',
                  'me_href': '', 'cards': [], 'has_qr': True}
        context = FakeContext(script)
        page = context.pages[0]
        seen_shots = []

        def sleep_and_scan(seconds):
            # 模拟用户扫码：两次等待之后页面变成已登录状态。
            seen_shots.append(len(page.element.shots))
            if len(seen_shots) >= 3:
                script['has_login_ui'] = False
                script['me_text'] = '我'
                script['me_href'] = f'/user/profile/{PID}'

        result = login_flow(user_data_dir=self.root / 'browser-profile', qr_path=self.qr,
                            max_minutes=5,
                            playwright_entry=lambda: FakePlaywright(context),
                            sleep=sleep_and_scan, clock=FakeClock())
        self.assertEqual(result, {'result': 'logged_in',
                                  'profile_url': 'https://www.xiaohongshu.com/user/profile/' + PID})
        self.assertTrue(context.closed)
        # 截图确实写过（tmp 原子替换到 qr_path），流程结束后又被删掉。
        self.assertTrue(seen_shots)
        self.assertFalse(self.qr.exists())

    def test_timeout_returns_dict_with_none_profile(self):
        script = {'url': EXPLORE_URL, 'has_login_ui': True, 'me_text': '',
                  'cards': [], 'has_qr': False}

        class StepClock:
            """每次调用加 10 秒：足够截一张图，几轮后超时退出。"""

            def __init__(self):
                self.now = 0.0

            def __call__(self):
                self.now += 10.0
                return self.now

        result, context = self.run_login(script, max_minutes=1, clock=StepClock())
        self.assertEqual(result, {'result': 'timeout', 'profile_url': None})
        self.assertTrue(context.closed)

    def test_qr_screenshot_written_atomically_and_private(self):
        # 截图应通过 .tmp + os.replace 写到 qr_path，权限 0600（控制台不会读到半张图）。
        script = {'url': EXPLORE_URL, 'has_login_ui': True, 'me_text': '',
                  'cards': [], 'has_qr': True}

        class StepClock:
            """每次调用加 10 秒：足够截一张图，几轮后超时退出。"""

            def __init__(self):
                self.now = 0.0

            def __call__(self):
                self.now += 10.0
                return self.now

        context = FakeContext(script)
        original_replace = os.replace
        state = {}

        def spy_replace(src, dst):
            state['src'] = src
            state['dst'] = dst
            state['mode'] = stat.S_IMODE(os.stat(src).st_mode)
            return original_replace(src, dst)

        os.replace = spy_replace
        try:
            result = login_flow(user_data_dir=self.root / 'browser-profile',
                                qr_path=self.qr, max_minutes=1,
                                playwright_entry=lambda: FakePlaywright(context),
                                sleep=instant_sleep, clock=StepClock())
        finally:
            os.replace = original_replace
        self.assertEqual(result['result'], 'timeout')
        self.assertEqual(state['dst'], self.qr)
        self.assertEqual(state['mode'], 0o600)
        # 临时文件以 .tmp.png 结尾（Playwright 按扩展名认格式），且和 qr_path 同目录。
        self.assertTrue(state['src'].endswith('.tmp.png'))
        self.assertEqual(Path(state['src']).parent, self.qr.parent)
        self.assertFalse(self.qr.exists())  # 结束后临时截图被删掉

    def test_screenshot_passes_png_type(self):
        # element.screenshot 必须显式传 type='png'，否则按 .tmp 扩展名猜格式会报错。
        script = {'url': EXPLORE_URL, 'has_login_ui': True, 'me_text': '',
                  'cards': [], 'has_qr': True}
        context = FakeContext(script)

        class StepClock:
            def __init__(self):
                self.now = 0.0

            def __call__(self):
                self.now += 10.0
                return self.now

        result = login_flow(user_data_dir=self.root / 'browser-profile',
                            qr_path=self.qr, max_minutes=1,
                            playwright_entry=lambda: FakePlaywright(context),
                            sleep=instant_sleep, clock=StepClock())
        self.assertEqual(result['result'], 'timeout')
        element = context.pages[0].element
        self.assertTrue(element.shots)  # 确实截过图
        for kwargs in element.shot_kwargs:
            self.assertEqual(kwargs.get('type'), 'png')
        # 截图目标文件也必须是 .png 结尾的临时文件。
        for path in element.shots:
            self.assertTrue(str(path).endswith('.tmp.png'))

    def test_qr_img_not_ready_skips_screenshot(self):
        # img 还没加载完（complete/naturalWidth 不满足）时，这一轮不截图。
        script = {'url': EXPLORE_URL, 'has_login_ui': True, 'me_text': '',
                  'cards': [], 'has_qr': True, 'qr_ready': False}
        context = FakeContext(script)

        class StepClock:
            def __init__(self):
                self.now = 0.0

            def __call__(self):
                self.now += 10.0
                return self.now

        result = login_flow(user_data_dir=self.root / 'browser-profile',
                            qr_path=self.qr, max_minutes=1,
                            playwright_entry=lambda: FakePlaywright(context),
                            sleep=instant_sleep, clock=StepClock())
        self.assertEqual(result['result'], 'timeout')
        element = context.pages[0].element
        self.assertEqual(element.shots, [])   # 从没截过图
        self.assertFalse(self.qr.exists())    # qr_path 也没被写过

    def test_white_image_not_replaced(self):
        # 截图内容不是合法 PNG（FakeElement 写的是假字节）时，
        # looks_blank 打不开图片也算白图：删临时文件、不替换 qr_path。
        script = {'url': EXPLORE_URL, 'has_login_ui': True, 'me_text': '',
                  'cards': [], 'has_qr': True}
        context = FakeContext(script)
        context.pages[0].element = FakeElement(content=b'x' * 728)  # 真实白图也是 728 字节
        original_replace = os.replace
        replaced = []

        def spy_replace(src, dst):
            replaced.append(dst)
            return original_replace(src, dst)

        os.replace = spy_replace
        try:

            class StepClock:
                def __init__(self):
                    self.now = 0.0

                def __call__(self):
                    self.now += 10.0
                    return self.now

            result = login_flow(user_data_dir=self.root / 'browser-profile',
                                qr_path=self.qr, max_minutes=1,
                                playwright_entry=lambda: FakePlaywright(context),
                                sleep=instant_sleep, clock=StepClock())
        finally:
            os.replace = original_replace
        self.assertEqual(result['result'], 'timeout')
        element = context.pages[0].element
        self.assertTrue(element.shots)               # 确实试着截过（每轮都重试）
        self.assertNotIn(self.qr, replaced)          # 但 qr_path 从没被替换过
        # 白图的临时文件也删干净了，目录里不留 .tmp.png。
        self.assertEqual(list(self.root.glob('*.tmp.png')), [])

    def _write_png(self, path, pixel_fn):
        # 用标准库构造 PNG 存成文件（looks_blank 的用例共用）。
        Path(path).write_bytes(png_bytes(128, 128, pixel_fn))
        return path

    def test_looks_blank_all_white(self):
        # 纯白 128×128（深色像素 0%）→ 白图。真实白图就是这种。
        path = self._write_png(self.root / 'white.png', lambda x, y: (255, 255, 255))
        self.assertTrue(scan.looks_blank(path))

    def test_looks_blank_half_black_not_blank(self):
        # 一半黑一半白（深色像素 50%）→ 不是白图（二维码通常 30%–60%）。
        def half(x, y):
            return (0, 0, 0) if x < 64 else (255, 255, 255)

        path = self._write_png(self.root / 'half.png', half)
        self.assertFalse(scan.looks_blank(path))

    def test_looks_blank_one_percent_dots(self):
        # 只撒约 1% 的黑点 → 低于 5% 门槛，仍算白图。
        def dots(x, y):
            return (0, 0, 0) if (y * 128 + x) % 100 == 0 else (255, 255, 255)

        path = self._write_png(self.root / 'dots.png', dots)
        self.assertTrue(scan.looks_blank(path))

    def test_looks_blank_broken_file(self):
        # 坏文件（不是 PNG）打不开 → 也算白图，让调用方重截。
        path = self.root / 'broken.png'
        path.write_bytes(b'this is not a png at all')
        self.assertTrue(scan.looks_blank(path))

    def test_looks_blank_handles_filtered_png(self):
        # 用"上行过滤"（过滤器 2）编码的同一张半黑图也能正确解码：
        # 验证手写解码器的反过滤逻辑不是只会处理过滤器 0。
        width = height = 64
        rows = []
        previous = bytes([0] * (width * 3))
        for y in range(height):
            line = bytearray()
            for x in range(width):
                line.extend((0, 0, 0) if x < 32 else (255, 255, 255))
            filtered = bytes(((line[i] - previous[i]) & 0xFF)
                             for i in range(width * 3))
            rows.append(b'\x02' + filtered)
            previous = bytes(line)
        raw = b''.join(rows)

        def chunk(ctype, payload):
            return (struct.pack('>I', len(payload)) + ctype + payload
                    + struct.pack('>I', zlib.crc32(ctype + payload) & 0xFFFFFFFF))

        header = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
        data = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header)
                + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))
        path = self.root / 'upfiltered.png'
        path.write_bytes(data)
        self.assertFalse(scan.looks_blank(path))   # 半黑图不是白图

    def test_goto_uses_domcontentloaded(self):
        # goto / reload 都要带 wait_until='domcontentloaded'，不等全部资源加载完。
        script = {'url': EXPLORE_URL, 'has_login_ui': True, 'me_text': '',
                  'cards': [], 'login_text': '二维码已过期，请刷新'}
        result, context = self.run_login(script, max_minutes=1)
        self.assertEqual(result['result'], 'timeout')
        page = context.pages[0]
        self.assertEqual(page.goto_kwargs.get('wait_until'), 'domcontentloaded')
        self.assertGreaterEqual(page.reload_count, 1)
        self.assertEqual(page.reload_kwargs.get('wait_until'), 'domcontentloaded')

    def test_expired_qr_triggers_reload(self):
        # 登录弹窗文字含"已过期" → 先刷新页面再重新等渲染。
        script = {'url': EXPLORE_URL, 'has_login_ui': True, 'me_text': '',
                  'cards': [], 'login_text': '二维码已过期，请刷新'}
        result, context = self.run_login(script, max_minutes=1)
        page = context.pages[0]
        self.assertGreaterEqual(page.reload_count, 1)
        self.assertTrue(context.closed)

    def test_stale_page_120s_triggers_reload(self):
        # 同一页停留满 120 秒也要刷新一次（即使弹窗没提示过期）。
        script = {'url': EXPLORE_URL, 'has_login_ui': True, 'me_text': '',
                  'cards': []}

        class FastClock:
            """每次调用加 40 秒：几轮就跨过 120 秒的停留阈值。"""

            def __init__(self):
                self.now = 1000.0

            def __call__(self):
                self.now += 40.0
                return self.now

        context = FakeContext(script)
        result = login_flow(user_data_dir=self.root / 'browser-profile', qr_path=self.qr,
                            max_minutes=5,
                            playwright_entry=lambda: FakePlaywright(context),
                            sleep=instant_sleep, clock=FastClock())
        page = context.pages[0]
        self.assertGreaterEqual(page.reload_count, 1)
        self.assertTrue(context.closed)

    def test_blocked_when_website_login(self):
        script = {'url': 'https://www.xiaohongshu.com/website-login/error',
                  'text': '安全限制 IP存在风险 300012', 'cards': []}
        result, context = self.run_login(script)
        self.assertEqual(result, {'result': 'blocked', 'profile_url': None})
        self.assertTrue(context.closed)
        self.assertFalse(self.qr.exists())


if __name__ == '__main__':
    unittest.main()
