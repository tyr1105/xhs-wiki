"""媒体链路回归测试：在临时目录合成视频，验证 PyAV 解码和解析覆盖标记。

本文件只编码虚构画面和正弦波，不下载素材、不运行外部媒体程序、不加载模型。
生成函数也供现场验收复用，确保演示与回归使用同一种可重复的合成素材。
"""

from fractions import Fraction
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import wave

import av
import numpy as np
import pytest

from xhs_wiki.download import _default_ffprobe
from xhs_wiki.parse import asr, package


# MOV 可保存未压缩 PCM 音轨，避免有损编码附加的填充采样干扰时长断言。
# 画面和声音均由数组现场生成，文件关闭时才写完整容器索引，截断可被发现。
def make_video(path: Path, *, with_audio: bool, seconds: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with av.open(str(path), mode="w", format="mov") as container:
        video = container.add_stream("mpeg4", rate=12)
        video.width, video.height, video.pix_fmt = 96, 64, "yuv420p"
        audio = None
        if with_audio:
            audio = container.add_stream("pcm_s16le", rate=48000)
            audio.layout = "stereo"
        rng = np.random.default_rng(4)
        for index in range(seconds * 12):
            pixels = rng.integers(0, 256, (64, 96, 3), dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
            frame.pts = index
            for packet in video.encode(frame):
                container.mux(packet)
        for packet in video.encode():
            container.mux(packet)
        if audio is not None:
            for offset in range(0, seconds * 48000, 4000):
                ticks = np.arange(offset, offset + 4000) / 48000
                tone = np.rint(np.sin(2 * np.pi * 440 * ticks) * 12000).astype(np.int16)
                # 打包立体声左右交替排列，解码时需要重采样且合并成一个声道。
                stereo = np.repeat(tone, 2).reshape(1, -1)
                frame = av.AudioFrame.from_ndarray(stereo, format="s16", layout="stereo")
                frame.sample_rate, frame.time_base, frame.pts = 48000, Fraction(1, 48000), offset
                for packet in audio.encode(frame):
                    container.mux(packet)
            for packet in audio.encode():
                container.mux(packet)
    path.chmod(0o600)
    return path


# 用合成素材构造下载回执，走真实 package 流程，检验无音轨最终落盘的 coverage。
def package_silent_video(root: Path) -> dict:
    video = make_video(root / "media" / "silent.mov", with_audio=False)
    result = {"status": "ok", "media_kind": "video", "meta": {"title": "合成测试"},
              "media": [{"path": "media/silent.mov", "bytes": video.stat().st_size,
                         "sha256": hashlib.sha256(video.read_bytes()).hexdigest()}]}
    return package.build_package("ab" * 12, root, result, models_dir=root / "models")


# 验证真实重采样：48kHz 立体声变成 16kHz 单声道 PCM16，时长精确到一个采样点。
def test_generated_video_extracts_expected_wave(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    video = make_video(tmp_path / "video with spaces.mov", with_audio=True)
    output = tmp_path / "audio" / "output.wav"
    asr.extract_audio(video, output)
    with wave.open(str(output), "rb") as stream:
        assert (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) == (16000, 1, 2)
        assert stream.getnframes() / stream.getframerate() == pytest.approx(2, abs=1 / 16000)
        samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2")
        assert np.max(np.abs(samples)) > 1000
    assert output.stat().st_mode & 0o777 == 0o600
    assert output.parent.stat().st_mode & 0o777 == 0o700


# 无音轨从提取、转写到 package 全程成功，且模型工厂绝不被触及。
def test_generated_silent_video_marks_empty_transcript_without_model(tmp_path, monkeypatch):
    def forbid_model(*args, **kwargs):
        pytest.fail("无音轨不应加载模型")

    monkeypatch.setattr(asr, "_get_backend", forbid_model)
    result = package_silent_video(tmp_path)
    assert result["coverage"] == "audio_transcript_empty"
    document = json.loads((tmp_path / "package.json").read_text())
    assert document["audio_transcript"] == ""
    assert document["coverage"] == "audio_transcript_empty"
    assert not list(tmp_path.glob(".package-audio-*"))


# 正常视频可以读出首帧，截断掉一半的普通 MOV 丢失索引，必须校验失败。
@pytest.mark.parametrize("with_audio", [True, False])
def test_generated_video_and_half_truncation(tmp_path, with_audio):
    video = make_video(tmp_path / "complete.mov", with_audio=with_audio)
    truncated = tmp_path / "truncated.mov"
    data = video.read_bytes()
    truncated.write_bytes(data[:len(data) // 2])
    assert _default_ffprobe(video) is True
    assert _default_ffprobe(truncated) is False
    with pytest.raises(RuntimeError, match="^audio_extract_failed$"):
        asr.extract_audio(truncated, tmp_path / "invalid.wav")
    assert not (tmp_path / "invalid.wav").exists()


# 真正存在的音轨却一个采样都没解出来，属于损坏，不能冒充「没有音轨」。
# 用一个什么都不输出的假重采样器模拟这种情况。
def test_audio_stream_without_samples_is_extraction_failure(tmp_path, monkeypatch):
    video = make_video(tmp_path / "complete.mov", with_audio=True)

    class SilentResampler:
        def __init__(self, **kwargs):
            pass

        def resample(self, frame):
            return []

    monkeypatch.setattr(av, "AudioResampler", SilentResampler)
    with pytest.raises(RuntimeError, match="^audio_extract_failed$"):
        asr.extract_audio(video, tmp_path / "invalid.wav")
    assert not (tmp_path / "invalid.wav").exists()


# 解码只用 PyAV 的公开接口，不依赖 faster-whisper 的 decode_audio：
# 即使它不可用（例如与新版 PyAV 不兼容而报错），提取音频照常成功。
def test_extraction_does_not_depend_on_faster_whisper_decoder(tmp_path, monkeypatch):
    video = make_video(tmp_path / "complete.mov", with_audio=True)

    def broken_decode_audio(*args, **kwargs):
        raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")

    monkeypatch.setitem(sys.modules, "faster_whisper.audio", SimpleNamespace(decode_audio=broken_decode_audio))
    output = tmp_path / "audio.wav"
    asr.extract_audio(video, output)
    with wave.open(str(output), "rb") as stream:
        assert (stream.getframerate(), stream.getnchannels()) == (16000, 1)
        assert stream.getnframes() / stream.getframerate() == pytest.approx(2, abs=1 / 16000)


# 解码过程中的任何异常（这里用会抛错的假重采样器模拟）都只暴露稳定的错误代码，
# 不带原始异常文本，也不留下半成品文件。
def test_decoder_errors_become_stable_code(tmp_path, monkeypatch):
    video = make_video(tmp_path / "complete.mov", with_audio=True)

    class BrokenResampler:
        def __init__(self, **kwargs):
            pass

        def resample(self, frame):
            raise ValueError("synthetic_private_detail /Users/someone/secret")

    monkeypatch.setattr(av, "AudioResampler", BrokenResampler)
    output = tmp_path / "audio.wav"
    with pytest.raises(RuntimeError, match="^audio_extract_failed$") as caught:
        asr.extract_audio(video, output)
    assert "synthetic_private_detail" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert not output.exists()
