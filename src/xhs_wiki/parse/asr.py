"""在解析流水线中提取音轨，并用本机 SenseVoice 或 Whisper 生成统一转写。

ASR 是自动语音识别；本模块只接收本地文件，不上传音频或下载模型。
模型工厂、音频解码、音频读取和时钟均可替换，单元测试不需要真实模型。
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
from threading import Lock
import time
from typing import Literal
import wave

import numpy as np


ModelName = Literal["sensevoice", "whisper"]
SAMPLE_RATE = 16000
SENSEVOICE_DIR = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
NUM_THREADS = 4
_clock = time.perf_counter
_MODEL_CACHE: dict[tuple[str, str], object] = {}
_CACHE_LOCK = Lock()
_SENSEVOICE_TAGS = re.compile(r"<\|[^<>]*\|>")


# 标签不是说话内容，统一删除语言、情绪、事件及 ITN 标记，保留原标点。
def _clean_sensevoice_text(text: str) -> str:
    return _SENSEVOICE_TAGS.sub("", text).strip()


# 固定输入格式可以避免把错误采样率的音频当作 16kHz，造成速度和文字失真。
def _read_wav(path: Path) -> np.ndarray:
    try:
        with wave.open(str(path), "rb") as source:
            if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (
                SAMPLE_RATE, 1, 2
            ) or source.getcomptype() != "NONE":
                raise ValueError("asr_audio_format_invalid")
            expected_bytes = source.getnframes() * 2
            data = source.readframes(source.getnframes())
    except ValueError:
        raise
    except (OSError, EOFError, wave.Error):
        raise ValueError("asr_audio_read_failed") from None
    if len(data) != expected_bytes or len(data) % 2:
        raise ValueError("asr_audio_truncated")
    # PCM16 是每个采样点用两个字节存储的有符号整数；模型需要 [-1, 1) 浮点数。
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


class _SenseVoiceBackend:
    """缓存识别器和 VAD；每段音频独立重置状态，并用锁防止跨请求串音。"""

    # VAD（语音活动检测）把长音频分成有人说话的小段，避免整段识别耗尽内存。
    def __init__(self, recognizer: object, vad: object):
        self.recognizer = recognizer
        self.vad = vad
        self.lock = Lock()

    # 时间戳使用 VAD 给出的原始采样位置，不把删除的静音从时间轴中扣掉。
    def transcribe(self, samples: np.ndarray) -> list[dict]:
        segments: list[dict] = []
        with self.lock:
            self.vad.reset()
            try:
                for offset in range(0, len(samples), 512):
                    chunk = samples[offset : offset + 512]
                    if len(chunk) < 512:
                        chunk = np.pad(chunk, (0, 512 - len(chunk)))
                    self.vad.accept_waveform(chunk)
                    self._drain_segments(segments, len(samples))
                # 最后一段可能没有足够长的静音结尾，flush 强制取回这段。
                self.vad.flush()
                self._drain_segments(segments, len(samples))
            finally:
                self.vad.reset()
        return segments

    # 边检测边消费队列，长视频也不会把所有分段音频同时留在 VAD 缓冲区里。
    def _drain_segments(self, segments: list[dict], sample_count: int) -> None:
        while not self.vad.empty():
            speech = self.vad.front
            start = max(0, int(speech.start))
            end = min(sample_count, start + len(speech.samples))
            if end > start:
                stream = self.recognizer.create_stream()
                stream.accept_waveform(SAMPLE_RATE, speech.samples[: end - start])
                self.recognizer.decode_stream(stream)
                text = _clean_sensevoice_text(stream.result.text)
                if text:
                    segments.append({
                        "start": start / SAMPLE_RATE,
                        "end": end / SAMPLE_RATE,
                        "text": text,
                    })
            self.vad.pop()


class _WhisperBackend:
    """把 faster-whisper 的惰性分段迭代器转换为普通字典列表。"""

    # 与 SenseVoice 一样串行使用一个模型，保证同一进程内缓存可安全复用。
    def __init__(self, model: object):
        self.model = model
        self.lock = Lock()

    # 迭代器实际迭代时才做识别，必须在计时和异常处理范围内完整消费。
    def transcribe(self, samples: np.ndarray) -> list[dict]:
        with self.lock:
            items, _ = self.model.transcribe(
                samples, language="zh", beam_size=5, vad_filter=False
            )
            return [
                {"start": float(item.start), "end": float(item.end), "text": item.text.strip()}
                for item in items if item.text.strip()
            ]


# 延迟导入让未启用的候选模型不成为运行时必需依赖，也方便注入替身。
def _create_sensevoice(models_dir: Path) -> _SenseVoiceBackend:
    import sherpa_onnx

    model_dir = models_dir / SENSEVOICE_DIR
    model_path = model_dir / "model.int8.onnx"
    tokens_path = model_dir / "tokens.txt"
    vad_path = models_dir / "silero_vad.onnx"
    if not all(path.is_file() for path in (model_path, tokens_path, vad_path)):
        raise ValueError("asr_model_files_missing")
    # ITN 是逆文本规范化，例如把口述数字整理成数字形式；该接口没有独立标点开关。
    # SenseVoice 的 use_itn 同时启用其内建文本规范化/标点输出，不额外下载标点模型。
    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model_path), tokens=str(tokens_path), sample_rate=SAMPLE_RATE,
        num_threads=NUM_THREADS, provider="cpu", language="zh", use_itn=True,
    )
    config = sherpa_onnx.VadModelConfig()
    config.silero_vad.model = str(vad_path)
    config.silero_vad.min_silence_duration = 0.5
    config.silero_vad.min_speech_duration = 0.25
    config.silero_vad.max_speech_duration = 20.0
    config.sample_rate = SAMPLE_RATE
    config.num_threads = 1
    config.provider = "cpu"
    vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=60)
    return _SenseVoiceBackend(recognizer, vad)


# local_files_only 确保缓存缺失时立即失败，绝不把一次转写变成隐式联网下载。
# 资源安装（assets.py）把模型按清单放在普通目录 models/whisper-large-v3-turbo/
# （文件构成见 src/xhs_wiki/assets_manifest.json）。优先从它加载；找不到
# 再退回 HF 缓存目录结构（开发环境用 assets 之外的下载方式装的）。
WHISPER_DIR = "whisper-large-v3-turbo"
WHISPER_FILES = (
    "model.bin", "config.json", "preprocessor_config.json",
    "tokenizer.json", "vocabulary.json",
)


def _create_whisper(models_dir: Path) -> _WhisperBackend:
    from faster_whisper import WhisperModel

    # 路线 1（安装环境）：普通目录，文件按 assets_manifest.json 逐个下载。
    local_dir = models_dir / WHISPER_DIR
    if all((local_dir / name).is_file() for name in WHISPER_FILES):
        model = WhisperModel(
            str(local_dir), device="cpu", compute_type="int8",
            cpu_threads=NUM_THREADS, local_files_only=True,
        )
        return _WhisperBackend(model)

    # 路线 2（开发环境）：HF 的缓存目录结构（snapshots/<revision>/…）。
    # 模型仓库换过组织名，缓存目录名同时认新旧两个组织名——
    # 先找 dropbox-dash（新），没有再找 mobiuslabsgmbh（旧），
    # 两种缓存布局都能继续用，不逼用户重新下载。
    download_root = models_dir / "faster-whisper"
    cache = None
    for org_dir in (
        "models--dropbox-dash--faster-whisper-large-v3-turbo",
        "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo",
    ):
        candidate = download_root / org_dir
        if (candidate / "refs" / "main").is_file():
            cache = candidate
            break
    if cache is None:
        raise ValueError("asr_model_files_missing")
    # 只解析已下载缓存的 main 引用，完全不调用 Hub 的下载/解析接口。
    # faster-whisper 即使指定 local_files_only，缺 tokenizer 时仍会另行下载备用文件，
    # 所以必须在构造前验证必要文件，并直接传本地快照目录。
    try:
        revision = (cache / "refs" / "main").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise ValueError("asr_model_files_missing") from None
    if not re.fullmatch(r"[a-fA-F0-9]{40}", revision):
        raise ValueError("asr_model_files_missing")
    snapshot = cache / "snapshots" / revision
    if not all((snapshot / name).is_file() for name in (
        "model.bin", "config.json", "tokenizer.json", "vocabulary.json"
    )):
        raise ValueError("asr_model_files_missing")
    model = WhisperModel(
        str(snapshot), device="cpu", compute_type="int8",
        cpu_threads=NUM_THREADS, download_root=str(download_root),
        local_files_only=True,
    )
    return _WhisperBackend(model)


# 单一工厂是测试替换真实模型的边界；公开入口先验证模型名称。
def _create_backend(model: ModelName, models_dir: Path) -> object:
    if model == "sensevoice":
        return _create_sensevoice(models_dir)
    return _create_whisper(models_dir)


# 缓存同时包含模型类型和规范化目录，切换模型仓库不会误用旧实例。
def _get_backend(model: ModelName, models_dir: Path) -> tuple[object, float]:
    if model not in ("sensevoice", "whisper"):
        raise ValueError("asr_model_invalid")
    directory = Path(models_dir).resolve()
    key = (model, str(directory))
    with _CACHE_LOCK:
        if key in _MODEL_CACHE:
            return _MODEL_CACHE[key], 0.0
        started = _clock()
        try:
            backend = _create_backend(model, directory)
        except ValueError as error:
            # 模型文件缺失（_create_* 抛 asr_model_files_missing）是最常见、
            # 也最需要让用户看懂的原因（实测遇到过 20 条全部
            # 解析失败，真正原因是"所选的语音模型没有安装"，但旧报错统一是
            # asr_failed，完全看不出来）。单独给它一个稳定代码；同样不带
            # 原始异常文本（可能含本机路径）。
            if str(error) == "asr_model_files_missing":
                raise RuntimeError("asr_model_missing") from None
            raise RuntimeError("asr_model_load_failed") from None
        except Exception:
            # 第三方异常可能包含路径或下载地址，只向调用方传递稳定错误代码。
            raise RuntimeError("asr_model_load_failed") from None
        seconds = _clock() - started
        _MODEL_CACHE[key] = backend
        return backend, seconds


# 基准试验预热入口：返回本次冷加载时间，已缓存时返回零，不重复初始化模型。
def load_model(model: ModelName, models_dir: Path | str) -> float:
    return _get_backend(model, Path(models_dir))[1]


# seconds 是这次调用的实际墙钟耗时，包含音频读取和推理；未预热时也包含加载。
def transcribe(
    wav_path: Path | str, *, model: ModelName, models_dir: Path | str
) -> dict:
    if model not in ("sensevoice", "whisper"):
        raise ValueError("asr_model_invalid")
    started = _clock()
    samples = _read_wav(Path(wav_path))
    if len(samples) == 0:
        return {"text": "", "segments": [], "model": model, "seconds": _clock() - started}
    backend, _ = _get_backend(model, Path(models_dir))
    try:
        segments = backend.transcribe(samples)
    except Exception:
        raise RuntimeError("asr_transcription_failed") from None
    return {
        "text": "".join(segment["text"] for segment in segments),
        "segments": segments,
        "model": model,
        "seconds": _clock() - started,
    }


# 只给新建的输出目录设置私有权限，不改变调用方已经存在的共享父目录。
def _make_private_parents(directory: Path) -> None:
    missing: list[Path] = []
    cursor = directory
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for path in reversed(missing):
        path.mkdir(mode=0o700, exist_ok=True)
        path.chmod(0o700)


# PyAV 是自带媒体解码库的 Python 包，无需系统安装 ffmpeg。
# 先区分「没有音轨」与「音轨解码失败」：前者返回空采样，沿用 transcribe
# 的空 WAV 分支，不加载模型，最终由 package 标记 audio_transcript_empty。
def _decode_audio(video_path: Path) -> np.ndarray:
    import av

    # 捕获并丢弃底层解码日志，避免其中的本机路径进入程序日志或错误页。
    with av.logging.Capture():
        with av.open(str(video_path), mode="r") as container:
            if not container.streams.audio:
                return np.empty(0, dtype=np.float32)
            # 直接用 PyAV 的公开接口解码第一条音轨，再用重采样器统一成
            # 16kHz、单声道、16 位整数（任意原始采样率和声道布局都适用）。
            # 不借用 faster-whisper 的 decode_audio：它在新版 PyAV 上会因为
            # 已删除的参数报错，而这里的写法在 PyAV 15 和 19 上都能用（不同
            # macOS 版本会装到不同版本的 PyAV）。
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            chunks = []
            for frame in container.decode(container.streams.audio[0]):
                for converted in resampler.resample(frame):
                    chunks.append(converted.to_ndarray().reshape(-1))
            # 传 None 冲洗重采样器内部缓存的最后一点采样，避免结尾被截掉。
            for converted in resampler.resample(None):
                chunks.append(converted.to_ndarray().reshape(-1))
    if chunks:
        samples = np.concatenate(chunks).astype(np.float32) / 32768.0
    else:
        samples = np.empty(0, dtype=np.float32)
    # 有音轨却没有解码出采样属于损坏，不能冒充「原本没有声音」。
    if samples.ndim != 1 or not samples.size or not np.isfinite(samples).all():
        raise RuntimeError("audio_extract_failed")
    return samples


# 同目录临时文件加原子替换，保证提取失败时不会覆盖已有音轨或留下半成品。
# ffmpeg 关键字仅为兼容旧调用方保留；现在不查找或执行任何外部媒体程序。
def extract_audio(
    video_path: Path | str, wav_path: Path | str, *, ffmpeg: str = "ffmpeg"
) -> None:
    video_path, wav_path = Path(video_path), Path(wav_path)
    if not video_path.is_file():
        raise ValueError("asr_video_missing")
    if video_path.resolve() == wav_path.resolve():
        raise ValueError("asr_audio_same_path")
    temporary: Path | None = None
    try:
        _make_private_parents(wav_path.parent)
        descriptor, name = tempfile.mkstemp(prefix=".asr-", suffix=".wav", dir=wav_path.parent)
        temporary = Path(name)
        os.close(descriptor)
        samples = _decode_audio(video_path.resolve())
        # PCM16 每个采样占两个字节；先限幅再转成小端有符号整数，防止
        # +1.0 溢出为负值。空采样写出合法的零帧 WAV，供现有转写流程识别。
        pcm = np.clip(np.rint(samples * 32768.0), -32768, 32767).astype("<i2")
        with wave.open(str(temporary), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(SAMPLE_RATE)
            output.writeframes(pcm.tobytes())
        temporary.chmod(0o600)
        os.replace(temporary, wav_path)
    except Exception:
        # 解码库、导入和写盘错误只暴露稳定代码，原异常可能含私密路径。
        raise RuntimeError("audio_extract_failed") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                raise RuntimeError("asr_audio_cleanup_failed") from None
