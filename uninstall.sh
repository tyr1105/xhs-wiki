#!/usr/bin/env bash
# uninstall.sh —— 停止 xhs-wiki 服务，并清理经过归属核对的安装文件。
# 本文件是安装流程的逆向入口：默认保留知识库；显式同时传入 --yes 和
# --purge-kb 时，只清理带本工具标记的知识库中的五类生成内容。
# 用法：./uninstall.sh [--yes] [--dry-run] [--app-home <目录>] [--purge-kb]

# 输出稳定的错误代码和中文处理办法；不回显进程命令行或设置全文。
die() {
  printf '\n错误 [%s]：%s\n' "$1" "$2" >&2
  exit 1
}

# 生成可复制的 shell 参数，仅用于打印：参数用单引号包住，内部单引号写成
# '\''。替换内容先放进变量再替换——macOS 自带的 bash 3.2 在
# ${1//\'/...} 这种写法里会保留替换串中的反斜杠，打印出的命令没法运行。
quote_arg() {
  local single_quote="'" escaped_quote="'\\''"
  printf "'%s'" "${1//$single_quote/$escaped_quote}"
}

# 步骤说明输出到终端；真正的命令回显走标准错误，避免污染重定向文件。
note() { printf '\n==> %s\n' "$*"; }
run() {
  local argument
  printf '    $' >&2
  for argument in "$@"; do
    printf ' ' >&2
    quote_arg "$argument" >&2
  done
  printf '\n' >&2
  if [ "$DRY_RUN" = "no" ]; then "$@"; fi
}

# 交互从真正打开成功的终端读取，不能读 curl | bash 的脚本标准输入。
confirm() {
  local answer
  if [ "$DRY_RUN" = "yes" ] || [ "$ASSUME_YES" = "yes" ]; then return 0; fi
  if ! { exec 3<>/dev/tty; } 2>/dev/null; then
    printf '%s\n' '非交互环境，无法询问；同意卸载请加 --yes。' >&2
    return 1
  fi
  printf '%s [y/N] ' "$1" >&3
  if ! read -r answer <&3; then answer=""; fi
  exec 3>&-
  case "$answer" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# 展开当前用户的 ~；不通过 eval 解释输入，避免路径被当作 shell 命令。
expand_home() {
  case "$1" in
    '~') printf '%s\n' "$HOME" ;;
    '~/'*) printf '%s/%s\n' "$HOME" "${1#\~/}" ;;
    '~'*) return 1 ;;
    *) printf '%s\n' "$1" ;;
  esac
}

# 得到物理绝对路径（解析已有父目录中的符号链接）。不存在的末级目录
# 也能预览，但不创建它；因此 dry-run 不会为了规范化路径产生副作用。
# cd 的输出丢弃——导出了 CDPATH 时 cd 会把目录名打印出来，
# 混进结果（main 开头也已 unset CDPATH）。
canonical_path() {
  local value parent leaf
  value="$(expand_home "$1")" || return 1
  [ -n "$value" ] || return 1
  case "$value" in *$'\n'*|*$'\r'*) return 1 ;; esac
  if [ -d "$value" ]; then
    (cd -P -- "$value" >/dev/null 2>&1 && pwd -P)
  else
    parent="$(dirname -- "$value")"
    leaf="$(basename -- "$value")"
    [ "$parent" != "$value" ] || return 1
    parent="$(canonical_path "$parent")" || return 1
    case "$leaf" in
      .) printf '%s\n' "$parent" ;;
      ..) dirname -- "$parent" ;;
      *) printf '%s/%s\n' "${parent%/}" "$leaf" ;;
    esac
  fi
}

# 取已存在路径的"设备号:inode"（磁盘上的身份证号）。macOS
# 默认的 APFS 不区分大小写（~/documents 就是 ~/Documents），
# /System/Volumes/Data/Users/... 又是 /Users/... 的别名（固件链接），同一个
# 目录有好几种写法；以前只比较字符串，这些写法都能绕过受保护目录的检查。
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

