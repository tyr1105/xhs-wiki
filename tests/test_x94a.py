# -*- coding: utf-8 -*-
"""tests/test_x94a.py —— 扫码窗口、Chrome 参数、转写错误码等一组回归测试。

覆盖以下几条（下面的"第 N 条"分节与此对应）：
1. 扫码登录时把浏览器窗口处理成不抢注意力的形态（最初的 CDP 最小化已被
   "说明页 + 小窗口"替代，相关断言移至 tests/test_x94c.py）；
2. Chrome 启动参数（chromium_sandbox=True + 关组件/后台更新，无反检测）；
3. 语音转写错误码说明具体原因（asr_model_missing / audio_extract_failed（兼容旧码））；
4. Whisper 纯音乐幻听重复短句的清理与 coverage 标记；
5. 4 处源码修正的回归测试（pyproject 主依赖、_load_xhs_class、
   默认 asr_model 与 install.sh 对齐、XHS_WIKI_SKILLS_ROOT）；
6. 整理时类别沿用已有清单（assign 提示词带上类别清单）。

红线自查：全部用虚构数据（编号由代码拼出来、提示词响应是"虚构类别甲"这
类占位词），不访问网络、不启动真实 Chrome、不碰真实主目录。
"""

import importlib
import json
import os
import sys
import tempfile
import tomllib
import unittest
import wave
from pathlib import Path

import pytest

from test_markdown_engine import (assign_to, card_resp, make_engine, make_pkg,
                                  page_from_prompt, parse_note_ids)
from test_scan import (EXPLORE_URL, FakeContext, FakeClock, FakePlaywright,
                       instant_sleep)

from xhs_wiki import download, scan, skill, store
from xhs_wiki.parse import asr as asr_parse
from xhs_wiki.parse import package


# ---------------- 第 1 条：扫码窗口自动最小化 ----------------
# 已把"最小化窗口"换成"说明页 + 小窗口"（macOS 上 Browser.setWindowBounds
# 的 minimized 实测不生效），对应的 MinimizeWindowTests 一并删除，
# 新的断言在 tests/test_x94c.py 里。


# ---------------- 第 2 条：Chrome 启动参数 ----------------

class ChromeLaunchArgsTests(unittest.TestCase):
    """启动参数统一由一个函数生成：三处启动共用，无任何反检测参数。"""

    def test_args_are_documented_switches_only(self):
        # 只有两条 Chromium 官方收录的参数（组件更新 / 后台网络），
        # 绝不包含改 UA、去 webdriver 之类的反检测开关。
        args = scan.chrome_launch_args()
        self.assertEqual(args, ['--disable-component-update',
                                '--disable-background-networking'])
        joined = ' '.join(args)
        for banned in ('user-agent', 'user_agent', 'webdriver', 'fingerprint'):
            self.assertNotIn(banned, joined)

    def test_launch_context_uses_sandbox_and_args(self):
        # _launch_context 是 scan_favorites / detect_profile_url / login_flow
        # 三处共用的启动入口：必须开 Chromium 沙箱（去掉 --no-sandbox 黄条）
        # 并带上统一参数。
        context = FakeContext({'cards': []})
        playwright = FakePlaywright(context)
        scan._launch_context(playwright, '/tmp/fake-profile', headless=True)
        self.assertIs(context.kwargs['chromium_sandbox'], True)
        self.assertEqual(context.kwargs['args'], scan.chrome_launch_args())

    def test_login_flow_launches_with_sandbox(self):
        # 端到端再核一遍：登录流程实际走 _launch_context 时的启动参数。
        script = {'url': EXPLORE_URL, 'has_login_ui': False, 'me_text': '我',
                  'me_href': '/user/profile/' + 'ab12' + '0' * 20, 'cards': []}
        context = FakeContext(script)
        with tempfile.TemporaryDirectory() as tmp:
            scan.login_flow(user_data_dir=Path(tmp) / 'profile',
                            qr_path=Path(tmp) / 'qr.png', max_minutes=1,
                            playwright_entry=lambda: FakePlaywright(context),
                            sleep=instant_sleep, clock=FakeClock())
        self.assertIs(context.kwargs['chromium_sandbox'], True)
        self.assertEqual(context.kwargs['args'], scan.chrome_launch_args())


# ---------------- 第 3 条：转写错误码说明具体原因 ----------------

def write_wav(path: Path, seconds: float = 0.1) -> None:
    """生成一段 16 kHz 单声道 PCM16 的静音 WAV（虚构音频，只供读入）。"""
    import struct
    frames = int(16000 * seconds)
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(struct.pack('<h', 0) * frames)


