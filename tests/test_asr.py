"""验证语音解析层的公共约定和失败边界，全部素材与模型都由测试生成。

测试不读取真实音频、不导入真实推理引擎，也不运行外部媒体程序。
模型、时钟和解码器均替换为假的对象，避免模型下载、联网和不稳定计时。
"""

from pathlib import Path
from types import SimpleNamespace
import re
import sys
import wave

import numpy as np
import pytest

from xhs_wiki.parse import asr


# 每个用例都清空缓存并阻断真实模型创建，防止用例互相影响或误加载大模型。
@pytest.fixture(autouse=True)
def isolated_models(monkeypatch):
    asr._MODEL_CACHE.clear()

    def forbid_real_backend(*args, **kwargs):
        raise AssertionError("测试没有注入假的模型工厂")

    monkeypatch.setattr(asr, "_create_backend", forbid_real_backend)
    yield
    asr._MODEL_CACHE.clear()


# 只生成零值 PCM 音频；PCM 是按固定采样频率记录振幅的未压缩声音格式。
def make_wav(path, *, channels=1, rate=16000, width=2, frames=1600):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(width)
        stream.setframerate(rate)
        stream.writeframes(bytes(frames * channels * width))
    return path


# 假后端只返回预先构造的片段，计时由调用方控制，不依赖机器运行速度。
class FakeBackend:
    def __init__(self, *, on_transcribe=None):
        self.calls = []
        self.on_transcribe = on_transcribe

    def transcribe(self, samples):
        self.calls.append(samples.copy())
        if self.on_transcribe:
            self.on_transcribe()
        return [
            {"start": 0.0, "end": 0.05, "text": "甲句。"},
            {"start": 0.05, "end": 0.1, "text": "乙句。"},
        ]


# 两种模型都必须给上层相同字段，计时也必须覆盖实际推理过程。
@pytest.mark.parametrize("model", ["sensevoice", "whisper"])
def test_transcribe_common_shape_and_elapsed_time(tmp_path, monkeypatch, model):
    clock = [10.0]

    def infer():
        clock[0] += 5.0

    backend = FakeBackend(on_transcribe=infer)

    def factory(*args):
        clock[0] += 3.0
        return backend

    monkeypatch.setattr(asr, "_create_backend", factory)
    monkeypatch.setattr(asr, "_clock", lambda: clock[0])
    result = asr.transcribe(make_wav(tmp_path / "synthetic.wav"), model=model,
                            models_dir=tmp_path / "models")
    assert set(result) == {"text", "segments", "model", "seconds"}
    assert "".join(result["text"].split()) == "甲句。乙句。"
    assert result["model"] == model
    assert result["seconds"] == 8.0
    assert len(result["segments"]) == 2
    assert backend.calls[0].dtype == np.float32
    for segment in result["segments"]:
        assert set(segment) == {"start", "end", "text"}
        assert 0 <= segment["start"] <= segment["end"] <= 0.1


# 缓存按模型与规范化目录区分：相同目录不同写法复用，不同模型或目录不混用。
def test_cache_reuses_model_and_separates_model_directories(tmp_path, monkeypatch):
    calls = []
    clock = [0.0]

    def factory(model, models_dir):
        calls.append((model, models_dir))
        clock[0] += 2.0
        return FakeBackend()

    monkeypatch.setattr(asr, "_create_backend", factory)
    monkeypatch.setattr(asr, "_clock", lambda: clock[0])
    models = tmp_path / "models"
    assert asr.load_model("sensevoice", models) == 2.0
    assert asr.load_model("sensevoice", models / "child" / "..") == 0.0
    assert asr.load_model("whisper", models) == 2.0
    assert asr.load_model("sensevoice", tmp_path / "alternate") == 2.0
    assert len(calls) == 3


# 后端加载失败时不能把半成品存进缓存，否则下一次调用会误用坏状态。
def test_failed_loading_is_not_cached(tmp_path, monkeypatch):
    calls = []

    def factory(*args):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("synthetic_sensitive_detail")
        return FakeBackend()

    monkeypatch.setattr(asr, "_create_backend", factory)
    with pytest.raises((RuntimeError, ValueError)) as error:
        asr.load_model("sensevoice", tmp_path)
    assert "synthetic_sensitive_detail" not in str(error.value)
    assert re.fullmatch(r"[a-z][a-z0-9_]+", str(error.value))
    asr.load_model("sensevoice", tmp_path)
    assert len(calls) == 2


