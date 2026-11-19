# 第三方声明 / Third-Party Notices

xhs-wiki 本身采用 [GPL-3.0-or-later](LICENSE) 许可证。本页说明可选的 LLM Wiki 应用和运行时下载组件的来源与许可证；本项目的许可证不替代它们各自的许可证。

## LLM Wiki 应用（用户自行安装）

- **来源**：[nashsu/llm_wiki](https://github.com/nashsu/llm_wiki)。
- **许可证**：GPL-3.0，见[上游许可证](https://github.com/nashsu/llm_wiki/blob/main/LICENSE)。
- **安装方式**：用户自行从[上游发布页](https://github.com/nashsu/llm_wiki/releases)安装 0.6.13 或更新版本；xhs-wiki 不代为下载、安装或更新。
- **分发范围**：本项目不分发 LLM Wiki 的任何文件，包括应用、源码、项目模板和配置；知识库项目由用户在 LLM Wiki 中创建或打开。
- **对接方式**：xhs-wiki 写入项目的来源 Markdown 文件，读取应用的整理记录与状态，不修改 LLM Wiki 的程序或配置内容；详见[技术设计 §6.2](docs/architecture.md#62-llm-wiki-引擎-enginesllmwiki_app进阶)。

## 运行时下载、不随仓库分发的组件

下面这些组件**不包含在本仓库里**，由用户自己的电脑在安装或首次运行时从官方渠道下载，并核对 SHA-256。列在这里，方便你了解它们各自的许可证：

| 组件 | 来源 | 许可证 |
|---|---|---|
| XHS-Downloader 2.8 | [JoeanAmier/XHS-Downloader](https://github.com/JoeanAmier/XHS-Downloader) | GPL-3.0 |
| Whisper large-v3-turbo（CTranslate2 转换版权重） | [dropbox-dash/faster-whisper-large-v3-turbo](https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo)，原始模型 [openai/whisper-large-v3-turbo](https://huggingface.co/openai/whisper-large-v3-turbo) | MIT |
| SenseVoice int8 权重 | [sherpa-onnx 模型发布页](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models)，原始模型 FunAudioLLM/SenseVoice | [FunASR Model License v1.1](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE)（可免费使用、修改和分享，须注明来源并保留模型名） |
| Silero VAD 权重 | [snakers4/silero-vad](https://github.com/snakers4/silero-vad) | MIT |

Python 依赖的许可证见各自的发行包元数据，直接依赖及版本见 [pyproject.toml](pyproject.toml)。