class AsrErrorCodeTests(unittest.TestCase):
    """模型未装、音频提取失败要报各自的稳定错误码，不带异常文本。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_model_reports_asr_model_missing(self):
        # 模型目录是空的：所选的 whisper 没有安装 → asr_model_missing。
        wav = self.root / 'audio.wav'
        write_wav(wav)
        asr_parse._MODEL_CACHE.clear()
        with pytest.raises(RuntimeError, match='^asr_model_missing$'):
            asr_parse.transcribe(wav, model='whisper',
                                 models_dir=self.root / 'models')

    def test_invalid_video_reports_generic_code_without_external_program(self):
        # 损坏视频由 PyAV 拒绝；旧 ffmpeg 参数即使指向不存在的程序也不会被执行。
        video = self.root / 'video.mp4'
        video.write_bytes(b'fictional video bytes')
        with pytest.raises(RuntimeError, match='^audio_extract_failed$'):
            asr_parse.extract_audio(video, self.root / 'out.wav',
                                    ffmpeg=str(self.root / 'unused-command'))


class FakeFailingASR:
    """按剧本抛特定错误码的假语音接口（形状与 asr.py 相同）。"""

    def __init__(self, code):
        self.code = code

    def extract_audio(self, video_path, wav_path):
        raise RuntimeError(self.code)

    def transcribe(self, wav_path, *, model, models_dir):
        raise RuntimeError(self.code)


class PackageAsrErrorMappingTests(unittest.TestCase):
    """package 层把 asr 的稳定错误码透传给 runner（控制台失败列表直接显示）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.note_id = 'cd34' + '0' * 20
        self.media = self.root / 'media' / 'generated.mp4'
        self.media.parent.mkdir(mode=0o700)
        self.media.write_bytes(b'fictional media')

    def tearDown(self):
        self.tmp.cleanup()

    def download(self):
        import hashlib
        return {'status': 'ok', 'media_kind': 'video',
                'meta': {'title': '虚构标题', 'author': '虚构作者',
                         'published_at': '2026-01-01', 'type': '视频',
                         'desc': '虚构正文', 'tags': []},
                'media': [{'path': 'media/generated.mp4',
                           'bytes': self.media.stat().st_size,
                           'sha256': hashlib.sha256(self.media.read_bytes()).hexdigest()}]}

    def test_model_missing_passes_through(self):
        for raw, expected in [
            ('asr_model_missing', 'asr_model_missing'),
            ('audio_extract_failed', 'audio_extract_failed'),
            ('asr_audio_extract_failed', 'audio_extract_failed'),
            ('asr_audio_extract_failed:ffmpeg_not_found',
             'audio_extract_failed:ffmpeg_not_found'),
            ('synthetic_private_detail', 'asr_failed'),
        ]:
            with self.subTest(raw=raw):
                with pytest.raises(ValueError, match=f'^{expected}$') as caught:
                    package.build_package(self.note_id, self.root, self.download(),
                                          asr=FakeFailingASR(raw))
                self.assertTrue(caught.value.__suppress_context__)
                # 错误码绝不携带原始异常文本（可能含本机路径）。
                self.assertNotIn('synthetic', str(caught.value))


# ---------------- 第 4 条：Whisper 幻听重复短句 ----------------

HALLUCINATION_TEXT = '\n\n'.join(['示例字幕组 某某'] * 12)


class HallucinationTests(unittest.TestCase):
    """同一短句连续重复 ≥5 次、或占全文 ≥60%，就删掉；清空则打标记。"""

    def test_consecutive_repeat_removed(self):
        cleaned, emptied = package.strip_hallucinated_repeats(HALLUCINATION_TEXT)
        self.assertEqual(cleaned, '')
        self.assertTrue(emptied)

    def test_ratio_repeat_removed(self):
        # 不连续但占了大多数（7/10 ≥ 60%）：同样清掉。
        lines = ['正常内容甲'] + ['示例字幕组 某某'] * 7 + ['正常内容乙', '正常内容丙']
        cleaned, emptied = package.strip_hallucinated_repeats('\n\n'.join(lines))
        self.assertEqual(cleaned, '正常内容甲\n\n正常内容乙\n\n正常内容丙')
        self.assertFalse(emptied)

    def test_normal_repetition_kept(self):
        # 只重复 4 次（< 5）、占比也不到 60%：不是幻听，原样保留。
        text = '\n\n'.join(['这是一句正常的话'] * 4
                             + ['别的正常内容甲', '别的正常内容乙',
                                '别的正常内容丙', '别的正常内容丁'])
        cleaned, emptied = package.strip_hallucinated_repeats(text)
        self.assertEqual(cleaned, text)
        self.assertFalse(emptied)

    def test_long_phrase_repeat_kept(self):
        # 超过 20 字的句子即使反复出现也不算幻听（阈值只针对短句）。
        long_line = '这一句是超过二十个字的正常转写内容所以不算幻听短句'
        cleaned, _ = package.strip_hallucinated_repeats('\n\n'.join([long_line] * 8))
        self.assertIn(long_line, cleaned)

    def test_empty_text_is_not_hallucination(self):
        # 本来就没有语音：不能误标成幻听。
        cleaned, emptied = package.strip_hallucinated_repeats('  \n \n')
        self.assertEqual((cleaned, emptied), ('', False))