# 无效选项应在触及模型工厂前报错，避免默认回落到另一模型。
def test_unknown_model_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        asr.transcribe(tmp_path / "missing.wav", model="unknown", models_dir=tmp_path)


# 空文件没有可识别的声音，不应加载大模型或凭空生成文字。
def test_empty_wav_returns_empty_result_without_loading(tmp_path):
    result = asr.transcribe(make_wav(tmp_path / "empty.wav", frames=0),
                            model="sensevoice", models_dir=tmp_path)
    assert result["text"] == ""
    assert result["segments"] == []
    assert result["seconds"] >= 0
    assert not asr._MODEL_CACHE


# 模型推理失败只暴露错误码，第三方异常内容不能进入调用方的报错信息。
def test_transcription_failure_hides_backend_details(tmp_path, monkeypatch):
    def infer(samples):
        raise RuntimeError("synthetic_sensitive_detail")

    monkeypatch.setattr(asr, "_create_backend", lambda *args: SimpleNamespace(transcribe=infer))
    with pytest.raises(RuntimeError) as error:
        asr.transcribe(make_wav(tmp_path / "synthetic.wav"),
                        model="whisper", models_dir=tmp_path)
    assert str(error.value) == "asr_transcription_failed"


# 仅生成虚构版本和空占位文件，模拟本机模型缓存结构，不读取真实模型目录。
# org_dir：缓存目录名。HF 仓库换过组织名，新旧两个名字都能用。
def make_whisper_cache(models_dir, org_dir="models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"):
    cache = models_dir / "faster-whisper" / org_dir
    revision = "a" * 40
    reference = cache / "refs" / "main"
    reference.parent.mkdir(parents=True)
    reference.write_text(revision, encoding="utf-8")
    snapshot = cache / "snapshots" / revision
    snapshot.mkdir(parents=True)
    for name in ["model.bin", "config.json", "tokenizer.json", "vocabulary.json"]:
        (snapshot / name).touch()
    return snapshot, reference


# 假模块截取配置即可确认 CPU/int8/离线读取，且传给模型的是已经验证的本地快照。
def test_whisper_factory_uses_local_cpu_int8(tmp_path, monkeypatch):
    snapshot, _ = make_whisper_cache(tmp_path)
    calls = []
    fake_model = object()

    def construct(name, **kwargs):
        calls.append((name, kwargs))
        return fake_model

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=construct))
    backend = asr._create_whisper(tmp_path)
    assert backend.model is fake_model
    name, kwargs = calls[0]
    assert name == str(snapshot)
    assert kwargs["device"] == "cpu"
    assert kwargs["compute_type"] == "int8"
    assert kwargs["local_files_only"] is True
    assert kwargs["download_root"] == str(tmp_path / "faster-whisper")


# 模型仓库换过组织名，路线 2 同时认新旧两个缓存目录名。
def test_whisper_factory_accepts_new_org_cache(tmp_path, monkeypatch):
    """只有新组织名（dropbox-dash）的缓存也能找到。"""
    snapshot, _ = make_whisper_cache(
        tmp_path, "models--dropbox-dash--faster-whisper-large-v3-turbo")
    calls = []
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(
        WhisperModel=lambda name, **kwargs: calls.append(name) or object()))
    asr._create_whisper(tmp_path)
    assert calls == [str(snapshot)]


def test_whisper_factory_accepts_old_org_cache(tmp_path, monkeypatch):
    """只有旧组织名（mobiuslabsgmbh）的缓存仍然能用。"""
    snapshot, _ = make_whisper_cache(tmp_path)  # 默认旧组织名
    calls = []
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(
        WhisperModel=lambda name, **kwargs: calls.append(name) or object()))
    asr._create_whisper(tmp_path)
    assert calls == [str(snapshot)]


def test_whisper_factory_prefers_new_org_cache(tmp_path, monkeypatch):
    """两个缓存目录都在时优先用新组织名的那个。"""
    new_snapshot, _ = make_whisper_cache(
        tmp_path, "models--dropbox-dash--faster-whisper-large-v3-turbo")
    make_whisper_cache(tmp_path)  # 旧组织名也造一份
    calls = []
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(
        WhisperModel=lambda name, **kwargs: calls.append(name) or object()))
    asr._create_whisper(tmp_path)
    assert calls == [str(new_snapshot)]


