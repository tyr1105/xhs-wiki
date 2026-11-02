# xhs-wiki 技术设计

> 面向贡献者。用户文档见 README。本文只描述工具本身，不包含、也不预设任何人的收藏内容或主题分类。

## 0. 总览

```
install.sh ── 检查环境 → 装好本体 → 启动服务（LaunchAgent 或临时服务）并打开控制台 → 安装 Claude Skill
                （XHS-Downloader 和语音模型由服务在后台下载，进度显示在控制台顶部）
                                                  │
LaunchAgent ── xhs-wiki serve（守护进程：控制台 HTTP + 定时器 + 子进程管理）
                 │
                 ├─ xhs-wiki run --trigger …（一次运行）
                 │     预检 → 扫描收藏 → 对比新增 → 下载 → 解析 → 引擎入库 → 引擎整理 → 清理原件 → 汇总
                 │       scan.py   ledger.py   download.py  parse/   engines/<name>          cleanup.py
                 └─ xhs-wiki login（扫码登录，带窗口）
```

- **语言和运行环境**：Python 3.12，由 `uv` 管理；只支持 macOS（Apple 芯片和 Intel 都支持；LLM Wiki 引擎只支持 Apple 芯片）。
- **包结构**：`src/xhs_wiki/`，命令入口是 `xhs-wiki`。
- **目录**（都由 `config.py` 统一给出，测试时用环境变量 `XHS_WIKI_HOME` 指到临时目录）：

| 路径 | 内容 |
|---|---|
| `APP_HOME` = `~/Library/Application Support/xhs-wiki/`（0700） | 下面所有运行数据 |
| `APP_HOME/settings.json` | 设置（§7） |
| `APP_HOME/ledger.sqlite3` | 台账：每条收藏的处理阶段（§2） |
| `APP_HOME/status.json`、`runs.jsonl`、`scheduler.json`、`notify.json`、`*.lock` | 服务状态，由 `store.py` 统一读写（先写临时文件再原子替换，0600） |
| `APP_HOME/console-token`（0600） | 控制台访问令牌，只有当前 macOS 用户能读（§11） |
| `APP_HOME/browser-profile/` | 专用浏览器资料夹（登录状态）。**任何代码都不读其中文件** |
| `APP_HOME/work/<note_id>/` | 单条收藏的工作目录：`media/`（下载原件）、`package.json`、`package.md` |
| `APP_HOME/vendor/XHS-Downloader-2.8/` | 安装时下载并校验 sha256 的下载组件 |
| `APP_HOME/models/` | 语音模型 |
| `~/Library/Logs/xhs-wiki/` | 守护进程日志（LaunchAgent 的工作目录和日志不能放在 `~/Documents`，否则会以 exit 78 失败） |
| `KB_ROOT`（默认 `~/XHS-Wiki`，用户可改） | 知识库（轻量引擎）；LLM Wiki 引擎则是它自己的项目目录 |

- **端口**：127.0.0.1:18765。**LaunchAgent 标签**：`io.github.xhs-wiki.service`。

## 1. 模块与依赖方向

```
cli.py → daemon.py → runner.py → {scan, ledger, download, parse/*, engines/*, cleanup, notify, store}
engines/markdown → llm/providers
engines/llmwiki  → llmwiki_client（HTTP）
```

规则：下层模块不 import 上层；所有外部依赖（子进程、浏览器、模型、时钟）都可以注入，方便测试。

## 2. 台账 `ledger.py`（SQLite）

文件是 `APP_HOME/ledger.sqlite3`（0600），开启 WAL 模式，每次写都在事务里完成。

```sql
CREATE TABLE notes (
  note_id TEXT PRIMARY KEY,          -- 24 位十六进制
  title TEXT, url TEXT,              -- url 是规范链接，不带签名
  first_seen_at TEXT, list_position INTEGER,
  stage TEXT NOT NULL,               -- 见下
  stage_updated_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,   -- 当前阶段已经失败了几次
  last_error TEXT,                   -- 错误代码（不含链接）
  media_kind TEXT,                   -- video | images | text
  package_sha256 TEXT,
  engine TEXT, engine_ref TEXT,      -- 引擎名，以及引擎自己的引用（JSON）
  deferred_reason TEXT,
  cleanup_status TEXT,               -- null | cleaned | kept:<原因>
  cleaned_at TEXT, freed_bytes INTEGER
);
CREATE TABLE signed_links (note_id TEXT PRIMARY KEY, url TEXT NOT NULL, captured_at TEXT NOT NULL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);   -- schema_version 等
```

