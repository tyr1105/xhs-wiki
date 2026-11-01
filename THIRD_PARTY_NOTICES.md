# 第三方声明 / Third-Party Notices

xhs-wiki 本身采用 [GPL-3.0-or-later](LICENSE) 许可证。仓库里包含一小段来自其他开源项目的内容，按各自的许可证使用，具体如下。

## LLM Wiki Desktop 的项目模板文字

- **来源**：[StoneLL1/llm-wiki-desktop](https://github.com/StoneLL1/llm-wiki-desktop)，提交 `749e213`
  - `src-tauri/templates/projects/general/purpose.md`
  - `src-tauri/templates/projects/general/schema.md`
  - `src-tauri/src/services/project_service.rs` 里的 starter 文本
- **在本仓库的位置**：`src/xhs_wiki/engines/llmwiki/project.py` 中的 `PURPOSE_MD`、`SCHEMA_MD`，以及 `_starter_index`、`_starter_log`、`_starter_overview` 生成的文字。
- **版权**：Copyright 2026 StoneLL1 and the LLM Wiki Desktop contributors
- **许可证**：Apache License 2.0，全文见 [LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt)。
- **改动**：没有修改，原样保留，以便 LLM Wiki 后端识别项目结构。

## 运行时下载、不随仓库分发的组件

下面这些组件**不包含在本仓库里**，由用户自己的电脑在安装或首次运行时从官方渠道下载，并核对 SHA-256。列在这里，方便你了解它们各自的许可证：

| 组件 | 来源 | 许可证 |
|---|---|---|
| XHS-Downloader 2.8 | [JoeanAmier/XHS-Downloader](https://github.com/JoeanAmier/XHS-Downloader) | GPL-3.0 |
| Whisper large-v3-turbo（CTranslate2 转换版权重） | [dropbox-dash/faster-whisper-large-v3-turbo](https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo)，原始模型 [openai/whisper-large-v3-turbo](https://huggingface.co/openai/whisper-large-v3-turbo) | MIT |
| SenseVoice int8 权重 | [sherpa-onnx 模型发布页](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models)，原始模型 FunAudioLLM/SenseVoice | [FunASR Model License v1.1](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE)（可免费使用、修改和分享，须注明来源并保留模型名） |
| Silero VAD 权重 | [snakers4/silero-vad](https://github.com/snakers4/silero-vad) | MIT |

Python 依赖的许可证见各自的发行包元数据，直接依赖及版本见 [pyproject.toml](pyproject.toml)。
