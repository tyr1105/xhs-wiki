# xhs-wiki 技术设计

> 面向贡献者。用户文档见 README。本文只描述工具本身，不包含、也不预设任何人的收藏内容或主题分类。

## 0. 总览

```
install.sh ── 检查环境 → 装好本体 → 启动服务（LaunchAgent 或临时服务）并打开控制台 → 写入查询 Skill（勾选的 AI 工具）
                （XHS-Downloader 和语音模型由服务在后台下载，进度显示在控制台顶部）
                                                  │
LaunchAgent ── xhs-wiki serve（守护进程：控制台 HTTP + 定时器 + 子进程管理）
                 │
                 ├─ xhs-wiki run --trigger …（一次运行）
                 │     预检 → 扫描收藏 → 对比新增 → 下载 → 解析 → 引擎入库 → 引擎整理 → 清理原件 → 汇总
                 │       scan.py   ledger.py   download.py  parse/   engines/<name>          cleanup.py
                 └─ xhs-wiki login（扫码登录，带窗口）
```

- **语言和运行环境**：Python 3.12，由 `uv` 管理；只支持 macOS（Apple 芯片和 Intel 都支持；LLM Wiki 引擎也不限制芯片类型）。
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
engines/llmwiki_app → 来源 Markdown 文件 + LLM Wiki 应用的记录文件（文件交接）
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
- 下载组件沿用上游默认的浏览器模拟（curl_cffi 的 `impersonate="chrome146"`）。评估过构造后把会话的 `impersonate` 置空：技术上可行，但小红书会把笔记页请求跳转到登录页、拿不到内容（见 [#1](https://github.com/tyr1105/xhs-wiki/issues/1)），所以保留默认。本项目自身不加其他反检测手段。
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
    def source_exists(self, note_id: str, engine_ref: dict) -> bool: ...  # 清理前核对来源
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

### 6.2 LLM Wiki 引擎 `engines/llmwiki_app/`（进阶）

v0.4.0 起对接 [nashsu/llm_wiki](https://github.com/nashsu/llm_wiki) 桌面应用（GPL-3.0），引擎名仍是 `llmwiki`。用户自行安装 **0.6.13 或更新版本**，本项目不下载或分发它的任何文件，也不修改它的配置内容。Apple 芯片和 Intel 均可选用。

**文件交接**就是：xhs-wiki 把解析后的文字放进项目，LLM Wiki 应用自动整理，xhs-wiki 再读应用的记录确认结果。模型、接口地址和密钥由用户在 LLM Wiki 的设置 → LLM Models 中配置；这个引擎不调用 `llm/` 的提供方，不直接请求模型。

| 文件或目录 | xhs-wiki 的操作 | 用途与路径口径 |
|---|---|---|
| `KB_ROOT/raw/sources/xhs/<note_id>.md` | 写入 | 来源文字；文件头包含 `title`、`author`、非空时的 `published_at`、不带签名的 `url`、`note_id`、`source`，后接 `package_md` |
| `KB_ROOT/.llm-wiki/ingest-cache.json` | 只读 | 完成记录：`entries` 的键相对 `raw/sources/`，例如 `xhs/<note_id>.md`；条目含 `hash`、`filesWritten` |
| `KB_ROOT/.llm-wiki/ingest-queue.json` | 只读 | 整理任务数组；`sourcePath` 相对项目根，例如 `raw/sources/xhs/<note_id>.md`；读取 `status`、`retryCount`、`error` |
| `KB_ROOT/.llm-wiki/review.json` | 只读 | 待审核建议数组，控制台统计 `resolved` 为假或缺失的条目；读不出时隐藏这项，不阻止整理 |
| `~/Library/Application Support/com.llmwiki.app/app-state.json` | 只读内容 | `lastProject.path` 是应用当前项目；`taskModelRouting.ingestPresetId`（空时用 `activePresetId`）选择 `providerConfigs` 中的整理提供方 |
| `KB_ROOT/wiki/` | 读取生成结果 | 索引 `wiki/index.md`，来源 `wiki/sources/`，知识页见下文；内容由 LLM Wiki 维护 |

`app-state.json` 可能含**明文 API 密钥**。解析状态文件后只判断 `apiKey` 是否为空，不把值写进日志、返回给页面或用来调用模型。控制台的「收紧为仅本人可读」按钮是唯一权限修改入口：经用户点击后只对该文件执行 `chmod 600`，不修改文件内容，也不改其他文件。权限提醒按 `mode & 0o077` 判断，含任意组或其他用户权限位就提示；`doctor` 只提示，不修改权限。

**运行与完成判定：**

1. `health_checks()` / `health()` 不启动应用、不调用模型。按顺序检查：`/Applications/LLM Wiki.app` 或 `~/Applications/LLM Wiki.app` 存在；版本不低于 0.6.13；项目有 `purpose.md`、`schema.md`、`.llm-wiki`（当前实现只检查存在性）；整理提供方配置满足要求。`health()` 返回首个失败原因；`doctor` 显示全部检查项。当前打开哪个项目由 `prepare()` 检查。
2. 提供方检查读取 Ingest model 的预设编号，空时退回当前活跃编号。内置 `claude-code-cli`、`codex-cli`、`ollama-local` 不要求密钥，也不要求 `providerConfigs` 有条目；配置中的 `provider` / `apiMode` 属于 `claude-code`、`codex-cli`、`ollama` 时同样免密钥。自定义提供方（`custom` 或 `custom-` 开头）的 `baseUrl` 主机是 `localhost`、`127.0.0.1` 或 `::1` 时免密钥，其余要求条目存在且 `apiKey` 非空。这是配置检查，不验证模型是否实际可调用、CLI 是否已登录或服务是否可达。
3. `runner` 在处理阶段开头调用 `prepare()`。通过 `pgrep -x llm-wiki` 检查进程；未运行时用参数列表执行 `open -g -j -b com.llmwiki.app` 后台启动，最多等待 60 秒，进程出现后再等 10 秒加载项目。随后规范化 `lastProject.path` 并与 `KB_ROOT` 比较，不一致或读不出就停止本轮、登记「待你处理」，不自动切换项目。关掉窗口可以继续整理，退出应用则不能整理；下次运行的处理阶段会再次尝试启动。
4. `ingest()` 原子写来源：先在同目录写点开头、`.tmp` 结尾的临时文件，完成后 `os.replace`；字符串用 JSON 双引号转义为合法的 YAML 字符串，避免标题里的引号、换行破坏文件头。已有文件内容相同时不重写。台账引用为 `engine_ref = {"source": "xhs/<note_id>.md", "sha256": "<内容指纹>"}`。
5. `distill()` 默认每 5 秒读一次 cache 和 queue。先比对来源文件**当前** SHA-256 与 cache 的 `hash`，再确认 `filesWritten` 是列表且列出的每个路径存在；全部满足才在 `per_note` 中记 `ok`。当前实现允许空 `filesWritten` 列表通过，因此不能把通过检查等同于一定新增了页面。未完成的来源只有在队列对应项 `status=failed` 且 `retryCount>=3` 时才记失败；队列原始错误只用于归类，不直接输出。
6. 默认总等待时间为条数 × 3 分钟，最少 10 分钟、最多 60 分钟。超时未完成的笔记不写入 `per_note`，由 `runner` 保持 `ingested`，下一轮接着核对。cache/queue 不存在时视为空记录；JSON 损坏或外层形状不识别时按无法判断处理，连续两个轮询周期读不出才在 `errors` 中报告 `llmwiki_record_unreadable`，不据此判成功或删除原件。
7. `source_exists()` 在清理前再次核对当前来源文件指纹、cache 与 `filesWritten`，不单凭 `engine_ref.sha256` 或本轮结束状态判断成功。待审核建议独立统计，既不拦截完成判定，也不由 xhs-wiki 自动接受或拒绝。

统计口径：`assigned` 是本批确认完成的条数；`pages_rewritten` 是这些条目的 `filesWritten` 长度之和，不能区分新建和更新；`new_topics` 是其中位于 `wiki/concepts/`、`wiki/entities/`、`wiki/synthesis/`、`wiki/comparisons/`、`wiki/queries/` 的去重路径，表示写过的页面，不保证都是新主题。控制台的主题总数只数前四个知识目录的 `.md` 文件；查询 Skill 的主题说明也排除 `wiki/queries/`。

**原因代码**（以 `engine.py`、`daemon.py` 和控制台文案为准）：

| 原因代码 | 触发条件 | 处理方式 |
|---|---|---|
| `llmwiki_app_missing` | 两个应用目录都找不到应用，或后台启动后进程没有在期限内出现 | 安装 0.6.13 或更新版本，或手动打开应用后重试 |
| `llmwiki_app_too_old` | 版本低于最低要求，或版本读不出 | 更新应用；运行预检将此原因放在通用 `engine_unavailable` 事项中 |
| `llmwiki_project_invalid` | 项目缺少 `purpose.md`、`schema.md`、`.llm-wiki` 中任一项 | 在 LLM Wiki 中新建或打开项目，选对知识库位置 |
| `llmwiki_model_missing` | 状态读不出、没有选中的预设，或应有密钥的配置缺少密钥 | 在 LLM Wiki 设置 → LLM Models 配好提供方并选为 Ingest model |
| `llmwiki_wrong_project` | `prepare()` 读不到当前项目，或它与知识库路径不同 | 在 LLM Wiki 中打开该知识库；xhs-wiki 停止本轮 |
| `llmwiki_model_param_rejected` | 队列最终失败原文包含 `temperature` | 换模型或使用自定义提供方；Claude 5 的具体设置见 README |
| `llmwiki_auth_failed` | 原文包含 `401`、`authentication` 或 `invalid x-api-key` | 在 LLM Wiki 中更新密钥 |
| `llmwiki_quota` | 原文包含 `credit`、`balance` 或 `billing` | 到模型提供方处理余额或计费限制 |
| `llmwiki_model_not_found` | 原文同时包含 `model` 与 `not_found` | 在 LLM Wiki 中核对模型名 |
| `llmwiki_ingest_failed` | 其他队列最终失败 | 查看 LLM Wiki 任务队列 |
| `llmwiki_record_unreadable` | cache 或 queue 连续两个轮询周期无法读取或外层形状不识别 | 保留待处理状态与原件，核对应用版本和记录格式 |
| `llmwiki_state_missing` | 点击收紧权限时找不到状态文件 | 先打开一次 LLM Wiki |
| `llmwiki_chmod_failed` | 收紧权限操作抛出文件系统错误 | 稍后重试；不输出系统错误原文 |
| `llmwiki_selftest_timeout` | 向导测试最多等待 5 分钟后没有该示例的完成结果，或等待过程抛错 | 确认应用和模型可用后再试 |

队列错误按表中 `model_param_rejected` → `auth_failed` → `quota` → `model_not_found` → `ingest_failed` 的顺序匹配，小写比较、先匹配先得。应用缺失、项目无效、模型缺失、项目不符四类有独立的运行事项；其他健康检查失败走 `engine_unavailable`。

**控制台接口：** 第 2 步通过 `POST /api/setup/engine` 调 `health()`，健康后保存引擎；只有 `llmwiki_model_missing` 允许先进入第 3 步配模型，但该次不会保存引擎。第 3 步 `GET /api/setup/llmwiki-model` 返回提供方、模型名、密钥满足与否、当前项目是否正确及文件权限摘要，不启动应用。模型摘要、收紧权限和测试接口都要求已保存的引擎是 `llmwiki`，否则返回 `engine_invalid`。

「测试」调用 `POST /api/setup/test-llmwiki`：没有 `confirm_cost=true` 则返回 `cost_confirmation_required`；有确认后先 `prepare()`，再写入虚构的 `raw/sources/xhs/xhs-wiki-selftest.md`，写失败返回 `package_write_failed`；等待至多 5 分钟。结束时删除该示例来源文件；LLM Wiki 检测到来源被删后，会自己删除这条来源生成的页面并移除整理记录（11-19 真机实测，60 秒内完成）。xhs-wiki 不直接删除任何页面。`POST /api/setup/llmwiki-tighten` 只收紧状态文件权限。三个接口沿用控制台的身份校验，两个 POST 也在既有白名单和 CSRF 校验内。

**升级后的调用边界：** 旧引擎的 Claude 包装脚本会检测命令支持的选项，只在 `--print` / `-p` 且原参数没有相应选项时补上 `--safe-mode`、`--no-session-persistence`；旧启动器还会设置 `LLM_WIKI_CONTENT_LANGUAGE=zh-CN`，固定生成页面的语言。v0.4.0 已删除这套包装与启动器，不再施加这些参数或页面语言设置。LLM Wiki 调用模型时的工具权限、会话保存和内容语言由应用及所选提供方负责，不能沿用旧引擎的隔离或固定中文保证。

## 7. 设置 `settings.json`

```json
{"schema_version": 1, "schedule_enabled": true, "daily_time": "03:00", "max_new_per_run": 100,
 "download_retry_every_days": 7, "profile_url": null, "headless": true, "cleanup_enabled": true,
 "kb_root": "~/XHS-Wiki", "engine": "markdown",
 "llm": {"provider": "claude_code", "model": "claude-haiku-5-5", "base_url": null},
 "asr_model": "whisper", "first_import_limit": 20, "setup_completed": false,
 "skill_targets": ["claude_code"]}
```

- `skill_targets`：查询 Skill 装进哪些 AI 工具（§12 表里的位置名，即 `store.SKILL_TARGETS`），默认只有 Claude Code。接口只接受这些固定名字、不能重复，可以为空列表；只能经 `/api/skill-targets`（向导第 6 步和日常页）修改。读设置文件时会清理：旧设置文件没有这个键、或者值不是列表，读出默认值；不认识的名字（手工改过、从新版本降级回来）和重复项直接去掉——不然整份设置都过不了校验，别的设置也存不进去。

- `first_import_limit`：首次导入范围。整数 N 只在**第一次成功扫描**时生效：当时排在第 N 位以后的旧收藏标记为 `skipped`，之后新收藏的笔记不受限制；`null` 表示全部收藏。改成 `null` 后，之前跳过的旧收藏会在下次运行时重新排队。
- `llm` 保存轻量引擎的提供方、模型名和地址；LLM Wiki 引擎实际整理使用应用自己的设置（§6.2）。旧设置里的 `llmwiki_binary` 读取时丢弃，不再用于启动应用。`headless` 控制扫描收藏时是否隐藏浏览器窗口，与 LLM Wiki 无关。
- 控制台日常页的设置接口只能修改定时、每次条数、清理开关、语音模型和模型名；提问用的 AI 工具走专用接口（改完立即同步 Skill）；其余字段只能通过首次设置向导写入。
- 轻量引擎的 API Key **不进** `settings.json`，存进 macOS 钥匙串：服务名 `xhs-wiki`，账户名为提供方名称。用系统自带的 `security` 命令读写，命令行参数里不出现 Key 本身（通过标准输入传入）。LLM Wiki 自己的 API 密钥以明文保存在其 `app-state.json`，由该应用管理；xhs-wiki 只做存在性检查（§6.2）。

## 8. 模型调用 `llm/`

以下四种提供方供轻量引擎调用。LLM Wiki 引擎的整理模型由应用自己选择和调用，不使用此表的调用参数或隔离设置；选 LLM Wiki 引擎时，运行预检、完成向导和 doctor 都不检查 xhs-wiki 自己的 `llm` 设置，模型检查由引擎的 `health()`（`llmwiki_model`）负责。

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
- xhs-wiki 设置中所选 provider：Claude Code 和 Codex 调 `check()` 查登录，不消耗用量；API 类只查钥匙串是否有密钥；
- 引擎的 `health()`；LLM Wiki 的检查内容见 §6.2，不要求关闭应用。

前一项 provider 预检只对轻量引擎执行；LLM Wiki 引擎跳过它，`doctor` 和向导完成接口同样只在轻量引擎下检查 xhs-wiki 自己的模型和钥匙串。LLM Wiki 的后台启动和当前项目核对在扫描之后、下载处理开始前由 `prepare()` 执行。

其他规则：
- **运行记录**：每次运行结束追加一行到 `runs.jsonl`，包括触发方式、结果、扫描数、新增数、各阶段成功和失败数、清理释放的空间、停止原因。
- **扫描不完整照常处理**：收藏列表没能完整滚动到底时（`scan_incomplete`），已经看到的收藏照常处理，运行记录里标记"不完整"。
- **追加运行**：一次运行结束时如果还有笔记没到终态（结果 `incomplete`，例如模型额度用完被熔断），60 分钟后自动追加运行，每天最多 6 次；没有运行在进行、也没有请求暂停时才会开始。
- **熔断**：模型返回"未登录""额度或频率限制"这类错误时，本轮不再调用模型，不计入失败次数，交给追加运行重试。
- **下载暂停**：同一轮里连续 3 条笔记下载失败，且错误都是 `metadata_unavailable` 或 `network_error`（很可能是被平台限制），就停止本轮剩下的下载，登记事项 `xhs_download_blocked` 并发系统通知；本轮已下载的笔记照常解析、整理和清理。这次运行不触发追加运行，等下一次定时运行再试；之后有下载成功时事项自动清除。

## 10. 清理 `cleanup.py`

条件：笔记已经 `distilled` 或 `deferred`，还没清理过。删除前做三道核对，任何一道不通过就跳过这条、记下原因、绝不删除：

1. 引擎确认来源已入库（轻量引擎检查来源页是否存在；LLM Wiki 引擎核对当前来源指纹、cache 与 `filesWritten` 中的路径，见 §6.2）；
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
- **首次向导**：`setup_completed=false` 时显示。依次是知识库位置 → 引擎 → 模型（选择后测试调用）→ 扫码 → 首次导入范围 → 提问用的 AI 工具（§12，默认只勾 Claude Code；日常页也有同一组勾选框）；日常页的"重新设置"可以重走向导。在向导里改知识库位置时，只有旧目录带 `.xhs-wiki/` 标记才走现有的搬移流程：旧目录里只有本工具生成的五类内容时整个目录移动，还有其他文件时只移动这五类内容；同一块磁盘上直接改名，跨磁盘时先完整复制并核对、再删除原件。此流程不转换 LLM Wiki 的 `raw/`、`wiki/` 和 `.llm-wiki/` 项目结构。
- 知识库统计（主题数、来源数、暂缓数）：轻量引擎数 `主题/`、`来源/` 下的文件；LLM Wiki 引擎的主题数是 `wiki/concepts/`、`wiki/entities/`、`wiki/synthesis/`、`wiki/comparisons/` 中直接包含的 `.md` 文件数之和，来源数是 `wiki/sources/` 中直接包含的 `.md` 文件数，不计 `wiki/queries/`；暂缓数两种引擎都取台账。LLM Wiki 待审核数读 `.llm-wiki/review.json`，读取失败时隐藏；这些建议不阻止整理。失败笔记单条重试；
- "打开知识库文件夹"按钮：用 `/usr/bin/open -R` 在访达里选中知识库目录，不会启动任何程序。

## 12. Skill `skill/SKILL.md.tmpl`

渲染时填入 `KB_ROOT`、引擎类型，以及当前引擎 `skill_context()` 给出的索引、主题页、来源页位置：轻量引擎是 `索引.md`、`主题/`、`来源/`，LLM Wiki 引擎的索引是 `wiki/index.md`，来源是 `wiki/sources/`；`topics_dir` 是 `wiki/concepts`，`topic_dirs` 还列出 `wiki/entities`、`wiki/synthesis`、`wiki/comparisons`、`wiki/queries`。渲染查询步骤时排除 `queries`，说明其余四个知识目录。

**装进哪些 AI 工具**：SKILL.md 是开放的 Agent Skills 格式（一个文件夹，放带 `name`、`description` 的 Markdown），各家只是去不同目录找。同一份渲染结果写到设置 `skill_targets` 勾选的位置（`skill.py` 的 `TARGET_ROOTS`）：

| 位置 | 目录 | 哪些工具读（2026-11-03 按官方文档核实） |
|---|---|---|
| `claude_code`（默认） | `~/.claude/skills/xhs-wiki/` | Claude Code；Cursor、OpenCode、Goose、Amp、VS Code 里的 GitHub Copilot 为了兼容也读 |
| `workbuddy` | `~/.workbuddy/skills/xhs-wiki/` | 腾讯 WorkBuddy（产品文档只写"用户配置目录"，具体路径来自腾讯 SkillHub 安装说明和腾讯官方 GitHub 仓库） |
| `agents` | `~/.agents/skills/xhs-wiki/` | OpenAI Codex（命令行、IDE 插件、ChatGPT 桌面版）、Cursor、Gemini CLI、GitHub Copilot、OpenClaw、Kimi Code、Qwen Code、新版 Qoder、Zed、Devin Desktop（原 Windsurf）、OpenCode、Goose、Amp、Factory Droid |
| `codebuddy` | `~/.codebuddy/skills/xhs-wiki/` | 腾讯 CodeBuddy（命令行版官方写明，IDE 版按同一套文档推断） |
| `trae` / `trae_cn` | `~/.trae/skills/xhs-wiki/` / `~/.trae-cn/skills/xhs-wiki/` | 字节 Trae 国际版 / 国内版 |
| `qoder` | `~/.qoder/skills/xhs-wiki/` | 阿里 Qoder |
| `lingma` | `~/.lingma/skills/xhs-wiki/` | Qoder CN（原通义灵码）的 IDE 和编辑器插件 |
| `qoder_cn_cli` | `~/.qoder-cn/skills/xhs-wiki/` | Qoder CN 命令行版 |
| `kiro` | `~/.kiro/skills/xhs-wiki/` | AWS Kiro |
| `antigravity` | `~/.gemini/config/skills/xhs-wiki/` | Google Antigravity（App、IDE、Antigravity CLI）；免费和 Google One 用户的 Gemini CLI 已在 2026-06-18 换成 Antigravity CLI，它不读 `~/.agents/skills` |
| `cline` | `~/.cline/skills/xhs-wiki/` | Cline |

实测过的只有 Claude Code 和 Codex 命令行（Codex 实测时 Skill 放在测试项目的 `.agents/skills`，和 `~/.agents/skills` 是同一套机制），其余按官方文档支持，控制台和 README 都写明"还没实测"。控制台把前三个位置直接列出，其余收在"更多 AI 工具"里。

- 目录以相对主目录的写法登记在 `skill.TARGET_DIRS`（`uninstall.sh` 的 `EXTRA_SKILL_DIRS` 是同一份清单，测试核对两边一致）。
- **隔离用的环境变量**：`XHS_WIKI_TOOLS_HOME` 只给测试和隔离安装用，正式安装不设。设了它，所有位置的技能目录（包括 Claude Code）和检测线索都换到它下面，检测也只看它下面的 `Applications`；`XHS_WIKI_SKILLS_ROOT` 再单独改写 Claude Code 的目录。`skill.py`、`install.sh`、`uninstall.sh` 三处默认值一致。安装脚本只在用户自己设了 `XHS_WIKI_TOOLS_HOME` 时才导出、才写进后台服务的环境——正式安装要是也写进去，服务就按隔离模式检测，系统"应用程序"文件夹里的 App 都认不出来；它的值正好就是主目录时也按没设处理。
- **只碰本工具的文件**：模板在 frontmatter 后面带一行归属标记 `<!-- managed-by: xhs-wiki -->`（文件仍以 `---` 开头，各工具照常加载）。v0.1~v0.2 写的旧文件没有标记，按模板一直没变的几行认领（第 1、2 行是 `---`、`name: xhs-wiki`，正文有"# 小红书收藏知识库""## 内容安全（先读这一段）"两个标题）。写入、撤掉、卸载都先核对：同名但不是本工具写的 `xhs-wiki/SKILL.md`（用户自己写的、别的项目恰好同名的、本身是符号链接的）一律不覆盖、不删除、不改文件夹权限，结果代码 `kept_foreign`，页面显示"已有别人写的同名技能"，不显示"已装好"。
- **符号链接**：`xhs-wiki` 文件夹本身是链接时，只有它正好指向另一个勾选了、本身不是链接的位置才算装上（经那个位置写一次）；其他情况不顺着链接写、也不顺着删，结果代码 `kept_symlink`，页面如实提示。整个技能目录被链接（比如 `~/.agents/skills` → `~/.claude/skills`）不算这种情况，按真实目录合并处理。
- **写入**：首次设置完成、改知识库位置、换引擎、升级（安装脚本第 7 步）时，只重写勾选的位置，没勾选的位置一概不碰。服务端在锁里重新读设置再写（避免和"改勾选"同时发生时把刚撤掉的位置写回去），并记下每个位置最近一次的结果；改知识库位置、换引擎、完成设置三个接口把结果（`skill_results`）带回页面，某个工具没写成功时页面说清是哪一个。安装脚本第 7 步只有 Claude Code 写不进去才让安装停下（和以前一样），其他工具写不进去只打印位置名和目录，安装照常完成。
- **撤掉**：只有用户在控制台取消勾选时，才撤那个位置里本工具写的 `SKILL.md` 和 `.skill-*` 临时文件；目录清空了才删目录，用户自己的文件保留；`xhs-wiki` 文件夹本身是符号链接时不删。上次取消时没删成（比如目录暂时不可写）的，之后每次保存勾选都会重试：没勾选、但还留着带归属标记文件的位置一并撤掉。几个位置其实是同一个目录（比如用户把 `~/.agents/skills` 链接到了 `~/.claude/skills`）时按真实目录合并处理，不会装了又删；撤的时候每个位置各用自己的目录，不借用组里别的位置去删。
- **卸载**：`uninstall.sh` 查遍所有位置，只删认得出是本工具写的 `SKILL.md`（同样核对标记和旧版签名，用系统自带的 grep、sed）；别人的同名技能列进"不会删除"并保留。Claude Code 的目录照旧严格：是符号链接或路径解析不了就整个停下，报错带路径和处理办法；其他工具的目录是符号链接、或上级目录进不去的，跳过并说明是哪个目录，其余照常卸载。知识库放在任何一个技能目录里面时拒绝卸载。
- **检测**：向导和日常页显示"本机有 xx"，只看工具自己建的文件或 App（系统和用户的"应用程序"文件夹）在不在（`~/.claude.json`、`Claude.app`、`WorkBuddy.app`、`~/WorkBuddy`、`~/.codex`、`~/.cursor`、`~/.gemini/settings.json`、`~/.copilot`、`Trae.app` 等，见 `skill._TOOL_MARKERS`），不读内容，也不拿本工具自己会建的技能目录（`~/.claude`、`~/.trae`、`~/.gemini/config` 这些）当线索。没有可靠线索的工具不显示。检测结果只是提示，装不装由用户勾选。判断一律用 `os.path` 系列（遇到没有进入权限的目录返回"不在"，不抛异常）；状态接口再兜一层底，某个目录出问题时退回最简状态，控制台不会一直显示"暂时无法读取"。
- **访问授权**：WorkBuddy 只读用户授权过的工作空间，Gemini CLI、Copilot、Devin Desktop、Antigravity 默认限于当前工作区，CodeBuddy、Kiro、Cline 读工作区外的文件会先问用户，所以页面按工具给出提示（把知识库文件夹选为工作空间、加进工作区或允许访问）；Skill 第 7 步要求读不到时如实说"读不到知识库"，不说成没收藏过。

**描述写法**：说明这是"用户自己的小红书收藏知识库"，触发条件是用户提到"我收藏的 / 我之前存过 / 查一下我的收藏"，或者问题需要结合他收藏过的内容来回答。描述保持通用，不列具体领域。

**使用步骤**：
1. 读索引；
2. 选出相关主题，读对应的主题页；
3. 索引里找不到合适的主题时，在来源页目录里按关键词搜索；
4. 需要细节时，顺着链接读来源页；
5. 回答时附上来源页标题和小红书原链接；
6. 找不到就如实说没收藏过；
7. 读不到知识库文件（没权限、不在工作区）时说读不到，请用户授权后再问，不说成没收藏过。

**安全提示**：知识库内容是第三方帖子的摘录。Skill 明确要求 AI 只把它当资料引用，不能当成指令执行，以防帖子里的文字被用来做提示注入。

## 13. 安装和卸载

- **安装**：`install.sh`（bash，可以重复执行，不允许用 root 运行）。
  1. 检查 macOS 版本、芯片和磁盘空间；
  2. 检查 Google Chrome；
  3. 检查 uv（没有就征求同意后，用官方安装脚本装到用户目录）；
  4. 在 `APP_HOME/venv` 里装好 Python 3.12 和本项目。远程安装直接下载 GitHub 的源码压缩包（`archive/refs/heads/main.tar.gz`），不需要 git；重型依赖只用预编译包（`--only-binary`），让安装器自动选出兼容当前 macOS 版本和芯片的版本；
  5. 启动服务并打开控制台：征得同意后写入 LaunchAgent（写 plist 时把当前的 PATH、代理和镜像设置一起写进去；工作目录和日志不放 `~/Documents`），否则用 nohup 启动临时服务；然后运行 `xhs-wiki open`；
  6. XHS-Downloader 和语音模型交给服务在后台下载（按固定 SHA-256 核对），脚本不等待；LLM Wiki 应用由用户自行安装，xhs-wiki 只检查应用和项目（§6.2）；
  7. 渲染 Skill，写进设置里勾选的位置（默认只有 Claude Code；升级时顺带更新勾选过的其他工具；没检测到 Claude Code 时跳过它的目录）；
  8. 建立命令链接 `~/.local/bin/xhs-wiki`；
  9. 结束提示。

  不需要 Homebrew、ffmpeg、git 或 Xcode 命令行工具。`--mirror cn` 会把 Python 包、Python 安装包和语音模型的下载切到国内镜像（阿里云 PyPI、npmmirror、hf-mirror），后台服务也沿用；安装脚本本身和源码包仍从 GitHub 下载。
- **卸载**：`uninstall.sh` 停止服务（LaunchAgent 和临时服务都会停掉），删除 LaunchAgent、`APP_HOME`、日志和 Skill；**知识库默认保留**。必须同时加上 `--yes` 和 `--purge-kb` 才清理知识库，而且只删本工具生成的文件（索引、主题、来源、暂缓页和 `.xhs-wiki/`），目录里没有 `.xhs-wiki/` 标记时拒绝删除。日志目录和 Skill 目录也只删本工具的文件，目录清空后才删除目录本身；§12 的所有 Skill 位置都会检查，任何一个是符号链接、指向受保护目录或包含知识库时整个卸载先停下。

## 14. 测试和质量门

- 用 pytest。所有测试都在临时目录里进行，用假的浏览器、子进程和模型，不访问网络、不调用真实模型。SDK 类的提供方用真实 SDK 客户端加假的 HTTP 层测试，确保调用方式和 SDK 一致。
- GitHub Actions 在 Apple 芯片（macos-15）和 Intel（macos-15-intel）两种 macOS 15 上跑全部测试。
- 真实网站、真实模型的联调由维护者手动进行，结果不进仓库。
- 发布前要做敏感信息扫描：个人路径、主页编号、签名链接、Key 的特征。开发环境和贡献流程见 [CONTRIBUTING.md](../CONTRIBUTING.md)。