# 缺少分词器会触发上游备用下载，因此必须在进入模型构造函数前直接拒绝。
def test_whisper_missing_tokenizer_never_constructs_model(tmp_path, monkeypatch):
    snapshot, _ = make_whisper_cache(tmp_path)
    (snapshot / "tokenizer.json").unlink()
    calls = []
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(
        WhisperModel=lambda *args, **kwargs: calls.append((args, kwargs)),
    ))
    with pytest.raises(ValueError, match="^asr_model_files_missing$"):
        asr._create_whisper(tmp_path)
    assert calls == []


# 不可信版本引用既不能变成路径穿越，也不能被传给上游当作在线仓库版本解析。
@pytest.mark.parametrize("revision", ["../outside", "g" * 40, "a" * 39])
def test_whisper_invalid_revision_never_constructs_model(tmp_path, monkeypatch, revision):
    _, reference = make_whisper_cache(tmp_path)
    reference.write_text(revision, encoding="utf-8")
    calls = []
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(
        WhisperModel=lambda *args, **kwargs: calls.append((args, kwargs)),
    ))
    with pytest.raises(ValueError, match="^asr_model_files_missing$"):
        asr._create_whisper(tmp_path)
    assert calls == []


# 假 sherpa 模块验证量化模型、ITN 和 VAD 的加载配置，所有模型文件都是空占位符。
def test_sensevoice_factory_enables_itn_and_local_vad(tmp_path, monkeypatch):
    model_dir = tmp_path / asr.SENSEVOICE_DIR
    model_dir.mkdir()
    for path in [model_dir / "model.int8.onnx", model_dir / "tokens.txt",
                 tmp_path / "silero_vad.onnx"]:
        path.touch()
    calls = {}
    recognizer = object()
    vad = object()

    def create_recognizer(**kwargs):
        calls["recognizer"] = kwargs
        return recognizer

    def create_vad(config, **kwargs):
        calls["vad"] = (config, kwargs)
        return vad

    module = SimpleNamespace(
        OfflineRecognizer=SimpleNamespace(from_sense_voice=create_recognizer),
        VadModelConfig=lambda: SimpleNamespace(silero_vad=SimpleNamespace()),
        VoiceActivityDetector=create_vad,
    )
    monkeypatch.setitem(sys.modules, "sherpa_onnx", module)
    backend = asr._create_sensevoice(tmp_path)
    assert backend.recognizer is recognizer and backend.vad is vad
    assert calls["recognizer"]["model"] == str(model_dir / "model.int8.onnx")
    assert calls["recognizer"]["use_itn"] is True
    assert calls["recognizer"]["provider"] == "cpu"
    assert calls["recognizer"]["sample_rate"] == 16000
    config, kwargs = calls["vad"]
    assert config.silero_vad.model == str(tmp_path / "silero_vad.onnx")
    assert config.sample_rate == 16000
    assert config.silero_vad.max_speech_duration <= kwargs["buffer_size_in_seconds"]


# 标签只是模型的附加信息，去除它们时要保留正文与已有标点。
@pytest.mark.parametrize("raw, expected", [
    ("<|zh|><|NEUTRAL|><|Speech|><|withitn|>甲句，乙句。", "甲句，乙句。"),
    ("  <|en|>alpha <|HAPPY|>beta!  ", "alpha beta!"),
    ("<|zh|><|NEUTRAL|>", ""),
    ("甲 < 乙。", "甲 < 乙。"),
])
def test_sensevoice_tags_are_removed_without_losing_text(raw, expected):
    assert asr._clean_sensevoice_text(raw) == expected


# 读取层只接受流水线承诺的 16kHz、单声道、16位 WAV，错误格式不能悄悄误解码。
@pytest.mark.parametrize("options", [
    {"channels": 2}, {"rate": 8000}, {"width": 1},
])
def test_read_wav_rejects_wrong_audio_format(tmp_path, options):
    path = make_wav(tmp_path / "synthetic.wav", **options)
    with pytest.raises(ValueError):
        asr._read_wav(path)


# 检查有符号采样值归一化，避免把负振幅当作很大的正数送给模型。
def test_read_wav_normalizes_signed_pcm(tmp_path):
    path = tmp_path / "synthetic.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(np.array([-32768, 0, 16384], dtype="<i2").tobytes())
    samples = asr._read_wav(path)
    np.testing.assert_array_equal(samples, [-1.0, 0.0, 0.5])


