#!/usr/bin/env bash
# install.sh —— 准备 xhs-wiki 运行环境并启动本机知识库服务。
# 本文件负责安装流程；组件和语音模型由服务下载，进度显示在控制台。
# 所有执行步骤都在 main 中，完整收到脚本后才开始，支持 curl 管道安装。
# 用法：./install.sh [--yes] [--dry-run] [--from-local <目录>] [--no-open]（完整参数见 --help）
#                    [--app-home <目录>] [--no-launch-agent] [--asr whisper|sensevoice] [--mirror cn]

# 只定义函数，不在文件加载期间执行安装，避免下载中断时执行半份脚本。
main() {
  set -euo pipefail
  umask 077
  # CDPATH 是 cd 查找相对路径时额外搜索的目录列表。用户导出了它时，
  # cd xhs-wiki 可能跳到别处，还会把目录名打印到标准输出、混进 $(...) 的
  # 结果里。脚本内部一律不用它。
  unset CDPATH
  XHS_WIKI_REPO="${XHS_WIKI_REPO:-github.com/tyr1105/xhs-wiki}"
  XHS_WIKI_REF="${XHS_WIKI_REF:-main}"
  UV_VERSION="0.12.13"
  UV_INSTALL_URL="https://astral.sh/uv/${UV_VERSION}/install.sh"
  ONLY_BINARY="av,ctranslate2,onnxruntime,numpy,tokenizers,sherpa-onnx,cryptography,lxml,curl-cffi,pydantic-core,pyobjc-core"
  LAUNCH_AGENT_LABEL="io.github.xhs-wiki.service"
  CONSOLE_PORT="18765"
  ASSUME_YES="no"
  DRY_RUN="no"
  FROM_LOCAL=""
  MIRROR=""
  APP_HOME="${XHS_WIKI_HOME:-$HOME/Library/Application Support/xhs-wiki}"
  NO_LAUNCH_AGENT="no"
  # --no-open：装完不自动打开浏览器（远程 ssh 安装、自动化测试等没有桌面可用的场景）
  NO_OPEN="no"
  # 默认模型单独列出；只有首次安装或显式 --asr 才写入用户设置。
ASR_MODEL="whisper"
  ASR_EXPLICIT="no"
  TEMP_FILE=""
  # 第 4 步停掉旧服务之后如果再失败，提示用户怎么把旧服务
  # 恢复起来（旧服务已经停了、不会自己重启）。停服务时由 stop_old_service
  # 填写，新服务启动后清空。
  RECOVERY_HINT=""

  # 出错只解释当前步骤，具体依赖错误由原命令保留，避免误导为网络问题。
  die() {
    printf '\n安装失败：%s\n处理办法：%s\n' "$1" "$2" >&2
    if [ -n "$RECOVERY_HINT" ]; then printf '%s\n' "$RECOVERY_HINT" >&2; fi
    exit 1
  }
  # 每一步给出可读的中文说明；命令回显单独进入 stderr，不能污染生成文件。
  note() { printf '\n==> %s\n' "$*"; }
  # 生成可复制的 shell 参数，仅用于打印。旧版 Bash 的 %q 会拆开某些
  # 中文字节，因此特殊参数用单引号包住，内部单引号写成 '\''（先结束
  # 单引号、放一个转义的单引号、再重新开始单引号）。
  # 替换内容先放进变量再替换。macOS 自带的 bash 3.2 在
  # ${1//\'/...} 这种写法里会把替换串中的反斜杠原样保留，得到的命令
  # 无法复制运行；放进变量后 3.2 和新版 bash 结果一致。
  quote_arg() {
    local single_quote="'" escaped_quote="'\\''"
    if [[ "$1" =~ ^[a-zA-Z0-9_./:=,@+-]+$ ]]; then
      printf '%s' "$1"
    else
      printf "'%s'" "${1//$single_quote/$escaped_quote}"
    fi
  }
  run() {
    local argument
    printf '    $' >&2
    for argument in "$@"; do printf ' ' >&2; quote_arg "$argument" >&2; done
    printf '\n' >&2
    if [ "$DRY_RUN" = "no" ]; then "$@"; fi
  }
  # 真正尝试打开终端；从 /dev/tty 读取，不能吃掉 curl | bash 的脚本输入。
  confirm() {
    if [ "$DRY_RUN" = "yes" ]; then
      printf '    [dry-run] 将询问：%s（自动同意）\n' "$1"; return 0
    fi
    [ "$ASSUME_YES" = "yes" ] && return 0
    local answer
    if ! { exec 3<>/dev/tty; } 2>/dev/null; then
      die "non_interactive：当前无法交互确认" "请在终端运行；已确认安装时可加 --yes"
    fi
    printf '%s [y/N] ' "$1" >&3
    if ! read -r answer <&3; then exec 3>&-; return 1; fi
    exec 3>&-
    case "$answer" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
  }
  # 临时文件统一清理，失败不会留下半份 plist 或未下载完整的安装器。
  cleanup_temp() { if [ -n "$TEMP_FILE" ]; then rm -f -- "$TEMP_FILE"; fi; }
  trap cleanup_temp EXIT

  # 逐级规范化已有父目录，避免首次安装使用 /var、重装变成 /private/var
  # 后把同一安装误判成另一个目录。此操作只读，不创建目标目录。
  canonical_path() {
    local value="$1" parent leaf
    if [ -d "$value" ]; then
      # 相对路径不经过 CDPATH（main 开头已 unset），cd 的输出丢弃，只取 pwd -P。
      (cd -P -- "$value" >/dev/null 2>&1 && pwd -P)
    else
      parent="$(dirname -- "$value")"; leaf="$(basename -- "$value")"
      [ "$parent" != "$value" ] || return 1
      parent="$(canonical_path "$parent")" || return 1
      case "$leaf" in
        .) printf '%s\n' "$parent" ;;
        ..) dirname -- "$parent" ;;
        *) printf '%s/%s\n' "${parent%/}" "$leaf" ;;
      esac
    fi
  }

  # 取已存在路径的"设备号:inode"（磁盘上的身份证号）。
  # macOS 默认的 APFS 不区分大小写，/System/Volumes/Data/... 又是 /Users
  # 等目录的别名，同一个目录可以有好几种写法，只比较字符串会被绕过；
  # 比较身份证号就不会。路径不存在时返回失败。
  file_identity() { /usr/bin/stat -f '%d:%i' -- "$1" 2>/dev/null; }
  # $2 是否就是 $1、或在 $1 里面：先比较字符串，再从 $2 往上逐级比较身份。
  same_or_inside() {
    local ancestor="$1" level="$2" ancestor_id level_id
    case "$level/" in "$ancestor/"*) return 0 ;; esac
    ancestor_id="$(file_identity "$ancestor")" || return 1
    while [ -n "$level" ]; do
      if level_id="$(file_identity "$level")" && [ "$level_id" = "$ancestor_id" ]; then return 0; fi
      [ "$level" != / ] || return 1
      # 用参数展开去掉最后一级（相当于 dirname，但不用另起子进程）。
      level="${level%/*}"; [ -n "$level" ] || level=/
    done
    return 1
  }
  # 程序目录不能是主目录、常用系统目录，也不能是它们的上级（和
  # uninstall.sh 的同名检查一致）。
  protected_path() {
    local target="$1" protected
    [ "$target" != / ] || return 0
    for protected in /Users /System/Volumes/Data "$HOME" "$HOME/Documents" "$HOME/Desktop" \
        "$HOME/Downloads" "$HOME/Movies" "$HOME/Music" "$HOME/Pictures" "$HOME/Public" \
        "$HOME/Library" "$HOME/Library/Application Support"; do
      protected="$(canonical_path "$protected")" || return 0
      same_or_inside "$target" "$protected" && return 0
    done
    return 1
  }
  # 安装前核对程序目录。以前 --app-home 不做任何检查，传 ~
  # 时会把主目录改成 0700，并往主目录里写安装标记、settings.json 和 venv。
  # 允许：不存在；空目录；已有本工具安装标记；或者是加标记之前的旧版
  # 安装（有能读出 kb_root 的 settings.json）。其余非空目录一律拒绝。
  validate_app_home() {
    local entries
    protected_path "$APP_HOME" &&
      die "app_home_unsafe：程序目录不能是主目录、系统目录或它们的上级" "换一个专用的空目录，或去掉 --app-home 使用默认位置"
    if [ -e "$APP_HOME" ] || [ -L "$APP_HOME" ]; then
      [ -d "$APP_HOME" ] && [ ! -L "$APP_HOME" ] ||
        die "app_home_invalid：程序目录不是普通目录" "检查 --app-home 指向的路径"
      [ ! -f "$APP_HOME/.xhs-wiki-install" ] || return 0
      entries="$(ls -A -- "$APP_HOME")" || die "app_home_invalid：无法读取程序目录" "检查目录权限后重试"
      [ -n "$entries" ] || return 0
      if [ -f "$APP_HOME/settings.json" ] &&
          /usr/bin/plutil -extract kb_root raw -o - "$APP_HOME/settings.json" >/dev/null 2>&1; then
        return 0
      fi
      die "app_home_not_empty：程序目录不是空的，也没有 xhs-wiki 的安装标记" "请换一个专用的空目录作为 --app-home；如果它是之前安装失败留下的，确认无误后删除再重试：rm -rf -- $(quote_arg "$APP_HOME")"
    fi
  }
  # 已存在的 LaunchAgent 配置是否属于本安装目录：优先看其中的
  # XHS_WIKI_HOME，没有时看启动程序是否在本目录的 venv 里。
  plist_belongs_here() {
    local configured_home program
    [ -f "$PLIST_PATH" ] && [ ! -L "$PLIST_PATH" ] || return 1
    configured_home="$(/usr/bin/plutil -extract EnvironmentVariables.XHS_WIKI_HOME raw -o - "$PLIST_PATH" 2>/dev/null || true)"
    program="$(/usr/bin/plutil -extract ProgramArguments.0 raw -o - "$PLIST_PATH" 2>/dev/null || true)"
    if [ -n "$configured_home" ]; then
      configured_home="$(canonical_path "$configured_home")" || return 1
      [ "$configured_home" = "$APP_HOME" ]; return
    fi
    case "$program" in
      /*/venv/bin/*) program="$(canonical_path "${program%%/venv/bin/*}")/venv/bin/$(basename "$program")" || return 1 ;;
      *) return 1 ;;
    esac
    [[ "$program" == "$VENV_BIN/"* ]]
  }

  # 先解析参数和检查安装来源，任何建目录、下载或停止旧服务都在它们之后。
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --yes) ASSUME_YES="yes" ;;
      --dry-run) DRY_RUN="yes" ;;
      --from-local|--app-home|--asr|--mirror)
        [ "$#" -ge 2 ] && [ -n "$2" ] || die "参数 $1 缺少值" "运行 bash install.sh --help 查看用法"
        case "$1" in
          --from-local) FROM_LOCAL="$2" ;;
          --app-home) APP_HOME="$2" ;;
          --mirror)
            case "$2" in cn) MIRROR="cn" ;;
              *) die "mirror_invalid：--mirror 只能是 cn" "例如：bash install.sh --mirror cn" ;; esac ;;
          --asr)
            case "$2" in whisper|sensevoice) ASR_MODEL="$2"; ASR_EXPLICIT="yes" ;;
              *) die "asr_invalid：--asr 只能是 whisper 或 sensevoice" "例如：bash install.sh --from-local . --asr sensevoice" ;; esac ;;
        esac
        shift ;;
      --no-launch-agent) NO_LAUNCH_AGENT="yes" ;;
      --no-open) NO_OPEN="yes" ;;
      -h|--help)
        echo '用法：bash install.sh [--yes] [--dry-run] [--from-local <目录>] [--app-home <目录>] [--no-launch-agent] [--no-open] [--asr whisper|sensevoice] [--mirror cn]'
        return 0 ;;
      *) die "未知参数 $1" "运行 bash install.sh --help 查看用法" ;;
    esac
    shift
  done
  CURRENT_UID="$(id -u)"
  [ "$CURRENT_UID" != "0" ] || die "root_not_allowed：不要用 root 或 sudo 运行" "以普通用户身份运行：bash install.sh --from-local ."
  # 没有终端可以提问、又没加 --yes 时，在任何副作用之前就
  # 停下。以前要到第 5 步第一次提问才发现，那时第 4 步已经停掉了旧服务、
  # 建好目录和 venv、装完了依赖——旧服务不会自己重启，留下半装状态。
  # （IDE 或桌面 App 的运行按钮、CI、不带 -t 的 ssh 都没有终端。）
  if [ "$DRY_RUN" = "no" ] && [ "$ASSUME_YES" = "no" ]; then
    if ! { exec 3<>/dev/tty; } 2>/dev/null; then
      die "non_interactive：当前无法交互确认" "请在终端里运行；确认要安装、并同意安装 LaunchAgent 和命令链接时，可加 --yes"
    fi
    exec 3>&-
  fi
  # 镜像只影响本次安装及其启动的后台服务，不修改用户的 shell 配置。
  # export 让 uv 和 nohup 子进程继承；print-launch-agent 再把它们写进 plist。
  if [ "$MIRROR" = "cn" ]; then
    export UV_DEFAULT_INDEX="https://mirrors.aliyun.com/pypi/simple/"
    export UV_PYTHON_INSTALL_MIRROR="https://registry.npmmirror.com/-/binary/python-build-standalone"
    export HF_ENDPOINT="https://hf-mirror.com"
  fi
  if [ -n "$FROM_LOCAL" ]; then
    # cd 的输出丢弃（导出了 CDPATH 时 cd 会把目录名打印出来，
    # 混进结果；main 开头也已 unset CDPATH），-P 取物理路径。
    FROM_LOCAL="$(cd -P -- "$FROM_LOCAL" >/dev/null 2>&1 && pwd -P)" || die "local_source_invalid：本地仓库目录不存在" "在项目目录运行：bash install.sh --from-local ."
  fi
  APP_HOME="$(canonical_path "$APP_HOME")" || die "app_home_invalid：无法解析程序目录" "检查 --app-home 目录后重试"
  VENV="$APP_HOME/venv"
  VENV_PYTHON="$VENV/bin/python"
  VENV_BIN="$VENV/bin"
  PLIST_PATH="$HOME/Library/LaunchAgents/$LAUNCH_AGENT_LABEL.plist"
  validate_app_home
  LOG_DIR="${XHS_WIKI_LOG_DIR:-$HOME/Library/Logs/xhs-wiki}"
  SKILLS_ROOT="${XHS_WIKI_SKILLS_ROOT:-$HOME/.claude/skills}"
  CHROME_APP="${XHS_WIKI_CHROME_APP:-/Applications/Google Chrome.app}"
  export XHS_WIKI_HOME="$APP_HOME" XHS_WIKI_LOG_DIR="$LOG_DIR" XHS_WIKI_SKILLS_ROOT="$SKILLS_ROOT"
  SERVE_COMMAND="env XHS_WIKI_HOME=$(quote_arg "$APP_HOME") XHS_WIKI_LOG_DIR=$(quote_arg "$LOG_DIR") $(quote_arg "$VENV_BIN/xhs-wiki") serve"
  OPEN_COMMAND="env XHS_WIKI_HOME=$(quote_arg "$APP_HOME") $(quote_arg "$VENV_BIN/xhs-wiki") open"

  # lsof 返回 1 表示没有监听者；其他失败不能当成端口空闲。
  listener_pids() {
    local status=0
    lsof -nP -iTCP:18765 -sTCP:LISTEN -t || status=$?
    [ "$status" -le 1 ] || die "port_check_failed：无法查询 18765 端口" "确认 lsof 可用后重新运行"
  }
  # 只接受本安装目录的入口或 Python 进程，并且必须是 serve 子命令。
  owns_process() {
    local command_line process_home
    command_line="$(ps -p "$1" -o command=)" || return 1
    case "$command_line" in /*/venv/bin/*) ;; *) return 1 ;; esac
    process_home="${command_line%%/venv/bin/*}"
    process_home="$(canonical_path "$process_home")" || return 1
    [ "$process_home" = "$APP_HOME" ] &&
      [[ "$command_line" =~ (xhs-wiki|xhs_wiki\.cli)[[:space:]]+serve([[:space:]]|$) ]]
  }
  # 先验证全部监听者，再停服务，避免升级代码时旧进程继续占用端口。
  stop_old_service() {
    local pids pid loaded="no" elapsed=0 kill_bin
    pids="$(listener_pids)"
    for pid in $pids; do
      case "$pid" in ''|*[!0-9]*) die "port_check_failed：进程号无效" "检查 lsof 输出后重试" ;; esac
      owns_process "$pid" || die "port_in_use：18765 端口被其他程序占用（PID ${pid}）" "请先退出占用端口的程序；可运行：lsof -nP -iTCP:18765 -sTCP:LISTEN"
    done
    if launchctl print "gui/$CURRENT_UID/$LAUNCH_AGENT_LABEL" >/dev/null 2>&1; then loaded="yes"; fi
    if [ "$loaded" = "yes" ]; then
      # 无监听者的残留 LaunchAgent 也可能自动重启，只在配置归属一致时卸载。
      plist_belongs_here ||
        die "launch_agent_home_mismatch：同名后台服务属于其他安装目录" "请使用原安装目录卸载旧服务后重试"
      run launchctl bootout "gui/$CURRENT_UID/$LAUNCH_AGENT_LABEL" || die "旧服务 bootout 失败" "运行：launchctl bootout gui/$CURRENT_UID/$LAUNCH_AGENT_LABEL"
      # 之后的步骤再失败时，告诉用户旧服务已停、怎么恢复。
      RECOVERY_HINT="提示：旧的后台服务已经停止。修复上面的问题后重新运行本安装脚本即可；想先恢复旧服务，可运行：launchctl bootstrap gui/$CURRENT_UID $(quote_arg "$PLIST_PATH")"
    fi
    # nohup 服务没有 LaunchAgent；bootout 后仍存在的监听者也需重新核对身份。
    pids="$(listener_pids)"
    for pid in $pids; do
      owns_process "$pid" || die "service_identity_changed：监听者已变化" "请检查 18765 端口后重试"
      kill_bin="$(type -P kill)" || die "kill_missing：找不到停止进程命令" "确认系统 kill 命令可用后重试"
      run "$kill_bin" -TERM "$pid" || die "service_stop_failed：旧服务未能停止" "请先停止原服务后重试"
      if [ -z "$RECOVERY_HINT" ]; then
        RECOVERY_HINT="提示：旧的临时服务已经停止。修复上面的问题后重新运行本安装脚本即可；想先恢复旧服务，可运行：nohup $SERVE_COMMAND &"
      fi
    done
    while true; do
      pids="$(listener_pids)" || die "port_check_failed：等待停止时无法查询端口" "确认 lsof 可用且旧服务已退出后重试"
      [ -n "$pids" ] || break
      for pid in $pids; do
        owns_process "$pid" || die "service_identity_changed：等待期间监听者已变化" "请检查 18765 端口后重试"
      done
      [ "$elapsed" -lt 10 ] || die "service_stop_timeout：旧服务仍占用端口" "请确认旧服务已退出，再运行安装脚本"
      sleep 1; elapsed=$((elapsed + 1))
    done
  }
  # 健康接口不含用户数据；绕开代理且把每次请求限制在剩余总预算内。
  wait_for_console() {
    local deadline=$((SECONDS + 60)) remaining
    echo "    等待服务在端口 $CONSOLE_PORT 起来（最多 60 秒）……"
    while [ "$SECONDS" -lt "$deadline" ]; do
      remaining=$((deadline - SECONDS))
      [ "$remaining" -gt 0 ] || break
      if curl --noproxy '*' -fsS --max-time "$remaining" "http://127.0.0.1:$CONSOLE_PORT/healthz" >/dev/null 2>&1; then
        echo "    服务已启动 ✓"; return 0
      fi
      [ "$SECONDS" -ge "$deadline" ] || sleep 1
    done
    die "service_timeout：服务 60 秒内没有响应" "查看日志目录 ${LOG_DIR}；或运行：$SERVE_COMMAND"
  }

  if [ "$DRY_RUN" = "yes" ]; then
    echo "=== dry-run 模式：只打印要做的事，不安装任何东西、不下载任何文件 ==="
    echo "APP_HOME=$APP_HOME"
    echo "语音模型：显式 --asr 或首次安装时使用 ${ASR_MODEL}，否则保留现有设置"
    [ -z "$FROM_LOCAL" ] || echo "从本地仓库安装：$FROM_LOCAL"
  fi
  if [ "$MIRROR" = "cn" ]; then
    echo "    使用国内镜像（仅本次安装和后台服务）；安装脚本本身和源码包仍然从 GitHub 下载，需要能访问 GitHub。"
    if [ "$DRY_RUN" = "yes" ]; then
      echo "    export UV_DEFAULT_INDEX=$UV_DEFAULT_INDEX"
      echo "    export UV_PYTHON_INSTALL_MIRROR=$UV_PYTHON_INSTALL_MIRROR"
      echo "    export HF_ENDPOINT=$HF_ENDPOINT"
    fi
  fi
  note "步骤 1/9 检查系统环境（需要 macOS 13 或更新版本、至少 10 GiB 磁盘剩余）"
  if [ "$DRY_RUN" = "yes" ]; then
    echo "    将检查 macOS 版本、芯片架构（Apple/Intel 均可）和磁盘剩余空间"
  else
    MACOS_VERSION="$(sw_vers -productVersion 2>/dev/null)" || die "无法识别 macOS 版本" "确认使用 macOS 13 或更新版本"
    MACOS_MAJOR="${MACOS_VERSION%%.*}"
    case "$MACOS_MAJOR" in ''|*[!0-9]*) die "macos_invalid：无法识别系统版本" "确认使用 macOS 13 或更新版本" ;; esac
    [ "$MACOS_MAJOR" -ge 13 ] || die "macOS 版本太旧" "先升级到 macOS 13 或更新版本"
    echo "    macOS ${MACOS_VERSION}；芯片架构：$(uname -m) ✓"
    FREE_KB="$(df -k "$HOME" | awk 'NR==2 {print $4}')"
    case "$FREE_KB" in ''|*[!0-9]*) die "disk_check_failed：无法读取磁盘空间" "确认主目录所在磁盘可用" ;; esac
    [ "$FREE_KB" -ge $((10 * 1024 * 1024)) ] || die "磁盘剩余空间不足 10 GiB" "清理磁盘后重试"
  fi
  note "步骤 2/9 检查 Google Chrome"
  if [ "$DRY_RUN" = "yes" ]; then
    echo "    将检查 $CHROME_APP 是否存在；没有就给出下载地址并退出"
  elif [ ! -d "$CHROME_APP" ]; then
    die "没有找到 Google Chrome" "从 https://www.google.com/chrome/ 下载安装后重试"
  fi
  note "步骤 3/9 检查 uv（Python 环境管理器）"
  if [ "$DRY_RUN" = "yes" ]; then
    echo "    将检查 uv；没有时询问，完整下载官方 ${UV_VERSION} 安装脚本后才执行（${UV_INSTALL_URL}）"
  elif ! command -v uv >/dev/null 2>&1; then
    confirm "没装 uv，现在用官方脚本安装版本 $UV_VERSION 吗？" || die "用户跳过 uv 安装" "安装 uv 后重试（https://docs.astral.sh/uv/）"
    TEMP_FILE="$(mktemp "${TMPDIR:-/tmp}/xhs-wiki-uv.XXXXXX")"
    run curl -fsSL "$UV_INSTALL_URL" -o "$TEMP_FILE" || die "uv_download_failed：下载 uv 安装脚本失败" "检查网络或代理后重试"
    run sh "$TEMP_FILE" || die "uv_install_failed：执行 uv 安装脚本失败" "查看上方安装器错误，检查目录权限与系统兼容性"
    rm -f -- "$TEMP_FILE"; TEMP_FILE=""
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 || die "uv 安装器完成但未找到命令" "检查安装器报告的目标目录，并将其加入 PATH 后重试"
  fi
  note "步骤 4/9 建立数据目录和 Python 3.12 虚拟环境，安装 xhs-wiki 本体"
  if [ "$DRY_RUN" = "yes" ]; then
    echo "    安装前用 lsof 核对 18765；仅停止本 APP_HOME 的 xhs-wiki serve（launchctl print / bootout 或 kill），其他程序占用时拒绝继续"
    echo "    将创建 ${APP_HOME}（0700）与安装标记 .xhs-wiki-install（0600）"
    run uv venv --python 3.12 "$VENV"
  else
    stop_old_service
    # 已存在但不可运行的 venv 不静默清空，明确给出只删除虚拟环境的修复命令。
    # 所有 "$VENV_PYTHON" 调用都加 -P，
    # 不把当前目录放进模块搜索路径——curl | bash 通常在主目录运行，主目录里
    # 要是有 plistlib.py、json.py 这类与标准库同名的文件，不加 -P 时安装过程
    # 会执行它们。
    if [ -e "$VENV" ] || [ -L "$VENV" ]; then
      if ! "$VENV_PYTHON" -P -c 'import sys' >/dev/null 2>&1; then
        REBUILD_COMMAND="rm -rf -- $(quote_arg "$VENV")"
        die "venv_broken：虚拟环境损坏" "仅删除虚拟环境后重新安装：${REBUILD_COMMAND}；随后重新运行本安装脚本"
      fi
    fi
    mkdir -p "$APP_HOME"; chmod 700 "$APP_HOME"
    TEMP_FILE="$(mktemp "$APP_HOME/.install-marker.XXXXXX")"
    printf 'xhs-wiki\n' > "$TEMP_FILE"
    chmod 600 "$TEMP_FILE"; mv -f -- "$TEMP_FILE" "$APP_HOME/.xhs-wiki-install"; TEMP_FILE=""
    if [ ! -x "$VENV_PYTHON" ]; then
      run uv venv --python 3.12 "$VENV" || die "创建虚拟环境失败" "查看上方 uv 错误，检查 Python 下载和磁盘空间"
    fi
  fi
  if [ -n "$FROM_LOCAL" ]; then
    run uv pip install --python "$VENV_PYTHON" --only-binary "$ONLY_BINARY" "$FROM_LOCAL" || die "安装 xhs-wiki 依赖失败" "查看上方 uv 错误，检查本地源码、依赖兼容性及下载连接"
  else
    # 直接安装分支源码压缩包，避免依赖尚未安装命令行开发工具的 Mac。
    run uv pip install --python "$VENV_PYTHON" --only-binary "$ONLY_BINARY" "https://${XHS_WIKI_REPO}/archive/refs/heads/${XHS_WIKI_REF}.tar.gz" || die "从远程安装 xhs-wiki 失败" "查看上方 uv 错误，检查仓库地址、分支名称、依赖兼容性及下载连接"
  fi

  note "步骤 5/9 启动后台服务并打开控制台"
  # 所有交互都在 open 之前结束；第 8 步仅执行此处已经确认的决定。
  INSTALL_AGENT="no"
  if [ "$NO_LAUNCH_AGENT" = "no" ] && confirm "安装 LaunchAgent（登录后自动在后台运行）吗？"; then INSTALL_AGENT="yes"; fi
  CREATE_LINK="no"
  if confirm "把 xhs-wiki 命令链接到 ~/.local/bin 吗？"; then CREATE_LINK="yes"; fi
  if [ "$ASR_EXPLICIT" = "yes" ] || [ ! -f "$APP_HOME/settings.json" ]; then
    run "$VENV_PYTHON" -P -c 'import sys; from xhs_wiki.store import ServiceStore; ServiceStore(sys.argv[1]).update_settings({"asr_model": sys.argv[2]})' "$APP_HOME" "$ASR_MODEL" || die "写入语音模型设置失败" "检查数据目录权限和上方错误"
  else
    echo "    保留 settings.json 中已有的语音模型设置"
  fi
  if [ "$DRY_RUN" = "yes" ]; then
    if [ "$INSTALL_AGENT" = "yes" ]; then
      echo "    已有 $PLIST_PATH 时先核对归属，属于其他安装目录就拒绝覆盖"
      echo "    将生成含 XHS_WIKI_HOME=$APP_HOME 的临时 plist，plutil -lint 通过后 mv 到 $PLIST_PATH"
      run launchctl bootstrap "gui/$CURRENT_UID" "$PLIST_PATH"
    else
      echo "    已有属于本程序目录的 $PLIST_PATH 时将删除它（避免下次登录又自动启动）"
      echo "    将用 nohup 启动临时服务；重启电脑后需要手动运行 xhs-wiki serve"
      run nohup "$VENV_BIN/xhs-wiki" serve
    fi
    echo "    用 curl --noproxy '*' -fsS http://127.0.0.1:$CONSOLE_PORT/healthz 探活，最多等 60 秒"
    if [ "$NO_OPEN" = "yes" ]; then
      echo "    --no-open：不自动打开浏览器，安装完成后运行 xhs-wiki open 打开控制台"
    else
      echo "    然后马上打开控制台："
      run "$VENV_BIN/xhs-wiki" open
    fi
  else
    mkdir -p "$LOG_DIR"; chmod 700 "$LOG_DIR"
    if [ "$INSTALL_AGENT" = "yes" ]; then
      # 已有同名配置时，不管它当前有没有被加载，都先核对归属。
      # 以前只在"已加载"时核对：正式安装的服务碰巧没加载时，用 --app-home
      # 做隔离安装会直接覆盖它的 plist，下次登录启动的就成了测试目录的服务。
      if { [ -e "$PLIST_PATH" ] || [ -L "$PLIST_PATH" ]; } && ! plist_belongs_here; then
        die "launch_agent_home_mismatch：已有的 LaunchAgent 配置属于其他安装目录（或无法识别）" "请先用原安装目录的 uninstall.sh 卸载；确认这份配置已经没用时，也可以删除后重试：rm -f -- $(quote_arg "$PLIST_PATH")"
      fi
      mkdir -p "$(dirname "$PLIST_PATH")" || die "无法创建 LaunchAgents 目录" "检查主目录权限后重试"
      [ -w "$(dirname "$PLIST_PATH")" ] || die "LaunchAgents 目录不可写" "检查目录权限后重试"
      TEMP_FILE="$(mktemp "$PLIST_PATH.tmp.XXXXXX")" || die "无法创建 plist 临时文件" "检查 LaunchAgents 目录的权限和磁盘空间"
      # 生成时不经过 run；环境变量写入 plist，launchd 才能使用自定义数据位置。
      "$VENV_PYTHON" -P -m xhs_wiki.cli print-launch-agent --python "$VENV_PYTHON" > "$TEMP_FILE" || die "生成 LaunchAgent 配置失败" "检查上方命令错误和 LaunchAgents 目录的写入权限"
      "$VENV_PYTHON" -P - "$TEMP_FILE" <<'PY' || die "plist_environment_failed：无法写入后台环境" "检查配置生成器输出和目录权限；原配置已保留"
# 将脚本已确定的目录传给 launchd；不依赖终端环境被后台继承。
import os
import plistlib
import sys
from pathlib import Path
path = Path(sys.argv[1])
with path.open('rb') as source:
    data = plistlib.load(source)
data.setdefault('EnvironmentVariables', {}).update({
    name: os.environ[name]
    for name in ('HOME', 'XHS_WIKI_HOME', 'XHS_WIKI_LOG_DIR', 'XHS_WIKI_SKILLS_ROOT')
})
# 日志位置也写入 plist，确保 launchd 打开日志时与已创建的目录一致。
data['StandardOutPath'] = str(Path(os.environ['XHS_WIKI_LOG_DIR']) / 'daemon.log')
data['StandardErrorPath'] = str(Path(os.environ['XHS_WIKI_LOG_DIR']) / 'daemon.err.log')
with path.open('wb') as target:
    plistlib.dump(data, target)
PY
      /usr/bin/plutil -lint "$TEMP_FILE" || die "plist_invalid：生成的配置无效" "保留原配置，请检查上方生成与校验错误"
      chmod 600 "$TEMP_FILE"; mv -f -- "$TEMP_FILE" "$PLIST_PATH"; TEMP_FILE=""
      run launchctl bootstrap "gui/$CURRENT_UID" "$PLIST_PATH" || die "launchctl bootstrap 失败" "运行：launchctl bootstrap gui/$CURRENT_UID $(quote_arg "$PLIST_PATH")"
    else
      # 这次选择不装 LaunchAgent，但旧安装留下的、属于本程序
      # 目录的 plist 还在（第 4 步只 bootout、没删文件），下次登录 launchd
      # 又会把它加载起来，等于"选了不装却照样开机自启"。删掉它。
      if plist_belongs_here; then
        run rm -f -- "$PLIST_PATH" || die "plist_remove_failed：无法删除旧的 LaunchAgent 配置" "手动删除后重试：rm -f -- $(quote_arg "$PLIST_PATH")"
      fi
      echo "    用 nohup 启动临时服务（重启电脑后需要手动运行 xhs-wiki serve）"
      nohup "$VENV_BIN/xhs-wiki" serve >> "$LOG_DIR/serve.log" 2>&1 &
    fi
    # 新服务已经交给 launchd 或 nohup 启动，之后失败不再提示"旧服务已停止"。
    RECOVERY_HINT=""
    wait_for_console
    if [ "$NO_OPEN" = "yes" ]; then
      echo "    --no-open：没有自动打开浏览器；运行 xhs-wiki open 打开控制台。完整命令：${OPEN_COMMAND}"
    elif run "$VENV_BIN/xhs-wiki" open; then
      echo "    控制台已在浏览器打开 ✓"
    else
      echo "    自动打开失败；运行 xhs-wiki open 打开控制台。完整命令：$OPEN_COMMAND" >&2
    fi
  fi
  note "步骤 6/9 下载组件和语音模型（由后台服务在后台下载，本脚本不等待）"
  echo "    XHS-Downloader 和设置中选定的语音模型由后台服务下载，进度显示在控制台顶部"
  note "步骤 7/9 安装 Claude Skill（让 AI 能按意图查你的知识库）"
  if [ "$DRY_RUN" = "yes" ]; then
    echo "    检测到 Claude Code 后写入 $SKILLS_ROOT/xhs-wiki/SKILL.md"
  elif command -v claude >/dev/null 2>&1 || [ -d "$SKILLS_ROOT" ] || [ -d "$HOME/.claude" ]; then
    run "$VENV_PYTHON" -P -c 'from xhs_wiki import config, skill; from xhs_wiki.store import ServiceStore; skill.install(ServiceStore(config.app_home()).load_settings())' || die "Skill 写入失败" "检查 $SKILLS_ROOT 是否可写以及上方错误"
  else
    echo "    没检测到 Claude Code，跳过 Skill"
  fi
  note "步骤 8/9 建立命令软链接 ~/.local/bin/xhs-wiki"
  if [ "$CREATE_LINK" = "yes" ]; then
    run mkdir -p "$HOME/.local/bin"
    run ln -sf "$VENV_BIN/xhs-wiki" "$HOME/.local/bin/xhs-wiki" || die "软链接创建失败" "检查目标目录是否可写"
  else
    echo "    已跳过命令软链接；可使用完整命令：$OPEN_COMMAND"
  fi
  note "步骤 9/9 完成"
  echo "    运行 xhs-wiki open 打开控制台，后续请看网页。若命令不在 PATH 中，可运行：$OPEN_COMMAND"
  if [ "$DRY_RUN" = "yes" ]; then
    echo "=== dry-run 结束：以上就是要执行的 9 个步骤，没有创建或修改任何东西 ==="
  else
    echo "安装完成。首次向导和组件下载进度都在控制台。"
  fi
}
main "$@"