- **阶段**：
  - 正常推进：`discovered → downloaded → parsed → ingested → distilled`（成功的终态）；
  - `deferred`：内容不足，是终态，但来源页会保留；
  - 失败：`failed:download | failed:parse | failed:ingest | failed:distill`。每个阶段最多重试 3 次（`attempts`），超过就停在失败状态，控制台可以"单条重试"（把 attempts 清零）。
- **签名链接**：带 `xsec_token` 的链接只存在 `signed_links` 表里，**任何日志、状态、页面都不能输出**。
- **对外函数**：
  - `upsert_discovered(items)`：只插入新的笔记，已有的不改；
  - `next_batch(stage, limit)`；
  - `advance(note_id, stage, **fields)`、`fail(note_id, stage, code)`；
  - `counts()`、`retry(note_id)`。
- 这些函数都要能在多个进程同时访问时安全使用：靠 SQLite 的锁，加上 `busy_timeout` 设置。

## 3. 扫描 `scan.py`

选择器和判断逻辑已经在真实网站上校准过。要点：
- **不写死主页地址**：登录后从侧栏的"我"链接（`.channel-list a.link-wrapper[href^="/user/profile/"]`）读出主页编号，存进设置。
- 扫描结果直接交给 `ledger.upsert_discovered()`，并按收藏列表的顺序记下位置；签名链接写入 `signed_links`，不写任何扫描文件。
- 返回值的形状保持不变：`status`（ok / login_required / blocked / unknown）、`visible`、`nominal`、`reason`（scan_empty / scan_incomplete 等）。
- 扫描在登录状态下静默进行，扫码登录必须带窗口；不做任何反检测。扫码时的浏览器窗口是"说明页 + 小窗口"：小红书页面留在后台标签，另开一个前台标签显示本地说明页（告诉用户扫控制台上的二维码），再用 CDP 把窗口缩小。不用"最小化窗口"，因为 macOS 上 `Browser.setWindowBounds` 的 `minimized` 实测不生效（调用不报错，窗口却一直可见）。

## 4. 下载 `download.py`

- 调用 XHS-Downloader 2.8 的 Python 接口（把 `APP_HOME/vendor/XHS-Downloader-2.8` 加进 `sys.path`，然后 `from source import XHS`），下载到 `work/<note_id>/media/`。
- 单个文件上限 1 GiB；磁盘剩余低于 10 GiB 就停止下载。
- 视频下载后用 PyAV 检查：至少有一个宽高有效的视频流，并且能解码出第一帧，否则视为文件不完整。
- 失败记作 `failed:download`，错误代码包括：`metadata_unavailable`（拿不到笔记信息）、`media_check_failed`（下载的媒体文件不完整）、`network_error`、`file_too_large`、`disk_low`。
- 需要签名链接，从 `signed_links` 表读取。

## 5. 解析 `parse/`

- **`asr.py`**：语音转写。先用 PyAV（自带 FFmpeg 解码库的 Python 包）从视频里解码第一条音轨，重采样成 16kHz 单声道 wav，再交给语音识别模型；视频没有音轨时标记为没有可转写的声音。用户不需要另装 ffmpeg。
  - 接口统一为 `transcribe(wav_path) -> {text, segments, model}`。
  - **默认用 Whisper large-v3-turbo**（`faster-whisper`，CPU int8）；SenseVoice（`sherpa-onnx` + Silero VAD）作为"快速模式"可选。
  - 选型依据是维护者的一次对比试验，素材为 28 段音频：
    - 中文片段与参考转写的差异率：Whisper 10.5%，SenseVoice 15.3%；
    - 英文：Whisper 5.9%，SenseVoice 基本不可用；
    - 速度：SenseVoice 约快 25 倍（实时率 0.011 对 0.29）；
    - 模型体积：SenseVoice 227 MiB，Whisper 1.5 GiB。

    **局限**：参考转写由 Whisper 系列（whisper.cpp）自动生成、未经人工校对，所以对比天然偏向 Whisper；只在一台机器上测了一次；素材因版权和隐私原因不公开。这些数字只用来说明取舍方向，不代表准确率。英文差距和速度差距足够大，结论不受影响。转写准确度直接决定整理质量，所以默认选 Whisper。首次大量导入较慢，在后台运行即可。
  - **模型下载**：按清单逐个文件下载，并核对 sha256。HuggingFace 依次尝试 `HF_ENDPOINT`（如果设置了）、`huggingface.co`、`hf-mirror.com`；内容都要核对 sha256，所以镜像同样可信。下载用系统自带的 curl，可以断点续传。