# VAD 即语音活动检测：这里用队列模拟边读边产出的片段和文件末尾的未完成片段。
class FakeVad:
    def __init__(self):
        self.queue = []
        self.accepted = []
        self.resets = 0
        self.flushes = 0
        self.emitted = False

    def reset(self):
        self.resets += 1
        self.queue.clear()
        self.emitted = False

    def accept_waveform(self, samples):
        self.accepted.append(samples.copy())
        if not self.emitted:
            self.queue.append(SimpleNamespace(start=1600, samples=np.ones(1600)))
            self.emitted = True

    def empty(self):
        return not self.queue

    @property
    def front(self):
        return self.queue[0]

    def pop(self):
        self.queue.pop(0)

    def flush(self):
        self.flushes += 1
        self.queue.append(SimpleNamespace(start=3200, samples=np.ones(800)))


# 假识别器记录采样率与收到的音频，并提供带标签的输出以验证后处理。
class FakeRecognizer:
    def __init__(self):
        self.accepted = []
        self.decoded = 0

    def create_stream(self):
        def accept(rate, samples):
            self.accepted.append((rate, len(samples)))

        return SimpleNamespace(accept_waveform=accept,
                               result=SimpleNamespace(text="<|zh|><|NEUTRAL|>甲句。"))

    def decode_stream(self, stream):
        self.decoded += 1


# 分段时间必须换算为原音频秒数；flush 保留末尾片段，第二次调用必须重置 VAD。
def test_sensevoice_vad_timestamps_tail_and_reuse():
    vad = FakeVad()
    recognizer = FakeRecognizer()
    backend = asr._SenseVoiceBackend(recognizer, vad)
    samples = np.ones(4000, dtype=np.float32)
    expected = [
        {"start": 0.1, "end": 0.2, "text": "甲句。"},
        {"start": 0.2, "end": 0.25, "text": "甲句。"},
    ]
    assert backend.transcribe(samples) == expected
    assert backend.transcribe(samples) == expected
    assert vad.resets >= 2
    assert vad.flushes == 2
    assert all(len(chunk) <= 512 for chunk in vad.accepted)
    assert sum(len(chunk) for chunk in vad.accepted) >= 8000
    assert recognizer.accepted == [(16000, 1600), (16000, 800)] * 2


# Whisper 的片段是惰性生成器：必须在返回结果前消费，避免漏计真实识别时间。
def test_whisper_consumes_generator_and_preserves_timestamps():
    state = []

    def recognize(samples, **kwargs):
        assert kwargs["language"] == "zh"
        assert kwargs["beam_size"] == 5

        def segments():
            state.append("consumed")
            yield SimpleNamespace(start=0.1, end=0.2, text="  甲句。  ")

        return segments(), SimpleNamespace()

    backend = asr._WhisperBackend(SimpleNamespace(transcribe=recognize))
    assert backend.transcribe(np.zeros(16000, dtype=np.float32)) == [
        {"start": 0.1, "end": 0.2, "text": "甲句。"},
    ]
    assert state == ["consumed"]


# 解码器收到完整路径，带空格不会被拆开；输出格式、限幅、权限和原子替换都要核对。
def test_extract_audio_uses_private_atomic_pcm_output(tmp_path, monkeypatch):
    video = tmp_path / "input with spaces.mp4"
    video.write_bytes(b"synthetic video")
    output = tmp_path / "private audio" / "output.wav"
    calls = []
    replace = asr.os.replace

    def decode(path):
        calls.append(path)
        assert not output.exists()
        return np.array([-2, -1, -0.5, 0, 0.5, 1, 2], dtype=np.float32)

    def publish(temporary, target):
        assert temporary.parent == output.parent
        assert temporary != target == output
        assert temporary.stat().st_mode & 0o777 == 0o600
        with wave.open(str(temporary), "rb") as stream:
            assert (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) == (
                16000, 1, 2)
            samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2")
        np.testing.assert_array_equal(samples, [-32768, -32768, -16384, 0, 16384, 32767, 32767])
        replace(temporary, target)

    monkeypatch.setattr(asr, "_decode_audio", decode)
    monkeypatch.setattr(asr.os, "replace", publish)
    # 旧关键字继续可传，但不存在的外部程序不会被调用。
    asr.extract_audio(video, output, ffmpeg="synthetic-unused-command")
    assert calls == [video.resolve()]
    assert output.stat().st_mode & 0o777 == 0o600
    assert output.parent.stat().st_mode & 0o777 == 0o700
    assert list(output.parent.iterdir()) == [output]