# 任何可删除目录都不能是主目录、常用系统目录或它们的上级。
# 同时比较物理路径和磁盘身份，防止通过 ../、父目录符号链接、大小写
# 不同的写法或 /System/Volumes/Data 别名绕过保护。
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

# 必须在删除前确认 APP_HOME 归属。新安装写入专用标记，旧的无标记目录
# 保守拒绝；不能仅凭目录名字猜测，更不能让符号链接替代标记文件。
validate_app_home() {
  protected_path "$APP_HOME" && die app_home_unsafe '拒绝删除主目录、系统目录或其上级；请检查 --app-home。'
  if [ -e "$APP_HOME" ]; then
    [ -d "$APP_HOME" ] || die app_home_invalid '程序目录不是目录，请检查 --app-home。'
    # 缺标记时给出出路（加标记之前的旧版安装都会遇到）。
    [ -f "$APP_HOME/.xhs-wiki-install" ] && [ ! -L "$APP_HOME/.xhs-wiki-install" ] ||
      die app_home_marker_missing "程序目录缺少 .xhs-wiki-install 标记，无法确认归属，已拒绝卸载。如果确认 $(quote_arg "$APP_HOME") 就是 xhs-wiki 的程序目录：可以在项目目录重新运行一次 bash install.sh --from-local . --app-home $(quote_arg "$APP_HOME")（会补写标记）后再卸载；也可以先停掉后台服务，再手动删除：rm -rf -- $(quote_arg "$APP_HOME")"
  fi
}

# macOS 自带 plutil 可以读 JSON，不依赖会触发开发工具安装的系统 Python。
# 读取失败时只报告位置未知；purge 不能据此猜一个目录去删除。
read_kb_root() {
  local value
  if [ -f "$APP_HOME/settings.json" ] &&
      value="$(/usr/bin/plutil -extract kb_root raw -o - "$APP_HOME/settings.json" 2>/dev/null)" &&
      [ -n "$value" ]; then
    expand_home "$value"
  else
    return 1
  fi
}

# purge 的授权独立于普通卸载确认：两个显式参数、已知路径和真实标记
# 必须同时存在。只删白名单条目，已有笔记库里的其他文件始终保留。
validate_purge() {
  [ "$ASSUME_YES" = "yes" ] || die purge_confirmation_required '清理知识库必须同时显式提供 --yes 和 --purge-kb；仅 --purge-kb 不会删除。'
  [ "$KB_ROOT_UNKNOWN" = "no" ] || die kb_root_unknown '设置中的知识库位置无法读取，已拒绝清理；请先核对 settings.json。'
  protected_path "$KB_ROOT" && die kb_root_unsafe '知识库是主目录、系统目录或其上级，已拒绝清理。'
  same_or_inside "$KB_ROOT" "$APP_HOME" && die kb_root_unsafe '知识库包含程序目录，已拒绝清理。'
  [ -d "$KB_ROOT/.xhs-wiki" ] && [ ! -L "$KB_ROOT/.xhs-wiki" ] ||
    die kb_marker_missing '知识库缺少真实的 .xhs-wiki/ 标记目录，已拒绝清理。'
}

# lsof 返回 1 表示没有监听者，其余错误不能当作端口空闲。
listener_pids() {
  local result status
  if result="$(lsof -nP -iTCP:18765 -sTCP:LISTEN -t 2>/dev/null)"; then
    printf '%s\n' "$result"
  else
    status=$?
    [ "$status" -eq 1 ] || die port_inspection_failed '无法检查 18765 端口；请确认 lsof 可用后重试。'
  fi
}