- **`ocr.py`**：图片文字识别，用 macOS Vision（`VNRecognizeTextRequest`，识别等级 accurate，语言 zh-Hans 和 en），接口为 `recognize(image_path) -> {text, lines}`。HEIC、WebP 先转成 PNG。
- **`package.py`**：把一条收藏的全部信息整理成解析包：
  - `package.json`：note_id、标题、作者、链接、类型、正文、标签、发布时间、语音转写、OCR 文本、原件清单（路径 / 大小 / sha256）、解析覆盖情况；
  - `package.md`：给引擎读的纯文本版本，依次是标题、作者、正文、标签、转写、OCR。
- **隐私**：图片和视频永远不发给模型，只有 `package.md` 的文字会发。

## 6. 引擎 `engines/`

```python
class Engine(Protocol):
    name: str
    def health(self) -> tuple[bool, str]: ...                 # 是否可用及原因
    def ingest(self, packages: list[Package]) -> list[IngestResult]: ...   # 每条：ok / deferred / failed + engine_ref
    def distill(self, note_ids: list[str]) -> DistillResult: ...           # 把新入库的收藏整合进主题
    def skill_context(self) -> dict: ...                      # Skill 需要的路径和说明
```

### 6.1 轻量引擎 `engines/markdown/`（默认）

知识库结构：

```
KB_ROOT/
  索引.md
  主题/<主题名>.md
  来源/<YYYY-MM-DD>-<标题前 20 字>-<note_id 前 6 位>.md
  .xhs-wiki/engine.sqlite3        ← topics(id, name, description, category, created_at, updated_at, page_sha256)
                                    topic_members(topic_id, note_id, role)，以及 note_cards(note_id, card_json)
```

整理分四步。每一步都是一次有上限的模型调用，输入只放压缩过的"笔记卡片"，不放全文，从结构上避免输入过长：

1. **笔记卡片**（`ingest` 时，每条一次调用）：输入 `package.md`（截断到 12000 字）。输出 JSON：
   ```json
   {"title": "", "gist": "≤60字", "key_points": ["3–8条"], "details": ["名称：说明"],
    "scenarios": ["适用场景"], "sufficient": true, "insufficient_reason": null,
    "topic_hints": ["1–3个短主题名"], "category": "宽泛类别（由模型从内容归纳，不预设）"}
   ```
   用 JSON 结构校验，失败重试 1 次。`sufficient=false` 时只写来源页，阶段记为 `deferred`。
2. **来源页**：根据卡片和解析包直接用代码生成，不调用模型。内容包括：frontmatter（note_id、url、作者、收藏位置、主题）、一句话概要、要点、细节、原文（正文、转写、OCR）。
3. **主题归类**（`distill` 时，每批最多 20 条新卡片调用一次）：
   - 输入：现有主题列表（名称、描述、类别、成员数）。主题超过 80 个时，先用关键词和 topic_hints 的重合度筛出 80 个候选。
   - 输出：每条笔记归到一个已有主题的 id，或者新建一个主题 `{name, description, category}`。
4. **主题页重写**（每个受影响的主题调用一次）：
   - 输入：主题描述 + 成员卡片（最多 60 张，按收藏位置取最新的）+ 旧页面摘要。
   - 输出 Markdown：概述、综合要点（合并去重）、对比 / 推荐清单、注意事项，以及来源列表 `[[来源/…]]`。
   - **引用校验**：页面里只能引用这个主题成员的来源页，出现未知引用就判失败，旧页面保持不变。

