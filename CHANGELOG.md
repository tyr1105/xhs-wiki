# 更新记录 / Changelog

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [0.2.2] - 2026-11-03

### 修复
- 选 LLM Wiki 引擎时，查询 Skill 里的索引、主题页和来源页位置改成这个引擎的真实目录（`wiki/index.md`、`wiki/`、`wiki/sources/`）。以前 Skill 写死了轻量引擎的 `索引.md`、`主题/`、`来源/`，在 Claude Code 里提问时会去不存在的目录里找，可能把收藏过的内容答成"没有收藏过"。现在 Skill 里的位置由当前引擎给出，轻量引擎生成的 Skill 和以前一字不差。
- 新建 LLM Wiki 知识库时一并建好 `wiki/sources/`（后台导入来源页的位置），第一次导入之前 Skill 指向的目录也存在。
- README 中英文改正"选 Claude Code 整理时关闭全部工具"的说法：这只对默认的轻量引擎成立。LLM Wiki 引擎由后台调用 Claude Code，需要时只开读文件和搜索这几个只读工具，范围限定在这次整理的临时工作目录。现在写的是两种引擎都成立的说法：读不到整理用的工作目录以外的文件，不加载你的 MCP 和 Skill，会话也不保存到本机。

### 说明
- 已经在用 LLM Wiki 引擎的：重新运行安装命令升级后，Skill 会按新位置自动重写，不用手动改。

## [0.2.1] - 2026-11-02

### 变更
- LLM Wiki 引擎整理出的页面固定用简体中文写，不再跟随 LLM Wiki 桌面版的界面语言；没装桌面版、或桌面版是英文界面，也会得到中文页面。xhs-wiki 启动后台时传 `LLM_WIKI_CONTENT_LANGUAGE=zh-CN`，不改桌面版的设置。

### 说明
- 后台程序换成 [tyr1105/llm-wiki-desktop](https://github.com/tyr1105/llm-wiki-desktop) 的 `headless-v2.0.6-2`（新增上面这个环境变量），由 GitHub Actions 构建，附件 SHA-256 `0d04228f53e915ff6b8e425695da8bbb9b6aa31c85caff49fd3d0af475ba7c81`。
- 已经装过 v0.2.0 后台的：升级后服务会自动下载新版后台（约 18 MB），下载完成前自动整理会等待。旧版在 `~/Library/Application Support/xhs-wiki/vendor/llm-wiki-headless-2.0.6-1-aarch64-apple-darwin/`，不再使用，可以手动删除。
- 升级前已经生成的英文页面不会被自动翻译；之后的整理按中文写，已有的英文页面被更新时可能出现中英混排。

## [0.2.0] - 2026-11-02

### 新增
- LLM Wiki 进阶引擎接入首次向导：仅 Apple 芯片，按需下载无窗口后台，显示进度和中文原因；就绪后自动健康检查，健康才保存选择。
- `xhs-wiki assets install-llmwiki` 下载、校验并自动配置后台路径；`doctor` 检查后台文件及所选 Claude Code / Codex 命令。
- 后台压缩包和程序本体分别按固定 SHA-256 校验，复用系统 curl 断点续传；失败不留下可用的半成品。

### 修复
- 守护进程与运行进程共用引擎工厂；向导健康检查结束后关闭后台，不遗留进程。
- 后台启动前自动生成所选提供方的包装脚本，支持只安装 Claude Code 或只安装 Codex；从系统账户读取 HOME/USER，安全引用路径与模型名。
- Claude 支持时在打印模式整理调用中启用 `--safe-mode` 和 `--no-session-persistence`，后台已传入的参数不重复追加；向导只允许 LLM Wiki 使用 Claude Code 或 Codex，并在完成设置时再次核对。
- LLM Wiki 的非必需资源不会阻止轻量引擎运行。

- 真机联调修正：导入后从提交任务结果里读取来源编号（后台的导入条目本身不带编号）；包装脚本写入 claude 的链接路径而不是带版本号的文件，Claude Code 升级后不会失效。

### 说明
- 后台程序：[tyr1105/llm-wiki-desktop](https://github.com/tyr1105/llm-wiki-desktop) 的 `headless-v2.0.6-1`，由 GitHub Actions 构建，附件 SHA-256 `bc2d18a71ea5416b6ce01a88bab102da0d34db018ff60bd92506a782bdcd50e9`。
- 整理出的页面语言跟随 LLM Wiki 桌面版的界面语言；没装桌面版时默认英文。

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