class FakeTextASR:
    """返回固定转写文本的假语音接口（不跑 ffmpeg、不加载模型）。"""

    def __init__(self, text):
        self.text = text

    def extract_audio(self, video_path, wav_path):
        Path(wav_path).write_bytes(b'fictional audio')

    def transcribe(self, wav_path, *, model, models_dir):
        return {'text': self.text, 'segments': [], 'model': model, 'seconds': 0.0}


class HallucinationPackageTests(unittest.TestCase):
    """纯幻听转写清空后：coverage 标记 audio_transcript_hallucination。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.note_id = 'cd34' + '0' * 20
        self.media = self.root / 'media' / 'generated.mp4'
        self.media.parent.mkdir(mode=0o700)
        self.media.write_bytes(b'fictional media')

    def tearDown(self):
        self.tmp.cleanup()

    def download(self):
        import hashlib
        return {'status': 'ok', 'media_kind': 'video',
                'meta': {'title': '虚构标题', 'author': '虚构作者',
                         'published_at': '2026-01-01', 'type': '视频',
                         'desc': '虚构正文', 'tags': []},
                'media': [{'path': 'media/generated.mp4',
                           'bytes': self.media.stat().st_size,
                           'sha256': hashlib.sha256(self.media.read_bytes()).hexdigest()}]}

    def test_pure_hallucination_coverage(self):
        result = package.build_package(
            self.note_id, self.root, self.download(),
            asr=FakeTextASR(HALLUCINATION_TEXT), models_dir=self.root / 'models')
        self.assertEqual(result['coverage'], 'audio_transcript_hallucination')
        document = json.loads((self.root / 'package.json').read_text('utf-8'))
        self.assertEqual(document['audio_transcript'], '')
        # 落盘的包能原样恢复（校验接受"幻听清空"与"本来没语音"的区别丢失）。
        self.assertEqual(package.load_package(self.root), result)

    def test_mixed_hallucination_keeps_real_text(self):
        text = '\n\n'.join(['示例字幕组 某某'] * 6 + ['这是真正讲到的虚构内容'])
        result = package.build_package(
            self.note_id, self.root, self.download(),
            asr=FakeTextASR(text), models_dir=self.root / 'models')
        self.assertEqual(result['coverage'], 'audio_transcript')
        document = json.loads((self.root / 'package.json').read_text('utf-8'))
        self.assertEqual(document['audio_transcript'], '这是真正讲到的虚构内容')
        self.assertEqual(package.load_package(self.root), result)


# ---------------- 第 5 条：4 处源码修正的回归测试 ----------------

REPO_ROOT = Path(__file__).resolve().parent.parent


class PyprojectDependencyTests(unittest.TestCase):
    """默认语音模型 whisper 需要的库必须在主依赖里（原误放可选依赖）。"""

    def test_faster_whisper_is_main_dependency(self):
        data = tomllib.loads((REPO_ROOT / 'pyproject.toml').read_text('utf-8'))
        deps = data['project']['dependencies']
        self.assertTrue(any(dep.startswith('faster-whisper') for dep in deps),
                        f'faster-whisper 必须在主依赖里，现在是：{deps}')


class LoadXhsClassTests(unittest.TestCase):
    """vendor 根目录和 XHS-Downloader-* 子目录都能加载到 XHS 类。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vendor = Path(self.tmp.name) / 'vendor'
        source_dir = self.vendor / 'XHS-Downloader-2.8' / 'source'
        source_dir.mkdir(parents=True)
        (source_dir / '__init__.py').write_text(
            'class XHS:\n    VERSION = "fictional"\n', encoding='utf-8')
        self._saved_modules = sys.modules.get('source')
        self._saved_path = list(sys.path)

    def tearDown(self):
        # 清干净这次测试往 sys.path / sys.modules 里塞的东西，不影响别的测试。
        sys.path[:] = [p for p in sys.path
                       if not p.startswith(str(self.tmp.name))]
        if self._saved_modules is None:
            sys.modules.pop('source', None)
        else:
            sys.modules['source'] = self._saved_modules
        sys.path.extend(p for p in self._saved_path if p not in sys.path)
        self.tmp.cleanup()

    def test_loads_from_vendor_root(self):
        # 实际遇到过的故障：只把 vendor 根目录传进来也要能找到子目录里的包。
        cls = download._load_xhs_class(self.vendor)
        self.assertEqual(cls.VERSION, 'fictional')

    def test_loads_from_downloader_subdirectory(self):
        cls = download._load_xhs_class(self.vendor / 'XHS-Downloader-2.8')
        self.assertEqual(cls.VERSION, 'fictional')


