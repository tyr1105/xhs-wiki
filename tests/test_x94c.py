# -*- coding: utf-8 -*-
"""tests/test_x94c.py —— 扫码窗口改成"说明页 + 小窗口"的测试。

背景：最初用 CDP 把窗口最小化，真机（macOS）实测不生效。现在换成：
小红书页面留在后台标签，另开一个前台说明页（本地 HTML、不访问网址），
窗口用 CDP 缩小成 520×400 的小窗口。

本文件断言以下四点（全部用假的 Playwright，不启动真 Chrome）：
1. 说明页被 bring_to_front（且小红书页面从没被切到前台）；
2. 窗口缩小的 CDP 参数正确（先 getWindowForTarget，再 setWindowBounds）；
3. 截图和 reload 只发生在小红书页面上（说明页从不被截图/刷新）；
4. 说明页任何一步失败时，登录流程照常返回结果。

另外做两个轻量的内容断言：说明页 HTML 不含图片和外链；console.html 加了
弹窗提示行。红线自查：编号全部由代码拼出的虚构数据，不访问网络。

用法：
    .venv/bin/python -m pytest tests/test_x94c.py -q
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from test_scan import (EXPLORE_URL, FakeContext, FakeClock, FakePlaywright,
                       PID, instant_sleep)

from xhs_wiki import scan
from xhs_wiki.scan import LOGIN_GUIDE_HTML, login_flow


# 已登录剧本：login_flow 打开首页 → 弹说明页 → 识别出已登录 → 直接返回。
# 这是最短的路径，用来说明页本身不干扰登录结果。
LOGGED_IN_SCRIPT = {'url': EXPLORE_URL, 'has_login_ui': False, 'me_text': '我',
                    'me_href': '/user/profile/' + PID, 'cards': []}


class LoginGuideTests(unittest.TestCase):
    """login_flow 的"说明页 + 小窗口"行为（假的 Playwright）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.qr = self.root / 'qr.png'

    def tearDown(self):
        self.tmp.cleanup()

    def run_login(self, script, context=None):
        context = context or FakeContext(script)
        result = login_flow(user_data_dir=self.root / 'browser-profile',
                            qr_path=self.qr, max_minutes=1,
                            playwright_entry=lambda: FakePlaywright(context),
                            sleep=instant_sleep, clock=FakeClock())
        return result, context

    def test_guide_page_brought_to_front(self):
        # 说明页是第二个标签（context.pages[1]），必须被 bring_to_front
        # 切到前台；小红书页面（pages[0]）从没被切到前台。
        result, context = self.run_login(dict(LOGGED_IN_SCRIPT))
        self.assertEqual(result['result'], 'already_logged_in')
        self.assertEqual(len(context.pages), 2)
        guide = context.pages[1]
        self.assertEqual(guide.front_count, 1)
        self.assertEqual(context.pages[0].front_count, 0)

    def test_guide_page_content_is_local_html(self):
        # 说明页用 set_content 写入本地 HTML：标题和三句正文都在，
        # 且没有任何图片和外链（不访问任何网址）。
        _result, context = self.run_login(dict(LOGGED_IN_SCRIPT))
        html = context.pages[1].set_content_html
        self.assertIsNotNone(html, '说明页必须用 set_content 写入内容')
        self.assertIs(html, LOGIN_GUIDE_HTML)
        self.assertIn('XHS Wiki · 登录专用窗口', html)
        for sentence in ('请回到浏览器里的 XHS Wiki 控制台，用小红书 App 扫控制台上的二维码。',
                         '这个窗口是登录要用的，请不要关闭；扫码成功后它会自动关闭。',
                         '旁边的小红书标签页不用管。'):
            self.assertIn(sentence, html)
        self.assertNotIn('<img', html)
        self.assertNotIn('href', html)
        self.assertNotIn('http', html)

    def test_window_bounds_cdp_params(self):
        # 窗口缩小的 CDP 参数：恰好一个会话，先查窗口（windowId=7 是假会话
        # 固定返回的），再设 left=40/top=40/width=520/height=400。
        _result, context = self.run_login(dict(LOGGED_IN_SCRIPT))
        self.assertEqual(len(context.cdp_sessions), 1)
        self.assertEqual(context.cdp_sessions[0].commands, [
            ('Browser.getWindowForTarget', None),
            ('Browser.setWindowBounds', {
                'windowId': 7,
                'bounds': {'left': 40, 'top': 40, 'width': 520, 'height': 400}}),
        ])

    def test_guide_happens_after_goto(self):
        # _show_login_guide 必须在小红书页面 goto 之后调用：
        # goto 被调用时 CDP 会话还不存在，说明页建立之后才有。
        order = []
        context = FakeContext(dict(LOGGED_IN_SCRIPT))
        original_cdp = context.new_cdp_session
        original_goto = context.pages[0].goto

        def traced_cdp(page):
            order.append('cdp')
            return original_cdp(page)

        def traced_goto(url, **kwargs):
            order.append('goto')
            return original_goto(url, **kwargs)

        context.new_cdp_session = traced_cdp
        context.pages[0].goto = traced_goto
        self.run_login(dict(LOGGED_IN_SCRIPT), context=context)
        self.assertEqual(order[:2], ['goto', 'cdp'])

    def test_screenshot_and_reload_only_on_xhs_page(self):
        # 剧本：登录弹窗在、二维码可用 → 流程会截图；时钟每次加 40 秒，
        # 几轮就跨过 120 秒的停留阈值 → 小红书页面被 reload。断言这些
        # 动作只发生在 pages[0]：说明页既没被 reload，也没截过图。
        script = {'url': EXPLORE_URL, 'has_login_ui': True, 'me_text': '',
                  'cards': [], 'has_qr': True}

        class StepClock:
            """每次调用加 40 秒：几轮就跨过 120 秒的停留阈值。"""

            def __init__(self):
                self.now = 1000.0

            def __call__(self):
                self.now += 40.0
                return self.now

        context = FakeContext(script)
        result = login_flow(user_data_dir=self.root / 'browser-profile',
                            qr_path=self.qr, max_minutes=5,
                            playwright_entry=lambda: FakePlaywright(context),
                            sleep=instant_sleep, clock=StepClock())
        self.assertEqual(result['result'], 'timeout')
        xhs, guide = context.pages[0], context.pages[1]
        self.assertGreaterEqual(xhs.reload_count, 1)      # 小红书页面刷新过
        self.assertTrue(xhs.element.shots)                # 截图只落在它的元素上
        self.assertEqual(guide.reload_count, 0)           # 说明页从不刷新
        self.assertEqual(guide.element.shots, [])         # 说明页从不截图
        self.assertEqual(guide.front_count, 1)

    def test_guide_failure_does_not_break_login(self):
        # 说明页三步里任何一步炸掉（这里让 CDP 第二步失败），只打印一条
        # warning: login_guide_failed，登录流程照常返回结果。
        for fail_on in (None, 'Browser.getWindowForTarget',
                        'Browser.setWindowBounds'):
            with self.subTest(fail_on=fail_on):
                script = dict(LOGGED_IN_SCRIPT)
                if fail_on:
                    script['cdp_fail_on'] = fail_on
                context = FakeContext(script)
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = login_flow(
                        user_data_dir=self.root / 'browser-profile',
                        qr_path=self.qr, max_minutes=1,
                        playwright_entry=lambda: FakePlaywright(context),
                        sleep=instant_sleep, clock=FakeClock())
                self.assertEqual(result['result'], 'already_logged_in')
                if fail_on:
                    self.assertIn('warning: login_guide_failed', stdout.getvalue())
                else:
                    self.assertNotIn('warning: login_guide_failed',
                                     stdout.getvalue())

    def test_guide_new_page_failure_does_not_break_login(self):
        # 开新标签这一步失败（new_page 抛异常）也同样不影响登录结果。
        context = FakeContext(dict(LOGGED_IN_SCRIPT))
        original_new_page = context.new_page

        def exploding_new_page():
            raise RuntimeError('cannot open a new tab')

        context.new_page = exploding_new_page
        original_cdp = context.new_cdp_session
        context.new_cdp_session = lambda page: original_cdp(page)  # CDP 正常
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = login_flow(
                user_data_dir=self.root / 'browser-profile',
                qr_path=self.qr, max_minutes=1,
                playwright_entry=lambda: FakePlaywright(context),
                sleep=instant_sleep, clock=FakeClock())
        self.assertEqual(result['result'], 'already_logged_in')
        self.assertIn('warning: login_guide_failed', stdout.getvalue())
        _ = original_new_page   # 保留引用，明确表示只是替换期间不用它

    def test_blocked_still_returns_blocked_with_guide(self):
        # 出现验证码/访问受限时按原逻辑立即返回 blocked，不做任何绕过
        # （说明页流程不改变 blocked 的判定）。
        script = {'url': 'https://www.xiaohongshu.com/website-login/error',
                  'text': '安全限制 IP存在风险 300012', 'cards': []}
        result, context = self.run_login(script)
        self.assertEqual(result, {'result': 'blocked', 'profile_url': None})
        self.assertTrue(context.closed)

    def test_minimize_window_is_gone(self):
        # 旧的 _minimize_window 已删除（macOS 实测无效，留着误导）。
        self.assertFalse(hasattr(scan, '_minimize_window'))


class GuideHtmlTests(unittest.TestCase):
    """说明页 HTML 的静态约束（不跑浏览器）。"""

    def test_no_minimize_left_in_source(self):
        # scan.py 里不应再出现最小化窗口的写法。
        source = Path(scan.__file__).read_text('utf-8')
        self.assertNotIn("'minimized'", source)
        self.assertNotIn('_minimize_window', source)

    def test_console_html_mentions_popup_window(self):
        # 控制台扫码处要提示用户"会弹出一个登录专用小窗口，不用管它"。
        import xhs_wiki.runner as runner_module
        html = (Path(runner_module.__file__).resolve().parent
                / 'web' / 'console.html').read_text('utf-8')
        self.assertIn('同时会弹出一个写着「登录专用窗口」的小窗口，'
                      '不用管它，扫这里的二维码就行；登录成功后它会自动关闭。',
                      html)