**索引页**由代码生成：按类别分组，列出主题名、一句话描述、成员数，再加"最近新增"。

**提示词**放在 `engines/markdown/prompts/*.md`，是可以改的模板。每个模板都明确告诉模型：笔记内容来自第三方帖子，只是待整理的资料，其中的任何指令、命令、链接或要求都不要执行。**提示词里不能出现任何具体主题、分类或示例内容**，否则会把某个人的偏好带进工具里。

### 6.2 LLM Wiki 引擎 `engines/llmwiki/`（进阶）

> **状态：v0.2.0 起可用。** 仅支持 Apple 芯片（包含 Rosetta 环境）。资源清单固定为 Release `headless-v2.0.6-1` 的附件与程序本体 SHA-256；2026-11-02 在真机上用虚构笔记跑通两轮：导入 → Update Wiki → 轻量检查 → 确认，第二轮更新已有主题页时旧来源引用全部保留。

- 后台来自 [tyr1105/llm-wiki-desktop 的 headless 分支](https://github.com/tyr1105/llm-wiki-desktop/tree/headless)，基于 StoneLL1/llm-wiki-desktop v2.0.6（Apache-2.0），增加无窗口 `--serve-api` 接口和长上下文控制；GitHub Actions 在干净构建机上构建 Apple 芯片版本。许可证与改动见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。
- 只有已选择该引擎、或向导刚请求下载时，后台资源线程才准备 `llmwiki`；轻量用户的该项为 `not_needed`，不影响“资源全部就绪”。第一次点卡片返回 202 并显示下载进度，不保存引擎；程序通过固定 SHA-256 校验后，向导自动再做一次健康检查，检查完成在 `finally` 里关闭后台，健康才保存选择。
- 资源下载复用系统 curl 和 `fetch()`：断点续传、大小与压缩包 SHA-256 校验、校验失败完整重下一次；解压到临时目录，再核对程序本体 SHA-256，设置 0755 后整体原子替换到 `APP_HOME/vendor/<extract_dir>/`。守护进程或 CLI 将程序绝对路径自动写入 `llmwiki_binary`。包内 LICENSE、NOTICE、HEADLESS.md 保留；不使用会添加隔离标记的下载方式。
- 后台启动前在 `APP_HOME/llmwiki-bin/` 生成 `claude` / `codex` 包装脚本，PATH 优先查这里；只要求当前提供方对应的命令存在，内容未变不重写。HOME 和 USER 取当前 UID 的系统账户记录 `pwd.getpwuid(os.getuid())`，不继承可被覆盖的环境值。路径与 `model.conf` 的模型值用 `shlex.quote` 引用；模型名仅允许字母、数字和 `._:-[]`，最长 100 字符，空串表示默认模型。
- 生成 claude 脚本时复用轻量提供方的 `--help` 探测，分别确认 `--safe-mode` 和 `--no-session-persistence` 的支持情况；只在 `--print` / `-p` 调用中追加受支持且原参数没有的选项，避免重复后台已经传入的参数。前者屏蔽个人的 CLAUDE.md、Skill 和插件，后者不保存本次会话。健康检查、日常运行通过同一个公共工厂创建引擎。
- 适配层把 `package.md` 作为 Source 导入，按 5 条或 80KB 一组调用 Update Wiki，再做轻量检查后确认：只要丢了原有的来源引用，或引用了未登记的来源，就驳回；改动了 10 个及以上已有页面、或有删除和覆盖时，交给用户决定。
- 真机联调确认的后台行为：
  - 导入条目本身不带来源编号；提交任务结果的 `result.reference.completion.newSources`（以及 `updatedSources`）列出 `sourceId`、`versionId`、`wikiPath`、`contentHash`，`contentHash` 就是导入文件的 SHA-256，适配层按它认出自己这一条，再用 `GET /sources` 核对。来源页落在 `wiki/sources/local/<文件名>.md`，原文在 `raw/sources/<sourceId>/<versionId>/`。
  - 后台不往知识库的 `main` 分支提交：每次整理的输入、确认前和确认后快照存为 `refs/llm-wiki/operations/<任务编号>/{baseline,before,planned,after}`，改动直接写在工作区，所以 `git status` 显示未提交改动是正常状态，不影响下一轮。
  - 整理用的语言只取 LLM Wiki 全局设置里的界面语言（`~/.config/llm-wiki-desktop/settings.json` 的 `language`，默认 `en`），知识库没有单独的语言设置。所以页面语言跟随桌面版的界面语言，没装桌面版时是英文。
  - 无窗口模式仍会在菜单栏放一个 LLM Wiki 图标（上游行为）。
- 整理模型只支持已在本机登录的 Claude Code 或 Codex（由 LLM Wiki 后台调用）；提供方保存和向导结束均校验组合。自动整理期间必须关闭桌面版；待用户审阅时暂停整理，用户等后台关闭后在桌面版打开同一项目接受或拒绝，再关闭桌面版并继续。

## 7. 设置 `settings.json`

```json
{"schema_version": 1, "schedule_enabled": true, "daily_time": "03:00", "max_new_per_run": 100,
 "download_retry_every_days": 7, "profile_url": null, "headless": true, "cleanup_enabled": true,
 "kb_root": "~/XHS-Wiki", "engine": "markdown",
 "llm": {"provider": "claude_code", "model": "claude-haiku-5-5", "base_url": null},
 "asr_model": "whisper", "first_import_limit": 20, "setup_completed": false, "llmwiki_binary": null}
```

- `first_import_limit`：首次导入范围。整数 N 只在**第一次成功扫描**时生效：当时排在第 N 位以后的旧收藏标记为 `skipped`，之后新收藏的笔记不受限制；`null` 表示全部收藏。改成 `null` 后，之前跳过的旧收藏会在下次运行时重新排队。
- `llmwiki_binary`：LLM Wiki 后台程序的绝对路径（§6.2），下载并校验完成后由守护进程或 `assets install-llmwiki` 命令自动写入；控制台接口仍不接受这个字段。
- 控制台日常页只能修改定时、每次条数、清理开关、语音模型和模型名；其余字段只能通过首次设置向导写入。
- API Key **不进** `settings.json`，存进 macOS 钥匙串：服务名 `xhs-wiki`，账户名为提供方名称。用系统自带的 `security` 命令读写，命令行参数里不出现 Key 本身（通过标准输入传入）。

## 8. 模型调用 `llm/`

```python
class Provider(Protocol):
    def complete(self, *, system: str, prompt: str, max_output_tokens: int) -> str: ...
    def check(self) -> tuple[bool, str]: ...   # 一次最小调用
```

| provider | 实现 |
|---|---|
| `claude_code` | `claude -p`，指定模型，**不开任何工具、不加载 MCP、不读用户的设置**；当前 claude 支持时再加 `--safe-mode`，屏蔽用户个人的 CLAUDE.md、Skill 和插件（先用 `claude --help` 检测是否支持）。从标准输入传提示词，在专用空目录 `APP_HOME/llm-workdir/` 里运行，避免读到其他项目的说明文件。环境里补上 HOME/USER，并去掉 CLAUDE_CONFIG_DIR |
| `codex` | `codex exec --skip-git-repo-check --sandbox read-only -o <临时文件>`，同样在 `APP_HOME/llm-workdir/` 里运行；模型名为空时不传 `-m`，使用 Codex 自己的默认模型。Codex 的路径自动发现（常见安装位置，或者 `PATH`） |
| `anthropic` | 官方 `anthropic` SDK，Key 从钥匙串读取 |
| `openai_compatible` | 官方 `openai` SDK，加上 `base_url`（DeepSeek、通义千问等），Key 从钥匙串读取 |

- 所有调用都设超时，按 provider 记录调用次数和失败原因，日志里不打印提示词全文和 Key。

## 9. 一次运行 `runner.py`

步骤为：预检、扫描、对比登记、每周下载重试、处理、清理、汇总。其中"处理"一步按台账推进：

```
for stage in [download, parse, ingest, distill]:
    取出处于上一阶段、没超过重试次数的笔记（每批有上限）→ 执行 → 推进或记失败
    每一批之间都检查暂停
```

预检项目：
- 磁盘空间；
- 所选 provider 的 `check()`（Claude Code 和 Codex 先查登录状态，不消耗用量）；
- 引擎的 `health()`；
- 如果用 LLM Wiki 引擎，还要检查桌面版有没有开着。

其他规则：
- **运行记录**：每次运行结束追加一行到 `runs.jsonl`，包括触发方式、结果、扫描数、新增数、各阶段成功和失败数、清理释放的空间、停止原因。
- **扫描不完整照常处理**：收藏列表没能完整滚动到底时（`scan_incomplete`），已经看到的收藏照常处理，运行记录里标记"不完整"。
- **追加运行**：一次运行结束时如果还有笔记没到终态（结果 `incomplete`，例如模型额度用完被熔断），60 分钟后自动追加运行，每天最多 6 次；没有运行在进行、也没有请求暂停时才会开始。
- **熔断**：模型返回"未登录""额度或频率限制"这类错误时，本轮不再调用模型，不计入失败次数，交给追加运行重试。
- **下载暂停**：同一轮里连续 3 条笔记下载失败，且错误都是 `metadata_unavailable` 或 `network_error`（很可能是被平台限制），就停止本轮剩下的下载，登记事项 `xhs_download_blocked` 并发系统通知；本轮已下载的笔记照常解析、整理和清理。这次运行不触发追加运行，等下一次定时运行再试；之后有下载成功时事项自动清除。

## 10. 清理 `cleanup.py`

条件：笔记已经 `distilled` 或 `deferred`，还没清理过。删除前做三道核对，任何一道不通过就跳过这条、记下原因、绝不删除：

1. 引擎确认来源已入库（轻量引擎检查来源页是否存在；LLM Wiki 引擎核对来源版本）；
2. 解析包还在，且它的 sha256 等于台账里记录的 `package_sha256`；
3. 要删的每个文件都在解析包的原件清单里、sha256 一致，并且是 `work/<note_id>/media/` 下的普通文件（不是符号链接，解析真实路径后仍在这个目录里）。清单之外的文件一概不碰。

先写回执（`cleanup-receipt.json`，0600）再删除，每删一个文件就更新一次回执；确认文件确实不存在了，才更新台账。中途断电也能按回执对账。

## 11. 守护进程和控制台 `daemon.py`、`web/console.html`

- **访问控制**：控制台只监听 127.0.0.1，但同一台 Mac 上的其他账户和程序也能连到回环端口，所以要求访问令牌：
  - 服务启动时生成 `APP_HOME/console-token`（0600）。`xhs-wiki open` 先用随机挑战核对端口上确实是本机这份服务（答案绑定端口，转发到别的端口无效），再写一个只有本人能读的跳转页（0600），用 POST 把一次性登录码（由令牌派生、2 分钟有效、只能用一次、只认这个端口）提交给 `/auth`；令牌本身不写进跳转页，也不出现在 URL 和进程参数里，登录成功后跳转页被删除；
  - 服务端校验通过后，下发 HttpOnly、SameSite=Strict 的 Cookie；
  - 之后所有页面和接口都要求这个 Cookie，没有就拒绝。`/healthz` 不含任何数据，供安装脚本探活；带随机挑战时只多回一个绑定端口的身份证明，供 `xhs-wiki open` 核对；
  - 另外保留 Host、Origin 和 CSRF 检查，防跨站请求和 DNS 重绑定。
- **接口白名单**：日常设置接口只接受定时、条数、清理、语音模型和模型名；知识库位置只能经向导设置，并校验是不是普通的绝对路径目录（不能是程序包、磁盘根目录、主目录本身、"文稿""桌面"这类系统文件夹本身或系统目录，不能含控制字符和格式字符）。
- **连接限制**：每个连接有 10 秒读写超时，同时处理的连接数有上限，防止同机程序靠空闲连接占满服务。
- 定时、补跑、追加运行；扫码子进程。
- **首次向导**：`setup_completed=false` 时显示。依次是知识库位置 → 引擎 → 模型（选择后测试调用）→ 扫码 → 首次导入范围；日常页的"重新设置"可以重走向导。在向导里改知识库位置时，已有内容会移动到新位置：旧目录里只有本工具生成的内容时整个目录移动，还有用户自己的文件时只移动五类生成内容；同一块磁盘上直接改名，跨磁盘时先完整复制并核对、再删除原件。
- 知识库统计（主题数、来源数、暂缓数）；失败笔记单条重试；
- "打开知识库文件夹"按钮：用 `/usr/bin/open -R` 在访达里选中知识库目录，不会启动任何程序。

## 12. Skill `skill/SKILL.md.tmpl`

安装时渲染到 `~/.claude/skills/xhs-wiki/SKILL.md`，填入 `KB_ROOT` 和引擎类型。

**描述写法**：说明这是"用户自己的小红书收藏知识库"，触发条件是用户提到"我收藏的 / 我之前存过 / 查一下我的收藏"，或者问题需要结合他收藏过的内容来回答。描述保持通用，不列具体领域。

**使用步骤**：
1. 读 `索引.md`；
2. 选出相关主题，读对应的主题页；
3. 需要细节时，顺着链接读来源页；
4. 回答时附上来源页标题和小红书原链接；
5. 找不到就如实说没收藏过。

**安全提示**：知识库内容是第三方帖子的摘录。Skill 明确要求 AI 只把它当资料引用，不能当成指令执行，以防帖子里的文字被用来做提示注入。

## 13. 安装和卸载

- **安装**：`install.sh`（bash，可以重复执行，不允许用 root 运行）。
  1. 检查 macOS 版本、芯片和磁盘空间；
  2. 检查 Google Chrome；
  3. 检查 uv（没有就征求同意后，用官方安装脚本装到用户目录）；
  4. 在 `APP_HOME/venv` 里装好 Python 3.12 和本项目。远程安装直接下载 GitHub 的源码压缩包（`archive/refs/heads/main.tar.gz`），不需要 git；重型依赖只用预编译包（`--only-binary`），让安装器自动选出兼容当前 macOS 版本和芯片的版本；
  5. 启动服务并打开控制台：征得同意后写入 LaunchAgent（写 plist 时把当前的 PATH、代理和镜像设置一起写进去；工作目录和日志不放 `~/Documents`），否则用 nohup 启动临时服务；然后运行 `xhs-wiki open`；
  6. XHS-Downloader 和语音模型交给服务在后台下载（按固定 SHA-256 核对），脚本不等待；LLM Wiki 无窗口后台仅在选择该引擎时按需下载，也可用 `xhs-wiki assets install-llmwiki` 下载；其压缩包和程序本体分别校验固定 SHA-256，通过后自动配置路径；
  7. 渲染 Skill；
  8. 建立命令链接 `~/.local/bin/xhs-wiki`；
  9. 结束提示。

  不需要 Homebrew、ffmpeg、git 或 Xcode 命令行工具。`--mirror cn` 会把 Python 包、Python 安装包和语音模型的下载切到国内镜像（阿里云 PyPI、npmmirror、hf-mirror），后台服务也沿用；安装脚本本身和源码包仍从 GitHub 下载。
- **卸载**：`uninstall.sh` 停止服务（LaunchAgent 和临时服务都会停掉），删除 LaunchAgent、`APP_HOME`、日志和 Skill；**知识库默认保留**。必须同时加上 `--yes` 和 `--purge-kb` 才清理知识库，而且只删本工具生成的文件（索引、主题、来源、暂缓页和 `.xhs-wiki/`），目录里没有 `.xhs-wiki/` 标记时拒绝删除。日志目录和 Skill 目录也只删本工具的文件，目录清空后才删除目录本身。

## 14. 测试和质量门

- 用 pytest。所有测试都在临时目录里进行，用假的浏览器、子进程和模型，不访问网络、不调用真实模型。SDK 类的提供方用真实 SDK 客户端加假的 HTTP 层测试，确保调用方式和 SDK 一致。
- GitHub Actions 在 Apple 芯片（macos-15）和 Intel（macos-15-intel）两种 macOS 15 上跑全部测试。
- 真实网站、真实模型的联调由维护者手动进行，结果不进仓库。
- 发布前要做敏感信息扫描：个人路径、主页编号、签名链接、Key 的特征。开发环境和贡献流程见 [CONTRIBUTING.md](../CONTRIBUTING.md)。