class DefaultAsrModelTests(unittest.TestCase):
    """store 的默认语音模型必须和安装脚本默认下载的一致。"""

    def test_default_matches_install_sh(self):
        text = (REPO_ROOT / 'install.sh').read_text('utf-8')
        # install.sh 顶部的 ASR_MODEL="..." 是默认值（--asr 参数可以覆盖）。
        import re
        match = re.search(r'^ASR_MODEL="(\w+)"', text, re.MULTILINE)
        self.assertIsNotNone(match, 'install.sh 里找不到默认 ASR_MODEL')
        self.assertEqual(store.DEFAULT_SETTINGS['asr_model'], match.group(1),
                         'store 默认语音模型与安装脚本默认下载的模型不一致')


class SkillsRootEnvTests(unittest.TestCase):
    """XHS_WIKI_SKILLS_ROOT 必须能改写 Skill 的安装目录（不写真实 ~/.claude）。"""

    SETTINGS = {'kb_root': '/tmp/fictional-kb', 'engine': 'markdown',
                'setup_completed': True}

    def test_env_var_redirects_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            target_root = Path(tmp) / 'skills'
            # 记下原来的值，结束时原样恢复（以前直接删掉，
            # conftest 设好的临时 Skill 目录会被清掉，之后的测试改回默认的
            # 真实 ~/.claude/skills）。
            previous = os.environ.get('XHS_WIKI_SKILLS_ROOT')
            os.environ['XHS_WIKI_SKILLS_ROOT'] = str(target_root)
            try:
                # 环境变量在模块导入时读取，必须重新加载才生效。
                reloaded = importlib.reload(skill)
                path = reloaded.install(self.SETTINGS)
                installed = path.is_file()
                removed = reloaded.uninstall()   # 仍在改写后的根目录下卸载
            finally:
                if previous is None:
                    os.environ.pop('XHS_WIKI_SKILLS_ROOT', None)
                else:
                    os.environ['XHS_WIKI_SKILLS_ROOT'] = previous
                importlib.reload(skill)   # 恢复成原来的模块状态
            self.assertEqual(path, target_root / 'xhs-wiki' / 'SKILL.md')
            self.assertTrue(installed)
            # 卸载也走同一个根。
            self.assertTrue(removed)
            self.assertFalse(path.exists())


# ---------------- 第 6 条：整理时类别沿用已有的 ----------------

class AssignPromptCategoryTests(unittest.TestCase):
    """assign 提示词必须带上现有类别清单（模板本身不预设任何类别词）。"""

    def test_prompt_contains_existing_categories(self):
        import re
        p1, p2 = make_pkg(1), make_pkg(2)
        # kb_root 先给个占位（make_engine 要求可 Path()），下面换进临时目录。
        engine, provider = make_engine(
            '.',
            # 响应按模型调用的先后排队：卡片1 → 归类1 → 主题页1 →
            # 卡片2 → 归类2（并入已有主题）→ 主题页2。
            [card_resp(1),
             lambda p: json.dumps({'assignments': [{
                 'note_id': nid,
                 'new_topic': {'name': '虚构主题甲', 'description': '收集同类虚构内容',
                               'category': '虚构类别甲'}} for nid in parse_note_ids(p)]},
                 ensure_ascii=False),
             page_from_prompt,
             card_resp(2),
             lambda p: assign_to(parse_note_ids(p), 1),
             page_from_prompt],
        )
        with tempfile.TemporaryDirectory() as tmp:
            engine.kb_root = Path(tmp) / 'kb'
            engine.ingest([p1])
            engine.distill([p1.note_id])
            # 第二批归类前库里已有"虚构类别甲"，提示词必须列出它。
            engine.ingest([p2])
            engine.distill([p2.note_id])
        assign_prompts = [p for p in provider.calls if '新卡片（每行一张' in p]
        self.assertEqual(len(assign_prompts), 2)
        _header, body = assign_prompts[1].split('现有类别（新主题的类别优先从这里选）：\n')
        self.assertTrue(body.splitlines()[0].startswith('虚构类别甲'),
                        f'第二批归类提示词应带上现有类别清单：{body[:80]}')
        # 首次归类（还没有任何类别）也要渲染出"（暂无类别）"占位。
        self.assertIn('（暂无类别）', assign_prompts[0])

    def test_prompt_template_has_no_preset_category_words(self):
        template = (Path(engine_module_path()) / 'prompts' / 'assign.md').read_text('utf-8')
        # 模板里只能有 {{categories}} 占位，不能出现任何具体类别词。
        self.assertIn('{{categories}}', template)
        self.assertNotIn('虚构', template)


def engine_module_path() -> Path:
    from xhs_wiki.engines.markdown import engine
    return Path(engine.__file__).resolve().parent
