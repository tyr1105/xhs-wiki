# 参与贡献

感谢你愿意改进 xhs-wiki！提交 issue 或 PR 之前，请先读完这份说明。整体设计见 [docs/architecture.md](docs/architecture.md)。

## 红线（任何贡献都不能违反）

1. **不提交任何个人数据**：代码、测试、注释、文档、截图里，不能出现真实的小红书主页或笔记编号、笔记标题和内容、用户名、本机个人路径（例如 `/Users/<你的名字>/`）、签名链接（`xsec_token`）、Cookie 或 API Key。测试数据一律用代码生成的虚构数据。
2. **不增加任何反检测手段**：本项目的代码不改 UA、不伪造指纹、不注入隐藏脚本、不自动处理验证码。遇到平台拦截只能停下来，并提示用户。（下载原件用的第三方组件 XHS-Downloader 沿用上游默认设置，会模拟 Chrome 的网络特征，README 的[风险与声明](README.md#风险与声明)里有说明；如果你能在不影响下载的前提下去掉这一点，欢迎提交改动。）
3. **只整理用户自己的收藏**：不接受把本项目改成抓取他人内容、批量下载的功能。
4. **测试不访问网络、不调用真实模型、不启动真实的 Chrome 或小红书页面**，外部依赖全部用替身（fake）。

## 开发环境

需要：macOS 13 或更新版本、Python 3.12、[uv](https://docs.astral.sh/uv/)、git。

```bash
git clone https://github.com/tyr1105/xhs-wiki.git
cd xhs-wiki
uv sync            # 建立 .venv，以可编辑方式安装本项目和开发依赖（pytest）
uv run pytest -q   # 运行全部测试：不联网、不调用真实模型
```

只想跑某一个测试文件时：`uv run pytest tests/test_runner.py -q`。

本地试用控制台：

```bash
XHS_WIKI_HOME="$PWD/.tmp-home" XHS_WIKI_SKILLS_ROOT="$PWD/.tmp-home/skills" uv run xhs-wiki serve
```

上面两个环境变量把数据目录和 Skill 安装位置指到仓库内的临时目录，不会动你真实安装的数据。运行时生成的文件都已被 `.gitignore` 忽略。用完删掉 `.tmp-home` 即可。

## 代码规范

- **注释用中文，写清楚"做什么、为什么这样做"**：每个文件开头说明它在整体里的位置，术语第一次出现时用一句话解释。
- **可以预料的失败，抛出带错误代码的异常**（例如 `ValueError("daily_time_invalid")`），不要吞掉异常；新的错误代码要在控制台的大白话对照表（`web/console.html` 里的 `CODE_LABELS`）里补上说明，测试会检查有没有遗漏。
- **写文件先写临时文件，再用 `os.replace` 替换**；私有文件权限 0600，目录 0700。
- **启动子进程一律用参数列表**，禁止 `shell=True`；系统命令写绝对路径。
- **外部依赖都要能注入**（子进程、浏览器、模型、时钟、网络），方便写测试。
- 签名链接、Cookie、Key **绝不进入**日志、状态文件、页面或报错信息。

## 提交 PR 之前

- [ ] `uv run pytest -q` 全部通过，新功能和修复都有对应测试；
- [ ] 改动没有违反上面的红线；可以先跑一遍：`git diff --cached | grep -nE "/Users/|xsec_token=|Cookie:|sk-[A-Za-z0-9]{16,}"`，确认没有输出；
- [ ] 改了用户能看到的行为时，同步更新 README 和 [CHANGELOG.md](CHANGELOG.md)；
- [ ] 一个 PR 只做一件事，描述里写清楚动机、改了什么、怎么测的。

## 报告问题

请使用 issue 模板。贴日志、截图或 `xhs-wiki doctor` 的输出之前，先**打码**：主页链接、笔记编号和标题、`xsec_token`、Cookie、API Key；把 `/Users/<你的用户名>` 换成 `~`。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

## 许可证

提交贡献，即表示你同意以本项目的许可证 [GPL-3.0-or-later](LICENSE) 授权你的贡献。
