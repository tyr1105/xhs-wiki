# 更新记录 / Changelog

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-11-02

第一个公开版本。

### 新增
- macOS 一键安装脚本：只需要 Google Chrome，不需要 Homebrew、ffmpeg、git 或 Xcode 命令行工具；装好本体后马上启动服务、打开本机控制台；下载组件和语音模型在后台进行，控制台顶部显示进度，支持断点续传。
- 国内网络：安装时加 `--mirror cn`，Python 包、Python 安装包和语音模型改从国内镜像下载。
- 本机网页控制台和 5 步首次设置向导：知识库位置、引擎、整理模型（可测试）、扫码登录小红书、首次导入范围。
- 每天定时（默认北京时间 03:00）自动发现新收藏，每次运行最多新增 100 条（可在 1–200 之间修改）；也可以随时手动运行、暂停、继续。
- 本机处理：用 macOS 自带的 Vision 识别图片文字；用 Whisper large-v3-turbo（默认）或 SenseVoice（快速）转写视频语音。
- 整理模型四选一：Claude Code、Codex、Anthropic API、OpenAI 兼容接口；API Key 保存在 macOS 钥匙串。
- 轻量 Markdown 知识库引擎：生成索引、主题页和来源页，Obsidian 或任意编辑器都能打开；内容不足的笔记标为"暂缓"。
- Claude Code Skill：提问时先查你自己的知识库，回答附出处（来源笔记的标题和链接）；Skill 要求查不到时如实说明，不编造。
- 原件核验后自动清理：入库和完整性核验都通过后，才删除下载的图片和视频。
- 遇到限制就停：扫描收藏或扫码登录遇到验证码、访问受限会立即停下；下载时同一轮连续 3 条失败会暂停本轮下载并提示，等下一次定时运行再试。
- 整理提示词明确要求模型把笔记内容只当资料，不执行其中的指令。
- 控制台访问认证：只有当前 macOS 用户能打开控制台（运行 `xhs-wiki open`，用一次性登录码完成认证）。

### 已知限制
- 只支持 macOS（13 或更新版本，Apple 芯片和 Intel）。
- LLM Wiki 引擎的配套程序尚未发布，向导里显示"即将提供"。
- 首次导入大量收藏时，语音转写比较耗时，会分几天完成。

[0.1.0]: https://github.com/tyr1105/xhs-wiki/releases/tag/v0.1.0