# 失败时保留旧文件并清理临时文件；第三方错误中的私密文本不应出现在公开异常里。
@pytest.mark.parametrize("failure", [RuntimeError, OSError, ImportError, ValueError])
def test_extract_audio_failure_preserves_previous_output(tmp_path, monkeypatch, failure):
    video = tmp_path / "synthetic.mp4"
    video.write_bytes(b"synthetic video")
    output = tmp_path / "output.wav"
    output.write_bytes(b"previous audio")

    def decode(path):
        raise failure("synthetic_sensitive_detail")

    monkeypatch.setattr(asr, "_decode_audio", decode)
    with pytest.raises(RuntimeError, match="^audio_extract_failed$") as error:
        asr.extract_audio(video, output)
    assert error.value.__suppress_context__
    assert output.read_bytes() == b"previous audio"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["output.wav", "synthetic.mp4"]


# 清理临时文件失败也必须脱敏，避免 finally 中的新异常泄露底层路径信息。
def test_extract_audio_cleanup_error_is_sanitized(tmp_path, monkeypatch):
    video = tmp_path / "synthetic.mp4"
    video.write_bytes(b"synthetic video")

    def fail_decode(path):
        raise RuntimeError("synthetic_sensitive_detail")

    def fail_unlink(path, *args, **kwargs):
        raise PermissionError("synthetic_sensitive_detail")

    # 只在调用期间替换 Path.unlink，离开上下文后允许 pytest 正常清理临时目录。
    with monkeypatch.context() as patches:
        patches.setattr(asr, "_decode_audio", fail_decode)
        patches.setattr(asr.Path, "unlink", fail_unlink)
        with pytest.raises(RuntimeError, match="^asr_audio_cleanup_failed$"):
            asr.extract_audio(video, tmp_path / "output.wav")


# ---------------------------------------------------------------------------
# Whisper 优先从安装目录 models/whisper-large-v3-turbo/ 加载
# ---------------------------------------------------------------------------

# 生成"安装脚本下载好的"普通目录结构（文件构成与 assets_manifest.json 一致）。
def make_whisper_install_dir(models_dir):
    directory = models_dir / "whisper-large-v3-turbo"
    directory.mkdir(parents=True, exist_ok=True)
    for name in asr.WHISPER_FILES:
        (directory / name).touch()
    return directory


# 普通目录优先：模型路径直接指向安装目录，不再走 HF 缓存结构。
def test_whisper_prefers_install_dir_over_hf_cache(tmp_path, monkeypatch):
    install_dir = make_whisper_install_dir(tmp_path)
    make_whisper_cache(tmp_path)  # HF 缓存也在，但应被普通目录抢先。
    calls = []
    fake_model = object()

    def construct(name, **kwargs):
        calls.append((name, kwargs))
        return fake_model

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=construct))
    backend = asr._create_whisper(tmp_path)
    assert backend.model is fake_model
    name, kwargs = calls[0]
    assert name == str(install_dir)
    assert kwargs["local_files_only"] is True
    # 普通目录模式不需要 download_root（没有缓存可查）。
    assert "download_root" not in kwargs


# 安装目录文件不齐（缺 vocabulary.json）：退回 HF 缓存结构。
def test_whisper_falls_back_to_hf_cache_when_install_dir_incomplete(tmp_path, monkeypatch):
    install_dir = make_whisper_install_dir(tmp_path)
    (install_dir / "vocabulary.json").unlink()
    snapshot, _ = make_whisper_cache(tmp_path)
    calls = []
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(
        WhisperModel=lambda name, **kwargs: calls.append(name) or object()))
    asr._create_whisper(tmp_path)
    assert calls == [str(snapshot)]


# 两处都没有：照旧报 asr_model_files_missing。
def test_whisper_missing_everywhere_raises(tmp_path, monkeypatch):
    make_whisper_install_dir(tmp_path)
    import shutil
    shutil.rmtree(tmp_path / "whisper-large-v3-turbo")  # 目录整个删掉
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(
        WhisperModel=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应构造模型"))))
    with pytest.raises(ValueError, match="^asr_model_files_missing$"):
        asr._create_whisper(tmp_path)