# PID 是系统进程编号。只允许停止本 APP_HOME 的 venv 启动的 serve，
# 其他程序占用相同端口时直接退出，绝不凭端口号杀进程。
verify_listener() {
  local pid="$1" command_line command_home
  case "$pid" in ''|*[!0-9]*) die service_pid_invalid '端口检查返回了无效的进程编号。' ;; esac
  command_line="$(ps -p "$pid" -o command= 2>/dev/null)" ||
    die service_process_unverified '无法核对监听进程；请确认服务状态后重新卸载。'
  case "$command_line" in
    */venv/bin/*) command_home="${command_line%%/venv/bin/*}" ;;
    *) die port_in_use '18765 端口由其他程序或其他安装目录占用；请在活动监视器中确认并退出该程序后重试。' ;;
  esac
  # macOS 的 /var 与 /private/var 可以指向同一目录，比较物理路径以免
  # 把此前使用另一种合法拼写启动的本工具服务误判为别的安装。
  command_home="$(canonical_path "$command_home")" ||
    die service_process_unverified '监听进程的程序目录无法核对，已停止卸载。'
  [ "$command_home" = "$APP_HOME" ] ||
    die port_in_use '18765 端口由其他安装目录占用；请退出该程序后重试。'
  [[ "$command_line" =~ (xhs-wiki|xhs_wiki\.cli)[[:space:]]+serve([[:space:]]|$) ]] ||
    die port_in_use '18765 端口不是本目录的 xhs-wiki serve；请在活动监视器中确认并退出占用程序后重试。'
}

# LaunchAgent 是 macOS 的用户后台服务。只处理配置明确属于本安装目录
# 的标签；另一个 --app-home 的配置不得因为同名标签被卸载。
plist_belongs_to_app() {
  local owner executable
  [ -f "$PLIST_PATH" ] && [ ! -L "$PLIST_PATH" ] || return 1
  owner="$(/usr/bin/plutil -extract EnvironmentVariables.XHS_WIKI_HOME raw -o - "$PLIST_PATH" 2>/dev/null)" || owner=""
  if [ -n "$owner" ]; then
    owner="$(canonical_path "$owner")" || return 1
    [ "$owner" = "$APP_HOME" ]; return
  fi
  executable="$(/usr/bin/plutil -extract ProgramArguments.0 raw -o - "$PLIST_PATH" 2>/dev/null)" || return 1
  case "$executable" in
    */venv/bin/*) owner="$(canonical_path "${executable%%/venv/bin/*}")" || return 1 ;;
    *) return 1 ;;
  esac
  [ "$owner" = "$APP_HOME" ]
}

# 先验证所有监听者，再停已确认的 LaunchAgent；仍存在的本目录 nohup
# 服务使用 TERM 正常退出。端口未释放就停止卸载，避免服务重建已删目录。
stop_service() {
  local pids pid attempt kill_bin
  if [ "$DRY_RUN" = "yes" ]; then
    echo '    将用 lsof 和 ps 核对 18765 端口，只停止本程序目录的 xhs-wiki serve'
    echo "    归属匹配时运行：launchctl bootout gui/$(id -u) \"$PLIST_PATH\"；nohup 服务用 kill 停止"
    echo "    将删除归属匹配的 LaunchAgent：$PLIST_PATH"
    return
  fi
  command -v lsof >/dev/null 2>&1 || die lsof_missing '找不到 lsof，无法安全停止服务。'
  pids="$(listener_pids)"
  for pid in $pids; do verify_listener "$pid"; done
  if launchctl print "gui/$(id -u)/$LAUNCH_AGENT_LABEL" >/dev/null 2>&1; then
    # 共享标签已被另一个安装使用时，日志和 Skill 也可能属于它，整个
    # 卸载必须先停下，不能只保留 plist 却删除另一个服务依赖的文件。
    plist_belongs_to_app || die launch_agent_home_mismatch '同名后台服务的安装目录不匹配；请先核对原安装目录，未删除任何文件。'
    run launchctl bootout "gui/$(id -u)" "$PLIST_PATH" ||
      die service_stop_failed '后台服务停止失败，程序目录已保留；请查看 launchctl 上方错误。'
  fi
  pids="$(listener_pids)"
  for pid in $pids; do
    verify_listener "$pid"
    # 用外部命令路径，使测试能通过 PATH 注入假 kill；不调用 Bash 内建 kill。
    kill_bin="$(type -P kill)" || die kill_missing '找不到 kill，无法安全停止 nohup 服务。'
    run "$kill_bin" -TERM "$pid" || die service_stop_failed '服务未能退出，程序目录已保留。'
  done
  for ((attempt=0; attempt<10; attempt++)); do
    pids="$(listener_pids)"
    [ -n "$pids" ] || break
    for pid in $pids; do verify_listener "$pid"; done
    sleep 1
  done
  [ -z "$pids" ] || die service_stop_timeout '等待服务退出超时，程序目录已保留；请确认后台服务已退出后重试。'
  if plist_belongs_to_app; then run rm -f -- "$PLIST_PATH"; fi
}

# 本工具写进日志目录的文件（守护进程日志、LaunchAgent 的标准错误、
# nohup 临时服务的日志）。卸载只删这几个文件，目录清空后
# 才删除目录本身。以前对整个日志目录 rm -rf，XHS_WIKI_LOG_DIR 指到
# ~/Library/Logs 这种公共目录时，会连其他程序的日志一起删掉。
LOG_FILES="daemon.log daemon.err.log serve.log"

# 日志目录里有没有本工具的文件（用于确认清单）。
log_files_present() {
  local entry
  for entry in $LOG_FILES; do
    if [ -e "$LOG_DIR/$entry" ] || [ -L "$LOG_DIR/$entry" ]; then return 0; fi
  done
  return 1
}

remove_log_files() {
  local entry
  if [ "$DRY_RUN" = "yes" ]; then
    echo "    将删除 $LOG_DIR 里本工具的日志文件（${LOG_FILES}）；目录清空后才删除目录本身"
    return
  fi
  for entry in $LOG_FILES; do
    if [ -e "$LOG_DIR/$entry" ] || [ -L "$LOG_DIR/$entry" ]; then run rm -f -- "$LOG_DIR/$entry"; fi
  done
  if [ -d "$LOG_DIR" ] && [ -z "$(ls -A -- "$LOG_DIR")" ]; then run rmdir -- "$LOG_DIR"; fi
  if [ -d "$LOG_DIR" ]; then echo "    日志目录里还有其他文件，目录已保留：$LOG_DIR"; fi
}

# 查询 Skill 可能装在三个技能目录里（控制台里勾选过哪个，本工具就往
# 哪个写过 xhs-wiki/SKILL.md）：Claude Code 的、WorkBuddy 的，以及
# Codex、Cursor、Gemini CLI、GitHub Copilot 共用的 ~/.agents/skills。
# 卸载时三个都查一遍。每个目录同样只删本工具写的 SKILL.md（和写入中断
# 时可能留下的 .skill-* 临时文件），目录清空后才删除目录本身。
# XHS_WIKI_*SKILLS_ROOT 指错时，<它>/xhs-wiki 可能恰好是用户自己的文件夹。
remove_skill_files() {
  local leftover dir
  for dir in "${SKILL_DIRS[@]}"; do
    if [ "$DRY_RUN" = "yes" ]; then
      echo "    将删除 $dir/SKILL.md（如果有）；目录清空后才删除目录本身"
      continue
    fi
    if [ -e "$dir/SKILL.md" ] || [ -L "$dir/SKILL.md" ]; then
      run rm -f -- "$dir/SKILL.md"
    fi
    for leftover in "$dir"/.skill-*; do
      if [ -e "$leftover" ] || [ -L "$leftover" ]; then run rm -f -- "$leftover"; fi
    done
    if [ -d "$dir" ] && [ -z "$(ls -A -- "$dir")" ]; then run rmdir -- "$dir"; fi
    if [ -d "$dir" ]; then echo "    Skill 目录里还有其他文件，目录已保留：$dir"; fi
  done
}

# 命令软链接是否属于本次安装（只认指向本程序目录 venv 的链接）。
bin_link_belongs_to_app() {
  [ -L "$BIN_LINK" ] && [ "$(readlink "$BIN_LINK")" = "$APP_HOME/venv/bin/xhs-wiki" ]
}

# main 完整定义后才会执行；curl 下载中断时只定义函数，不会提前删除。
main() {
  set -euo pipefail
  # 脚本内部的 cd 一律不经过用户导出的 CDPATH。
  unset CDPATH
  local supplied_app kb_value entry log_check_path skill_check_path skill_dir plan="" app_explicit="no"
  local skill_check_paths=()
  ASSUME_YES="no"
  DRY_RUN="no"
  PURGE_KB="no"
  APP_HOME="${XHS_WIKI_HOME:-$HOME/Library/Application Support/xhs-wiki}"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --yes) ASSUME_YES="yes" ;;
      --dry-run) DRY_RUN="yes" ;;
      --purge-kb) PURGE_KB="yes" ;;
      --app-home)
        [ "$#" -ge 2 ] && [ -n "$2" ] || die argument_invalid '--app-home 需要非空目录参数。'
        APP_HOME="$2"; app_explicit="yes"; shift ;;
      -h|--help)
        echo '用法：bash uninstall.sh [--yes] [--dry-run] [--app-home <目录>] [--purge-kb]'
        return ;;
      *) die argument_invalid '未知参数；运行 bash uninstall.sh --help 查看用法。' ;;
    esac
    shift
  done
  [ "$(id -u)" -ne 0 ] || die root_not_allowed '不要用 sudo 或 root 运行；请以普通用户执行 bash uninstall.sh。'
  supplied_app="$(expand_home "$APP_HOME")" || die app_home_invalid '程序目录路径无效。'
  [ ! -L "$supplied_app" ] || die app_home_unsafe '程序目录不能是符号链接，请提供实际安装目录。'
  APP_HOME="$(canonical_path "$supplied_app")" || die app_home_invalid '无法解析程序目录，请检查 --app-home。'
  PLIST_PATH="$HOME/Library/LaunchAgents/io.github.xhs-wiki.service.plist"
  LAUNCH_AGENT_LABEL="io.github.xhs-wiki.service"
  LOG_DIR="$(expand_home "${XHS_WIKI_LOG_DIR:-$HOME/Library/Logs/xhs-wiki}")" || die log_dir_invalid '日志目录路径无效。'
  # 三个技能目录（见 remove_skill_files 上方的说明），顺序：Claude Code、
  # WorkBuddy、共用目录。每个都能用环境变量改写，和 install.sh 一致。
  SKILL_DIRS=()
  skill_dir="$(expand_home "${XHS_WIKI_SKILLS_ROOT:-$HOME/.claude/skills}/xhs-wiki")" || die skill_dir_invalid 'Skill 目录路径无效。'
  SKILL_DIRS+=("$skill_dir")
  skill_dir="$(expand_home "${XHS_WIKI_WORKBUDDY_SKILLS_ROOT:-$HOME/.workbuddy/skills}/xhs-wiki")" || die skill_dir_invalid 'WorkBuddy 的 Skill 目录路径无效。'
  SKILL_DIRS+=("$skill_dir")
  skill_dir="$(expand_home "${XHS_WIKI_AGENTS_SKILLS_ROOT:-$HOME/.agents/skills}/xhs-wiki")" || die skill_dir_invalid '共用技能目录（~/.agents/skills）的路径无效。'
  SKILL_DIRS+=("$skill_dir")
  # 删除参数保留原路径，绝不能把末级符号链接解析成目标后再删目标。
  # 去掉末尾斜杠才能可靠判断目录链接；遇到链接直接停下，交给用户核对。
  while [ "$LOG_DIR" != / ] && [ "${LOG_DIR%/}" != "$LOG_DIR" ]; do LOG_DIR="${LOG_DIR%/}"; done
  [ ! -L "$LOG_DIR" ] || die log_dir_unsafe '日志目录是符号链接，已拒绝卸载。'
  for skill_dir in "${SKILL_DIRS[@]}"; do
    [ ! -L "$skill_dir" ] || die skill_dir_unsafe "Skill 目录是符号链接，已拒绝卸载：$(quote_arg "$skill_dir")"
  done
  log_check_path="$(canonical_path "$LOG_DIR")" || die log_dir_invalid '日志目录路径无效。'
  for skill_dir in "${SKILL_DIRS[@]}"; do
    skill_check_path="$(canonical_path "$skill_dir")" || die skill_dir_invalid 'Skill 目录路径无效。'
    skill_check_paths+=("$skill_check_path")
  done
  BIN_LINK="$HOME/.local/bin/xhs-wiki"
  # 显式写了 --app-home 却找不到这个目录，多半是拼错了。
  # 以前会跳过程序目录，接着照删当前环境的日志和 Skill（也就是正式安装
  # 在用的那份）；现在直接停下，什么都不删。
  if [ "$app_explicit" = "yes" ] && [ ! -d "$APP_HOME" ]; then
    if [ "$DRY_RUN" = "yes" ]; then
      echo "注意：指定的程序目录不存在：${APP_HOME}；实际运行时会报错退出，不删除任何文件。"
    else
      die app_home_missing "指定的程序目录不存在：$(quote_arg "$APP_HOME")。请检查 --app-home 的拼写；为避免误删，没有删除任何文件。"
    fi
  fi
  validate_app_home
  protected_path "$log_check_path" && die log_dir_unsafe '日志目录指向受保护目录，已拒绝卸载。'
  for skill_check_path in "${skill_check_paths[@]}"; do
    protected_path "$skill_check_path" && die skill_dir_unsafe 'Skill 目录指向受保护目录，已拒绝卸载。'
  done
  KB_ROOT_UNKNOWN="no"
  if kb_value="$(read_kb_root)"; then
    KB_ROOT="$(canonical_path "$kb_value")" || die kb_root_invalid '设置中的知识库路径无效。'
  else
    KB_ROOT_UNKNOWN="yes"
    KB_ROOT="$HOME/XHS-Wiki"
  fi
  if [ "$PURGE_KB" = "yes" ]; then validate_purge; fi
  # 避免先删程序目录时顺带删除嵌在其中的知识库；这里不能靠 purge 补救。
  # 按磁盘身份比较，大小写不同的写法也认得出来。
  same_or_inside "$APP_HOME" "$KB_ROOT" &&
    die kb_root_in_app_home '知识库位于程序目录内；请先迁出知识库，再卸载。'
  if same_or_inside "$log_check_path" "$KB_ROOT"; then
    die kb_root_in_install_files '知识库位于日志或 Skill 目录内；请先迁出知识库，再卸载。'
  fi
  for skill_check_path in "${skill_check_paths[@]}"; do
    if same_or_inside "$skill_check_path" "$KB_ROOT"; then
      die kb_root_in_install_files '知识库位于日志或 Skill 目录内；请先迁出知识库，再卸载。'
    fi
  done
  if [ "$DRY_RUN" = "yes" ]; then
    echo '=== dry-run 模式：只打印要做的事，不删除任何东西 ==='
    echo "APP_HOME=$APP_HOME"
  fi
  # 确认前列出将要删除的全部内容（程序目录、LaunchAgent、
  # 日志、Skill、命令链接，以及 purge 时知识库里的生成内容）；程序目录
  # 不存在时也要确认，因为其余几项照样会被删除。
  if [ -d "$APP_HOME" ]; then
    plan="${plan}    - 程序目录（含登录状态、处理记录和语音模型）：$APP_HOME"$'\n'
  fi
  if plist_belongs_to_app; then plan="${plan}    - LaunchAgent 配置：$PLIST_PATH"$'\n'; fi
  if log_files_present; then plan="${plan}    - 日志文件：$LOG_DIR 里的 ${LOG_FILES}"$'\n'; fi
  for skill_dir in "${SKILL_DIRS[@]}"; do
    if [ -e "$skill_dir/SKILL.md" ]; then plan="${plan}    - 查询 Skill：$skill_dir/SKILL.md"$'\n'; fi
  done
  if bin_link_belongs_to_app; then plan="${plan}    - 命令链接：$BIN_LINK"$'\n'; fi
  if [ "$PURGE_KB" = "yes" ]; then
    plan="${plan}    - 知识库里本工具生成的内容：$KB_ROOT 下的 索引.md、暂缓.md、主题/、来源/、.xhs-wiki/"$'\n'
  fi
  # 所有等待回答都放在任何停止和删除之前，拒绝或无终端时保持原状。
  if [ -n "$plan" ]; then
    echo '将要删除：'
    printf '%s' "$plan"
    if ! confirm "确认删除以上内容吗？（知识库默认保留）"; then
      echo '已取消卸载，没有停止服务或删除文件。'
      return
    fi
  else
    echo '没有找到属于这个程序目录的安装文件；仍会检查并停止它的后台服务。'
  fi

  note '步骤 1/5 停止并删除后台服务（LaunchAgent）'
  stop_service
  note '步骤 2/5 删除程序目录（包含登录状态和语音模型）'
  if [ "$DRY_RUN" = "yes" ]; then
    echo "    将在标记核对通过后删除：$APP_HOME"
  elif [ -d "$APP_HOME" ]; then
    validate_app_home
    run rm -rf -- "$APP_HOME"
  else
    echo '    目录不存在，跳过'
  fi
  note '步骤 3/5 删除日志文件'
  remove_log_files
  note '步骤 4/5 删除查询 Skill 和命令软链接'
  remove_skill_files
  # 仅删除本安装的软链接；同名普通文件或其他安装的链接不属于本次卸载。
  if [ "$DRY_RUN" = "yes" ]; then
    echo "    将删除目标为 $APP_HOME/venv/bin/xhs-wiki 的命令软链接：$BIN_LINK"
  elif bin_link_belongs_to_app; then
    run rm -f -- "$BIN_LINK"
  fi
  note '步骤 5/5 知识库（默认保留）'
  if [ "$KB_ROOT_UNKNOWN" = "yes" ]; then echo '    知识库位置未知，默认在 ~/XHS-Wiki'; fi
  if [ "$PURGE_KB" = "yes" ]; then
    validate_purge
    for entry in 索引.md 主题 来源 暂缓.md .xhs-wiki; do
      run rm -rf -- "$KB_ROOT/$entry"
    done
    if [ "$DRY_RUN" = "yes" ]; then
      echo "    仅当目录为空时删除目录本身：$KB_ROOT"
    else
      # rmdir 只删除空目录；剩有任何用户文件就正常保留，不递归删除目录。
      if [ -z "$(ls -A -- "$KB_ROOT")" ]; then run rmdir -- "$KB_ROOT"; fi
      echo '    本工具生成的知识库内容已清理，其他文件已保留。'
    fi
  else
    echo "    已保留知识库：${KB_ROOT}（需同时提供 --yes 和 --purge-kb 并通过标记检查才清理）"
  fi
  if [ "$DRY_RUN" = "yes" ]; then
    echo '=== dry-run 结束：以上就是要执行的全部步骤，没有删除任何东西 ==='
  else
    echo '卸载完成。'
  fi
}
main "$@"
