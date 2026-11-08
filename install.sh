#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# =========================================================
# Debian 13: Xray VLESS-WS + VLESS-REALITY + Node Monitor
# Installs panel_dual.py from the configured GitHub raw URL.
# Cloudflared is intentionally not installed or configured.
#
# 用法：
#   bash install-dual.sh              正常安装
#   bash install-dual.sh --check      只读检查当前部署状态（不修改系统）
#   bash install-dual.sh --recover    在新 Shell 中恢复中断/失败的安装事务
#   bash install-dual.sh --help       查看帮助
# =========================================================

PANEL_URL="https://raw.githubusercontent.com/universeglitch/vps-monitor-lite/main/panel_dual.py"
XRAY_INSTALL="https://github.com/XTLS/Xray-install/raw/main/install-release.sh"

PANEL_DIR="/opt/node-panel"
PANEL_FILE="$PANEL_DIR/panel.py"
PANEL_CONFIG="/etc/node-panel/panel-config.json"
AUTH_FILE="/etc/node-panel/auth.json"
SECRET_FILE="/etc/node-panel/secret.key"
XRAY_CONFIG="/usr/local/etc/xray/config.json"

XRAY_SERVICE="xray.service"
PANEL_SERVICE="node-panel.service"
CLOUDFLARED_SERVICE="cloudflared.service"
NODE_PANEL_UNIT="/etc/systemd/system/node-panel.service"
XRAY_RESTART_DROPIN="/etc/systemd/system/xray.service.d/90-restart-on-failure.conf"

# 安装互斥锁与事务目录
LOCK_FILE="/run/lock/node-panel-install.lock"
BACKUP_PREFIX="/var/backups/node-panel-install-"
STATE_FILE="state.txt"
MANIFEST_FILE="manifest.tsv"
DIRS_FILE="dirs.txt"
META_FILE="meta.txt"
HASH_FILE="hashes.tsv"
# 逐文件恢复日志：记录每个受管文件的恢复动作与状态，使 --recover 可断点续跑。
JOURNAL_FILE="restore-journal.tsv"
JOURNAL_VERSION="1"

red(){ echo -e "\033[31m$*\033[0m"; }
green(){ echo -e "\033[32m$*\033[0m"; }
yellow(){ echo -e "\033[33m$*\033[0m"; }
die(){ red "错误：$*"; exit 1; }

ask(){
  local prompt="$1" default="${2:-}" value
  if [[ -n "$default" ]]; then
    read -r -p "$prompt [$default]: " value
    echo "${value:-$default}"
  else
    read -r -p "$prompt: " value
    echo "$value"
  fi
}

valid_port(){
  [[ "$1" =~ ^[0-9]+$ ]] &&
    (( 10#$1 >= 1 && 10#$1 <= 65535 ))
}

valid_host(){
  [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] &&
    [[ "$1" != *..* ]]
}

svc_active(){ systemctl is-active --quiet "$1" 2>/dev/null; }
svc_enabled(){ systemctl is-enabled --quiet "$1" 2>/dev/null; }
svc_exists(){ systemctl cat "$1" >/dev/null 2>&1; }

port_in_use(){
  local port="$1"
  ss -H -lnt 2>/dev/null | awk -v p=":$port" '$4 ~ (p "$") {found=1} END {exit !found}'
}

usage(){
  cat <<'EOF'
install-dual.sh —— Xray 双节点 + Node Monitor 安装脚本

用法：
  bash install-dual.sh              正常安装（交互式）
  bash install-dual.sh --check      只读检查当前部署状态，不修改系统
  bash install-dual.sh --recover    在新 Shell 中恢复中断/失败的安装事务
  bash install-dual.sh --help       显示本帮助

说明：
  --check   仅检查系统版本架构、Xray、面板、服务、认证文件和未完成事务，
            不会生成密钥、创建目录、修改权限、重启服务或删除文件。
  --recover 扫描 /var/backups/node-panel-install-* 中未完成或回滚失败的事务，
            按事务清单恢复文件与服务状态，可独立于上一次安装进程运行。
EOF
}

# =========================================================
# 模式解析
# =========================================================
MODE="install"
case "${1:-}" in
  "")
    MODE="install"
    ;;
  --check)
    MODE="check"
    ;;
  --recover)
    MODE="recover"
    ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    red "未知参数：$1"
    usage
    exit 2
    ;;
esac

# =========================================================
# 安全安装事务：覆盖保护、备份、失败回滚
# =========================================================
INSTALL_COMMITTED=0
ROLLBACK_READY=0
BACKUP_DIR=""
TRANSACTION_ID=""
CREATED_AT=""
CURRENT_STAGE=""
# MANIFEST_COMPLETE：备份清单（含安装前指纹）是否已完整写入
MANIFEST_COMPLETE=0
# HASHES_COMPLETE：是否已把所有受管文件的“安装后指纹”写入 hashes.tsv
# 两者语义不同，严禁混用：安装中途中断时 MANIFEST_COMPLETE=1 但 HASHES_COMPLETE=0。
HASHES_COMPLETE=0
PASSWORD_FILE=""
declare -a TRACK_DIRS=()

now_iso(){ date -u +%Y-%m-%dT%H:%M:%SZ; }

# 读取 key=value 形式文件中的单个字段
state_get(){
  local f="$1" k="$2" line=""
  [[ -f "$f" ]] || return 0
  line="$(grep -m1 "^${k}=" "$f" 2>/dev/null || true)"
  printf '%s' "${line#*=}"
}

# 状态文件必须包含的字段
STATE_KEYS=(
  transaction_id created_at updated_at status current_stage
  xray_was_active xray_was_enabled
  node_panel_was_active node_panel_was_enabled
  manifest_complete hashes_complete
)

# 状态文件合法性状态取值
STATE_VALUES=(IN_PROGRESS COMMITTED ROLLBACK_IN_PROGRESS ROLLED_BACK ROLLBACK_FAILED NOT_STARTED)

state_status_ok(){
  local v="$1" s
  for s in "${STATE_VALUES[@]}"; do [[ "$v" == "$s" ]] && return 0; done
  return 1
}

# 持久化记录必须以换行结束；不能让 read 静默丢掉截断的末行。
file_has_final_newline(){
  local f="$1" last_byte
  [[ -f "$f" && -s "$f" ]] || return 1
  last_byte="$(tail -c 1 -- "$f" | od -An -tu1)" || return 1
  [[ "$last_byte" =~ ^[[:space:]]*10[[:space:]]*$ ]]
}

# 安全决策使用的布尔字段必须恰好出现一次，且只能为字面量 0 / 1。
# 不使用 state_get 的“首条匹配”来掩盖重复字段，也不把缺失/空值当成 0。
verify_binary_fields(){
  local f="$1" k count v
  shift
  file_has_final_newline "$f" || return 1
  for k in "$@"; do
    count="$(grep -c "^${k}=" "$f")" || return 1
    [[ "$count" == "1" ]] || return 1
    v="$(state_get "$f" "$k")"
    [[ "$v" == "0" || "$v" == "1" ]] || return 1
  done
  return 0
}

# 把文件/目录刷入磁盘，降低断电丢失状态/指纹/恢复日志的风险。
# 关键持久化操作失败必须被调用方感知，因此这里绝不返回假成功：
#   - 成功持久化返回 0；
#   - 目标不存在视为无需持久化，返回 0；
#   - 所有可用手段（sync -f 目标 / sync 全量）都失败时返回 1，
#     由调用方决定是中止危险操作还是明确降级，绝不静默忽略。
fsync_path(){
  local f="$1"
  [[ -e "$f" ]] || return 0
  if command sync -f "$f" 2>/dev/null; then
    return 0
  fi
  if command sync 2>/dev/null; then
    return 0
  fi
  return 1
}

# 校验状态文件：字段唯一且非空，服务状态及完整性标记只能为 0 / 1。
state_verify_fields(){
  local f="$1" want_status="${2:-}" want_stage="${3:-}" k count
  [[ -f "$f" ]] || return 1
  for k in "${STATE_KEYS[@]}"; do
    count="$(grep -c "^${k}=" "$f")" || return 1
    [[ "$count" == "1" && -n "$(state_get "$f" "$k")" ]] || return 1
  done
  verify_binary_fields "$f" \
    xray_was_active xray_was_enabled \
    node_panel_was_active node_panel_was_enabled \
    manifest_complete hashes_complete || return 1
  state_status_ok "$(state_get "$f" status)" || return 1
  [[ -z "$want_status" || "$(state_get "$f" status)" == "$want_status" ]] || return 1
  [[ -z "$want_stage" || "$(state_get "$f" current_stage)" == "$want_stage" ]] || return 1
  return 0
}

# 原子写入事务状态文件（临时文件 + rename），权限 600。
# 任何一步（字段检查 / 写入 / chmod / rename / 回读）失败都必须返回非 0 并明确报错，
# 绝不静默忽略，以免状态文件与实际部署不一致。
state_write(){
  local status="$1" stage="${2:-$CURRENT_STAGE}"
  CURRENT_STAGE="$stage"

  # 写入前的必需字段检查
  if [[ -z "${BACKUP_DIR:-}" || ! -d "${BACKUP_DIR:-}" ]]; then
    red "错误：事务备份目录不存在，无法写入状态文件（status=$status）。"
    return 1
  fi
  if [[ -z "${TRANSACTION_ID:-}" ]]; then
    red "错误：事务 ID 为空，无法写入状态文件。"
    return 1
  fi
  if [[ -z "${CREATED_AT:-}" ]]; then
    red "错误：事务创建时间为空，无法写入状态文件。"
    return 1
  fi
  if [[ -z "$stage" ]]; then
    red "错误：当前阶段为空，无法写入状态文件。"
    return 1
  fi
  if ! state_status_ok "$status"; then
    red "错误：非法的状态取值：${status:-空}"
    return 1
  fi

  local tmp="$BACKUP_DIR/.state.txt.tmp.$$"
  local target="$BACKUP_DIR/$STATE_FILE"
  if ! {
    printf 'transaction_id=%s\n' "${TRANSACTION_ID:-}"
    printf 'created_at=%s\n' "${CREATED_AT:-}"
    printf 'updated_at=%s\n' "$(now_iso)"
    printf 'status=%s\n' "$status"
    printf 'current_stage=%s\n' "$stage"
    printf 'xray_was_active=%s\n' "${XRAY_WAS_ACTIVE:-0}"
    printf 'xray_was_enabled=%s\n' "${XRAY_WAS_ENABLED:-0}"
    printf 'node_panel_was_active=%s\n' "${NODEPANEL_WAS_ACTIVE:-0}"
    printf 'node_panel_was_enabled=%s\n' "${NODEPANEL_WAS_ENABLED:-0}"
    printf 'manifest_complete=%s\n' "${MANIFEST_COMPLETE:-0}"
    printf 'hashes_complete=%s\n' "${HASHES_COMPLETE:-0}"
  } > "$tmp"; then
    red "错误：无法写入状态临时文件：$tmp"
    rm -f -- "$tmp" 2>/dev/null || true
    return 1
  fi
  if ! fsync_path "$tmp"; then
    red "错误：无法持久化状态临时文件（fsync 失败）：$tmp"
    rm -f -- "$tmp" 2>/dev/null || true
    return 1
  fi
  if ! chmod 600 "$tmp"; then
    red "错误：无法设置状态文件权限（应为 600）：$tmp"
    rm -f -- "$tmp" 2>/dev/null || true
    return 1
  fi
  if ! state_verify_fields "$tmp" "$status" "$stage"; then
    red "错误：状态临时文件字段校验失败：$tmp"
    rm -f -- "$tmp" 2>/dev/null || true
    return 1
  fi
  if ! mv -f -- "$tmp" "$target"; then
    red "错误：无法替换状态文件：$target"
    rm -f -- "$tmp" 2>/dev/null || true
    return 1
  fi
  if ! fsync_path "$target"; then
    red "错误：无法持久化状态文件（fsync 失败）：$target"
    return 1
  fi
  # 持久化目录项，确保 rename 本身也已落盘
  if ! fsync_path "$BACKUP_DIR"; then
    red "错误：无法持久化事务目录（fsync 失败）：$BACKUP_DIR"
    return 1
  fi
  # rename 后回读验证完整字段，任何缺失/不一致都视为写入失败
  if ! state_verify_fields "$target" "$status" "$stage"; then
    red "错误：状态文件回读校验失败：$target"
    return 1
  fi
  return 0
}

# 阶段推进：状态写入失败必须阻止安装继续。
# 进入新阶段前，先把上一阶段结束时的受管文件“安装后指纹”持久化，
# 这样即使在这之后（下一阶段内）被 SIGKILL，已发生的改动都有可验证依据。
set_stage(){
  if [[ "${MANIFEST_COMPLETE:-0}" == "1" && -n "${BACKUP_DIR:-}" ]]; then
    # 关键指纹记录失败必须中止：绝不在关键指纹无法可靠落盘时继续修改受管文件。
    if ! record_manifest_hashes "$BACKUP_DIR/$MANIFEST_FILE"; then
      die "无法持久化受管文件的安装后指纹，安装中止（备份目录：$BACKUP_DIR）。"
    fi
  fi
  if ! state_write "IN_PROGRESS" "$1"; then
    die "无法更新安装状态（阶段：$1），安装中止。"
  fi
}

# 文件指纹：用于判断目标文件是否与事务预期一致（类型 + 内容 + 权限 + 属主 + 修改时间）。
#  - MISSING                                            ：路径不存在
#  - LINK:<目标>|perm=<p>|owner=<u>:<g>|mtime=<m>        ：符号链接（含链接目标与自身属性）
#  - DIR:<目录内容摘要>|perm=<p>|owner=<u>:<g>|mtime=<m> ：目录（含类型与内容状态）
#  - FILE:<sha256>|perm=<p>|owner=<u>:<g>|mtime=<m>      ：普通文件
#  - SPECIAL:<类型>|perm=<p>|owner=<u>:<g>|mtime=<m>     ：FIFO/socket/设备等特殊类型
# mtime 为纳秒级修改时间；对符号链接只校验链接自身（不跟随链接）。
# 历史备份中的指纹不含 mtime 字段，比较由 fp_equal 兼容。
file_fingerprint(){
  local f="$1" perm owner h mt
  if [[ -L "$f" ]]; then
    perm="$(stat -c '%a' -- "$f" 2>/dev/null || echo '?')"
    owner="$(stat -c '%U:%G' -- "$f" 2>/dev/null || echo '?')"
    mt="$(stat -c '%.9Y' -- "$f" 2>/dev/null || echo '?')"
    printf 'LINK:%s|perm=%s|owner=%s|mtime=%s' \
      "$(readlink -- "$f" 2>/dev/null || echo '?')" "$perm" "$owner" "$mt"
  elif [[ -d "$f" ]]; then
    perm="$(stat -c '%a' -- "$f" 2>/dev/null || echo '?')"
    owner="$(stat -c '%U:%G' -- "$f" 2>/dev/null || echo '?')"
    mt="$(stat -c '%.9Y' -- "$f" 2>/dev/null || echo '?')"
    # 目录内容状态：直属条目（名称+类型）的稳定摘要
    h="$(find "$f" -mindepth 1 -maxdepth 1 -printf '%f\t%y\n' 2>/dev/null |
           LC_ALL=C sort | sha256sum 2>/dev/null | awk '{print $1}')" || h=""
    [[ -n "$h" ]] || h="FAIL"
    printf 'DIR:%s|perm=%s|owner=%s|mtime=%s' "$h" "$perm" "$owner" "$mt"
  elif [[ -f "$f" ]]; then
    perm="$(stat -c '%a' -- "$f" 2>/dev/null || echo '?')"
    owner="$(stat -c '%U:%G' -- "$f" 2>/dev/null || echo '?')"
    mt="$(stat -c '%.9Y' -- "$f" 2>/dev/null || echo '?')"
    h="$(sha256sum -- "$f" 2>/dev/null | awk '{print $1}')" || h=""
    [[ -n "$h" ]] || h="FAIL"
    printf 'FILE:%s|perm=%s|owner=%s|mtime=%s' "$h" "$perm" "$owner" "$mt"
  elif [[ -e "$f" ]]; then
    # 存在但不是普通文件/目录/符号链接（FIFO / socket / 设备等特殊类型）：
    # 明确标注其类型，绝不误判为 MISSING（缺失）。
    perm="$(stat -c '%a' -- "$f" 2>/dev/null || echo '?')"
    owner="$(stat -c '%U:%G' -- "$f" 2>/dev/null || echo '?')"
    mt="$(stat -c '%.9Y' -- "$f" 2>/dev/null || echo '?')"
    printf 'SPECIAL:%s|perm=%s|owner=%s|mtime=%s' \
      "$(stat -c '%F' -- "$f" 2>/dev/null || echo 'unknown')" "$perm" "$owner" "$mt"
  else
    printf 'MISSING'
  fi
}

# 计算一行中的制表符数量（用于校验清单/日志的字段数量）
tab_count(){ local s="${1//$'\t'/}"; printf '%d' "$(( ${#1} - ${#s} ))"; }

# 校验指纹字符串格式是否合法（用于清单/指纹记录/恢复日志的完整性检查）
fingerprint_ok(){
  local fp="$1"
  [[ "$fp" == "MISSING" ]] && return 0
  case "$fp" in
    FILE:*|LINK:*|DIR:*|SPECIAL:*) ;;
    *) return 1 ;;
  esac
  [[ "$fp" == *'|perm='* && "$fp" == *'|owner='* ]]
}

# 指纹相等比较（兼容历史备份）：
#   双方都带 mtime 字段时严格比较（含 mtime）；
#   任一方为旧格式（不含 mtime）时忽略 mtime，仅比较其余属性，
#   保证本次改动之前产生的事务/备份仍可被恢复。
fp_equal(){
  local a="$1" b="$2"
  [[ "$a" == "$b" ]] && return 0
  if [[ "$a" == *'|mtime='* && "$b" == *'|mtime='* ]]; then
    return 1
  fi
  [[ "${a%%|mtime=*}" == "${b%%|mtime=*}" ]]
}

# 记录一个受事务管理的文件：src 为真实路径，rel 为备份目录内相对路径。
# 清单第四列为备份时刻的“安装前指纹”，恢复时用于校验目标文件归属。
track_file(){
  local src="$1"
  local rel="$2"
  local dst="$BACKUP_DIR/$rel"
  local existed=0 pre="MISSING"
  mkdir -p "$(dirname "$dst")"
  if [[ -e "$src" || -L "$src" ]]; then
    cp -a -- "$src" "$dst"
    existed=1
    pre="$(file_fingerprint "$src")"
  else
    : > "$dst.absent"
    pre="MISSING"
  fi
  printf '%s\t%s\t%s\t%s\n' "$src" "$rel" "$existed" "$pre" >> "$BACKUP_DIR/$MANIFEST_FILE"
}

# 记录某文件当前的“安装后指纹”，持久化追加写入 hashes.tsv。
# 每行：<路径>\t<指纹>\tpost，同路径以最后一条为准。
# 追加后立即 fsync，确保安装中途 SIGKILL / 断电时该记录仍可读。
# 追加或持久化失败都必须返回非 0，由调用方决定是否中止，
# 绝不在关键指纹无法可靠记录时继续修改受管文件。
record_hash(){
  local src="$1"
  [[ -n "${BACKUP_DIR:-}" && -d "${BACKUP_DIR:-}" ]] || return 0
  local j="$BACKUP_DIR/$HASH_FILE"
  if ! printf '%s\t%s\tpost\n' "$src" "$(file_fingerprint "$src")" >> "$j"; then
    red "错误：无法写入安装后指纹记录：$j"
    return 1
  fi
  if ! fsync_path "$j"; then
    red "错误：无法持久化安装后指纹记录（fsync 失败）：$j"
    return 1
  fi
  return 0
}

# 为清单中所有受管文件记录当前“安装后指纹”。
# 在安装的每个阶段边界以及受管文件被外部安装器改写之后调用，
# 以尽量缩小子进程被强杀时“已改动但无记录”的窗口。
# 任一文件记录失败即返回非 0（注意：安装阶段“修改文件”与“记录指纹”之间
# 仍存在无法消除的中断窗口；无安装后指纹的文件恢复时会保守拒绝，绝不猜测归属）。
record_manifest_hashes(){
  local manifest="$1"
  [[ -f "$manifest" ]] || return 0
  local s r e pre
  while IFS=$'\t' read -r s r e pre; do
    [[ -n "${s:-}" ]] || continue
    record_hash "$s" || return 1
  done < "$manifest"
  return 0
}

# 读取 hashes.tsv 中某文件的最新指纹（无则输出空）
hash_of(){
  local dir="$1" src="$2"
  awk -F'\t' -v s="$src" '$1==s{v=$2} END{if(v!="")print v}' "$dir/$HASH_FILE" 2>/dev/null || true
}

# 判断某路径是否由该事务管理（出现在清单中）
manifest_has(){
  local dir="$1" path="$2"
  [[ -f "$dir/$MANIFEST_FILE" ]] || return 1
  awk -F'\t' -v p="$path" '$1==p{found=1} END{exit !found}' "$dir/$MANIFEST_FILE"
}

# =========================================================
# 逐文件恢复日志（restore-journal.tsv）——断点续跑的核心
# =========================================================
# 每行 6 列（制表符分隔）：
#   <版本>  <源路径>  <动作>  <状态>  <安装前指纹>  <安装后指纹>
# 动作：RESTORE（安装前存在，需还原）/ REMOVE（安装前不存在，需删除）
# 状态：PENDING / INTENT / RESTORED / REMOVED
#   PENDING  尚未开始
#   INTENT   已持久化恢复意图，尚未确认完成
#   RESTORED 已成功恢复到安装前状态
#   REMOVED  原本不存在的文件已安全移除
# 更新方式：整表原子重写（临时文件 + fsync + rename + 回读校验 + 目录持久化），
# 任意时刻被 SIGKILL/断电，日志要么是旧版本、要么是新版本，绝不半写。

# 读取某源路径的日志记录，输出 "状态\t动作\t安装前指纹\t安装后指纹"（无则空）
journal_record(){
  local dir="$1" src="$2"
  local f="$dir/$JOURNAL_FILE"
  [[ -f "$f" ]] || return 0
  awk -F'\t' -v s="$src" '
    $2==s { st=$4; ac=$3; pr=$5; po=$6 }
    END { if (st!="") printf "%s\t%s\t%s\t%s", st, ac, pr, po }
  ' "$f" 2>/dev/null || true
}

# 读取某源路径的日志状态（无记录则输出空）
journal_state(){
  local r; r="$(journal_record "$1" "$2")"
  printf '%s' "${r%%$'\t'*}"
}

# 原子写入/更新某源路径的恢复日志记录。任何一步失败都返回非 0。
journal_set(){
  local dir="$1" src="$2" action="$3" state="$4" pre="$5" post="${6:-}"
  if [[ -z "$src" || -z "$dir" ]]; then
    red "恢复日志写入失败：参数不足（src 为空）"
    return 1
  fi
  case "$state" in
    PENDING|INTENT|RESTORED|REMOVED) ;;
    *) red "恢复日志写入失败：非法状态取值：${state:-空}"; return 1 ;;
  esac
  case "$action" in
    RESTORE|REMOVE) ;;
    *) red "恢复日志写入失败：非法动作取值：${action:-空}"; return 1 ;;
  esac

  local f="$dir/$JOURNAL_FILE"
  local tmp="$dir/.$JOURNAL_FILE.tmp.$$"
  if ! {
    # 保留其它路径的既有记录，替换本路径记录
    if [[ -f "$f" ]]; then
      awk -F'\t' -v s="$src" 'NF && $2!=s' "$f"
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$JOURNAL_VERSION" "$src" "$action" "$state" "$pre" "${post:-}"
  } > "$tmp"; then
    red "错误：无法写入恢复日志临时文件：$tmp"
    rm -f -- "$tmp" 2>/dev/null || true
    return 1
  fi
  if ! fsync_path "$tmp"; then
    red "错误：无法持久化恢复日志临时文件（fsync 失败）：$tmp"
    rm -f -- "$tmp" 2>/dev/null || true
    return 1
  fi
  if ! chmod 600 "$tmp"; then
    red "错误：无法设置恢复日志权限（应为 600）：$tmp"
    rm -f -- "$tmp" 2>/dev/null || true
    return 1
  fi
  if ! mv -f -- "$tmp" "$f"; then
    red "错误：无法替换恢复日志：$f"
    rm -f -- "$tmp" 2>/dev/null || true
    return 1
  fi
  if ! fsync_path "$f"; then
    red "错误：无法持久化恢复日志（fsync 失败）：$f"
    return 1
  fi
  if ! fsync_path "$dir"; then
    red "错误：无法持久化事务目录（fsync 失败）：$dir"
    return 1
  fi
  # 回读校验：确认状态与动作已按预期落盘
  local got; got="$(journal_state "$dir" "$src")"
  if [[ "$got" != "$state" ]]; then
    red "错误：恢复日志回读校验失败（期望 $state，实际 ${got:-空}）：$f"
    return 1
  fi
  return 0
}

# 校验逐文件恢复日志（restore-journal.tsv）完整性与自洽性。任何一项不满足都
# 必须拒绝恢复，绝不使用损坏、截断或字段不完整的日志继续恢复，也绝不猜测文件
# 状态。复用现有清单（manifest.tsv）、指纹（hashes.tsv）与恢复日志函数。
# 返回 0 表示日志可用于恢复；否则打印全部问题并返回 1。
# 日志不存在视为“尚未开始恢复”（全部按 PENDING 处理），属于合法情况。
verify_restore_journal(){
  local dir="$1"
  local jf="$dir/$JOURNAL_FILE"
  local manifest="$dir/$MANIFEST_FILE"
  local -a issues=()
  [[ -f "$jf" ]] || return 0
  if [[ ! -f "$manifest" ]]; then
    red "恢复日志校验失败：缺少事务清单，无法核对日志：$manifest"
    return 1
  fi

  # journal_set 始终写入至少一条完整记录，并以换行结束。
  # 日志不存在是合法初始状态；已存在但为空或末行无换行则视为损坏/截断。
  local last_byte
  if [[ ! -s "$jf" ]]; then
    red "恢复日志校验失败：日志为空：$jf"
    return 1
  fi
  if ! last_byte="$(tail -c 1 -- "$jf" | od -An -tu1)"; then
    red "恢复日志校验失败：无法读取日志末尾：$jf"
    return 1
  fi
  if [[ ! "$last_byte" =~ ^[[:space:]]*10[[:space:]]*$ ]]; then
    red "恢复日志校验失败：日志末行不完整，可能已截断：$jf"
    return 1
  fi

  # 建立清单索引：src -> 安装前是否存在 / 安装前指纹
  local -A man_existed=() man_pre=()
  local ms me mp
  while IFS=$'\t' read -r ms _ me mp; do
    [[ -n "${ms:-}" ]] || continue
    man_existed["$ms"]="$me"
    man_pre["$ms"]="${mp:-}"
  done < "$manifest"

  local -A seen=()
  local line ver src action state pre post
  local n=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    n=$((n+1))
    # 每条记录必须有且只有六列（5 个制表符）
    if [[ "$(tab_count "$line")" != "5" ]]; then
      issues+=("恢复日志第 $n 行列数错误（应恰好 6 列）")
      continue
    fi
    IFS=$'\t' read -r ver src action state pre post <<< "$line"
    # 版本字段必须合法
    [[ "$ver" == "$JOURNAL_VERSION" ]] ||
      issues+=("恢复日志第 $n 行版本非法：${ver:-空}")
    # 路径不得为空、不得非法、不得重复
    if [[ -z "$src" || "$src" != /* || "$src" == *..* ]]; then
      issues+=("恢复日志第 $n 行路径非法：${src:-空}")
      continue
    fi
    if [[ -n "${seen[$src]:-}" ]]; then
      issues+=("恢复日志路径重复：$src")
    fi
    seen["$src"]=1
    # 动作只能是现有支持的 RESTORE / REMOVE
    case "$action" in
      RESTORE|REMOVE) ;;
      *) issues+=("恢复日志第 $n 行动作非法：${action:-空}") ;;
    esac
    # 状态只能取现有支持的状态
    case "$state" in
      PENDING|INTENT|RESTORED|REMOVED) ;;
      *) issues+=("恢复日志第 $n 行状态非法：${state:-空}") ;;
    esac
    # 完成状态必须与动作一致；PENDING / INTENT 对两种动作均合法。
    if [[ "$action" == "RESTORE" && "$state" == "REMOVED" ]] ||
       [[ "$action" == "REMOVE" && "$state" == "RESTORED" ]]; then
      issues+=("恢复日志第 $n 行动作与完成状态不一致：$action/$state")
    fi
    # 与清单一致性：路径须受本事务管理；动作须与“安装前是否存在”一致；前指纹须一致
    if [[ -z "${man_existed[$src]:-}" ]]; then
      issues+=("恢复日志路径未出现在事务清单中：$src")
    else
      if [[ "$action" == "RESTORE" && "${man_existed[$src]}" != "1" ]]; then
        issues+=("恢复日志动作与清单不一致（清单:安装前不存在，日志:RESTORE）：$src")
      fi
      if [[ "$action" == "REMOVE" && "${man_existed[$src]}" != "0" ]]; then
        issues+=("恢复日志动作与清单不一致（清单:安装前存在，日志:REMOVE）：$src")
      fi
      if ! fp_equal "$pre" "${man_pre[$src]}"; then
        issues+=("恢复日志安装前指纹与清单不一致：$src")
      fi
    fi
    # 后指纹须与 hashes.tsv 中已有记录一致（无记录时必须为空）
    local hpost; hpost="$(hash_of "$dir" "$src")"
    if ! fp_equal "$post" "$hpost"; then
      issues+=("恢复日志安装后指纹与 hashes.tsv 记录不一致：$src")
    fi
  done < "$jf"

  if (( ${#issues[@]} > 0 )); then
    printf '%s\n' "${issues[@]}"
    return 1
  fi
  return 0
}

# 校验备份条目：备份必须位于事务目录内、类型/内容/权限/属主与安装前指纹一致。
# 返回 0 表示备份可用；否则返回 1 并打印具体原因。
# 覆盖：普通文件内容、权限、owner/group、符号链接目标与属性、目录类型与内容摘要。
verify_backup_entry(){
  local dir="$1" src="$2" rel="$3" pre="$4"
  if [[ -z "$rel" || "$rel" == /* || "$rel" == *..* ]]; then
    red "备份校验失败：相对路径非法（禁止路径穿越）：${rel:-空}"
    return 1
  fi
  local bak="$dir/$rel"
  if [[ ! -e "$bak" && ! -L "$bak" ]]; then
    red "备份校验失败：缺少备份文件：$bak"
    return 1
  fi
  local bfp; bfp="$(file_fingerprint "$bak")"
  if ! fp_equal "$bfp" "$pre"; then
    red "备份校验失败：$bak 与安装前指纹不一致（可能被损坏或篡改）。"
    red "  期望：$pre"
    red "  实际：$bfp"
    return 1
  fi
  return 0
}

# 恢复服务启用状态：安装前未启用则必须确保恢复后仍未启用
restore_enabled(){
  local unit="$1" was="$2"
  if [[ "$was" == "1" ]]; then
    systemctl enable "$unit" >/dev/null 2>&1 || return 1
  elif svc_exists "$unit"; then
    systemctl disable "$unit" >/dev/null 2>&1 || return 1
  fi
  return 0
}

# 恢复服务运行状态：安装前未运行则必须确保恢复后仍未运行
restore_active(){
  local unit="$1" was="$2"
  if [[ "$was" == "1" ]]; then
    systemctl start "$unit" >/dev/null 2>&1 || return 1
  elif svc_exists "$unit"; then
    systemctl stop "$unit" >/dev/null 2>&1 || return 1
  fi
  return 0
}

# 判断目标文件是否处于“事务预期状态”，返回：
#   0 = 与已记录的安装后指纹一致，或（无安装后指纹但）与安装前指纹一致 → 可安全恢复
#   1 = 与已记录的安装后指纹不一致 → 疑似被外部修改，禁止覆盖
#   2 = 既无安装后指纹、又与安装前指纹不一致 → 归属未知，保守拒绝（不猜测）
check_recovery_target(){
  local dir="$1" src="$2" pre="$3" post cur
  post="$(hash_of "$dir" "$src")"
  cur="$(file_fingerprint "$src")"
  if [[ -n "$post" ]]; then
    fp_equal "$cur" "$post" && return 0
    return 1
  fi
  fp_equal "$cur" "$pre" && return 0
  return 2
}

# 根据返回值输出可读的失败原因
report_recovery_target_issue(){
  local rc="$1" src="$2"
  if [[ "$rc" == "1" ]]; then
    red "校验失败：$src 与已记录的安装后指纹不一致（可能被外部修改），禁止覆盖。"
  else
    red "校验失败：$src 既无安装后指纹、又与安装前状态不一致（归属未知），保守拒绝恢复。"
  fi
}

# 恢复前校验：目标文件必须与事务预期一致，否则停止恢复（绝不覆盖外部修改，
# 也绝不猜测归属未知的文件）。仅用于 --recover（无法信任进程内状态）。
verify_recovery_targets(){
  local dir="$1"
  local manifest="$dir/$MANIFEST_FILE"
  local failed=0 s r e pre rc
  if [[ ! -f "$manifest" ]]; then
    red "缺少事务清单，无法校验恢复目标：$manifest"
    return 1
  fi
  while IFS=$'\t' read -r s r e pre; do
    [[ -n "${s:-}" ]] || continue
    if [[ "$s" != /* || "$s" == *..* ]]; then
      red "校验失败：清单中的路径非法：$s"; failed=1; continue
    fi
    rc=0
    check_recovery_target "$dir" "$s" "$pre" || rc=$?
    if (( rc != 0 )); then
      report_recovery_target_issue "$rc" "$s"
      failed=1
    fi
  done < "$manifest"
  (( failed == 0 ))
}

# 校验服务最终状态是否与安装前一致（active 与 enabled 都要一致）。
# 服务单元不存在时不能直接判定成功：
#   - 安装前 active 或 enabled → 单元缺失即恢复失败；
#   - 安装前既非 active 也非 enabled → 单元缺失才符合预期。
verify_service_state(){
  local unit="$1" wa="$2" we="$3" ok=1
  if ! svc_exists "$unit"; then
    if [[ "$wa" == "1" || "$we" == "1" ]]; then
      red "校验失败：$unit 服务单元不存在（安装前 active=$wa enabled=$we）"
      return 1
    fi
    return 0
  fi
  if [[ "$we" == "1" ]]; then svc_enabled "$unit" || ok=0; else svc_enabled "$unit" && ok=0; fi
  if [[ "$wa" == "1" ]]; then svc_active "$unit" || ok=0; else svc_active "$unit" && ok=0; fi
  (( ok == 1 ))
}

# 单个文件的安全恢复：备份 → 同文件系统临时路径 → 校验 → 原子替换 → 持久化 → 复核指纹。
# 绝不直接把备份写入正式目标（复制中途被中断会留下半截文件）。
# 符号链接按链接本身处理（cp -a 不跟随链接），保留其目标与属性；
# 目录项因无法保证原子替换而明确拒绝自动恢复（保留备份，提示人工处理）。
restore_one_file(){
  local dir="$1" src="$2" rel="$3" pre="$4"
  local bak="$dir/$rel"

  case "$pre" in
    DIR:*)
      red "恢复失败：$src 是目录项，无法安全原子替换，已保留备份，请人工恢复。"
      return 1
      ;;
    SPECIAL:*)
      red "恢复失败：$src 是特殊文件类型（FIFO/socket/设备），不支持自动恢复，已保留备份，请人工处理。"
      return 1
      ;;
  esac

  local dstdir; dstdir="$(dirname "$src")"
  if ! mkdir -p -- "$dstdir"; then
    red "恢复失败：无法创建目标目录：$dstdir"
    return 1
  fi

  # 临时文件放在目标同一目录（同一文件系统），保证后续 rename 为原子操作。
  # 文件名包含当前事务 ID，使其唯一归属本事务；清理时只删除本事务自己的遗留
  # 临时文件，绝不批量删除其他事务或其他程序创建的同前缀文件。
  local tmp="$dstdir/.node-panel-restore.${TRANSACTION_ID:-txn}.${BASHPID:-$$}.$RANDOM"
  # 仅清理本事务（同一事务 ID）遗留的普通文件（本事务持有 flock，无并发写入者）。
  # -type f 保证不会删除目录，rm -f 删除符号链接本身而非其目标。
  if [[ -n "${TRANSACTION_ID:-}" ]]; then
    find "$dstdir" -maxdepth 1 -type f \
      -name ".node-panel-restore.${TRANSACTION_ID}.*" \
      -exec rm -f -- {} + 2>/dev/null || true
  fi
  rm -f -- "$tmp" 2>/dev/null || true
  if ! cp -a -- "$bak" "$tmp"; then
    red "恢复失败：无法将备份复制到临时路径：$tmp"
    rm -f -- "$tmp" 2>/dev/null || true
    return 1
  fi
  # 校验临时文件的内容/类型/权限/属主与安装前指纹一致
  local tfp; tfp="$(file_fingerprint "$tmp")"
  if ! fp_equal "$tfp" "$pre"; then
    red "恢复失败：临时文件指纹与安装前指纹不一致（$tfp != $pre）"
    rm -f -- "$tmp" 2>/dev/null || true
    return 1
  fi
  # 原子替换（-T 保证目标是文件/符号链接时不会误判为目录移动）
  if ! mv -fT -- "$tmp" "$src" 2>/dev/null; then
    if [[ ! -d "$src" ]] && ! mv -f -- "$tmp" "$src"; then
      red "恢复失败：无法原子替换目标：$src"
      rm -f -- "$tmp" 2>/dev/null || true
      return 1
    fi
  fi
  # 持久化目标所在目录，确保 rename 本身也已落盘
  if ! fsync_path "$dstdir"; then
    red "恢复失败：无法持久化目标目录：$dstdir"
    return 1
  fi
  # 重新计算目标指纹，确认与安装前一致
  local cfp; cfp="$(file_fingerprint "$src")"
  if ! fp_equal "$cfp" "$pre"; then
    red "恢复失败：$src 替换后指纹与安装前不一致（$cfp != $pre）"
    return 1
  fi
  return 0
}

# 按事务清单恢复（供自动回滚与 --recover 共用）。
# 完全依赖磁盘上的清单/状态/指纹文件与逐文件恢复日志 restore-journal.tsv，
# 可在新的 Shell 中独立运行，并支持被 SIGKILL / 断电打断后再次续跑：
#   - 已 RESTORED / REMOVED 的文件会被识别并跳过，绝不因“不再是安装后状态”而被误判为外部修改；
#   - INTENT 的文件按“当前 == 安装前指纹（此前已替换完成）”或“当前 == 安装后指纹（可安全重试）”区分处理；
#   - 与安装前、安装后指纹都不一致，或缺安装后指纹又与安装前不一致的文件，一律保守拒绝，绝不覆盖。
# 兼容调用方传入的第二个参数（历史 verify 标志），恢复前始终逐项校验目标与备份。
restore_transaction(){
  local dir="$1"
  local st="$dir/$STATE_FILE"
  local manifest="$dir/$MANIFEST_FILE"
  local dirs="$dir/$DIRS_FILE"
  local meta="$dir/$META_FILE"
  local failed=0

  # 自动回滚与独立恢复共享同一前置门禁，不能只依赖 recover_mode 的校验。
  # 元数据/状态/清单/指纹全部通过之前，不得操作服务、恢复文件或删除账户。
  local issues
  if ! issues="$(verify_transaction "$dir")"; then
    red "恢复中止：事务元数据或备份校验失败："
    echo "$issues"
    return 1
  fi

  # 恢复前校验逐文件恢复日志：损坏、截断或字段不完整的日志一律拒绝恢复，
  # 绝不猜测文件状态。
  local jissues
  if ! jissues="$(verify_restore_journal "$dir")"; then
    red "恢复中止：恢复日志校验失败，拒绝继续恢复："
    echo "$jissues"
    return 1
  fi

  local xa xe na ne xu_existed xg_existed
  xa="$(state_get "$st" xray_was_active)"
  xe="$(state_get "$st" xray_was_enabled)"
  na="$(state_get "$st" node_panel_was_active)"
  ne="$(state_get "$st" node_panel_was_enabled)"
  xu_existed="$(state_get "$meta" xray_user_existed)"
  xg_existed="$(state_get "$meta" xray_group_existed)"

  # 只停止“由本事务管理”的服务，避免影响不属于本事务的外部服务（幂等，可重复执行）。
  local panel_owned=0 xray_owned=0
  if manifest_has "$dir" "$NODE_PANEL_UNIT"; then panel_owned=1; fi
  if manifest_has "$dir" "/usr/local/bin/xray" ||
     manifest_has "$dir" "/etc/systemd/system/xray.service" ||
     manifest_has "$dir" "/lib/systemd/system/xray.service" ||
     manifest_has "$dir" "/usr/local/lib/systemd/system/xray.service"; then
    xray_owned=1
  fi

  # 单元存在时，停止失败、停止后仍 active 或 disable 失败均立即中止。
  # 单元不存在时，仅安装前明确为 inactive/disabled 才允许跳过 stop/disable。
  if (( panel_owned )); then
    if svc_exists "$PANEL_SERVICE"; then
      if ! systemctl stop "$PANEL_SERVICE" >/dev/null 2>&1; then
        red "恢复中止：无法停止 $PANEL_SERVICE，已中止文件恢复以避免覆盖运行中服务的配置。"
        return 1
      fi
      if svc_active "$PANEL_SERVICE"; then
        red "恢复中止：$PANEL_SERVICE 停止后仍处于 active 状态，已中止文件恢复。"
        return 1
      fi
      if [[ "$ne" != "1" ]]; then
        if ! systemctl disable "$PANEL_SERVICE" >/dev/null 2>&1; then
          red "恢复中止：无法禁用 $PANEL_SERVICE（安装前未启用），已中止文件恢复。"
          return 1
        fi
      fi
    elif [[ "$na" != "0" || "$ne" != "0" ]]; then
      red "恢复中止：$PANEL_SERVICE 服务单元不存在（安装前 active=$na enabled=$ne），拒绝恢复。"
      return 1
    fi
  fi
  if (( xray_owned )); then
    if svc_exists "$XRAY_SERVICE"; then
      if ! systemctl stop "$XRAY_SERVICE" >/dev/null 2>&1; then
        red "恢复中止：无法停止 $XRAY_SERVICE，已中止文件恢复以避免覆盖运行中服务的配置。"
        return 1
      fi
      if svc_active "$XRAY_SERVICE"; then
        red "恢复中止：$XRAY_SERVICE 停止后仍处于 active 状态，已中止文件恢复。"
        return 1
      fi
      if [[ "$xe" != "1" ]]; then
        if ! systemctl disable "$XRAY_SERVICE" >/dev/null 2>&1; then
          red "恢复中止：无法禁用 $XRAY_SERVICE（安装前未启用），已中止文件恢复。"
          return 1
        fi
      fi
    elif [[ "$xa" != "0" || "$xe" != "0" ]]; then
      red "恢复中止：$XRAY_SERVICE 服务单元不存在（安装前 active=$xa enabled=$xe），拒绝恢复。"
      return 1
    fi
  fi

  local -a S=() R=() E=() P=()
  local s r e p
  while IFS=$'\t' read -r s r e p; do
    [[ -n "${s:-}" ]] || continue
    S+=("$s"); R+=("$r"); E+=("$e"); P+=("${p:-}")
  done < "$manifest"

  local i src rel existed pre post jstate cur
  for ((i=${#S[@]}-1; i>=0; i--)); do
    src="${S[$i]}"; rel="${R[$i]}"; existed="${E[$i]}"; pre="${P[$i]}"

    # 防御：拒绝可疑清单路径，避免误删/误写
    if [[ "$src" != /* || "$src" == *..* || "$rel" == /* || "$rel" == *..* ]]; then
      red "恢复失败：清单路径非法（src=$src rel=$rel）"
      failed=1; break
    fi

    post="$(hash_of "$dir" "$src")"          # 已记录的安装后指纹（可能为空）
    jstate="$(journal_state "$dir" "$src")"  # 持久化恢复日志状态（可能为空→视为 PENDING）
    cur="$(file_fingerprint "$src")"

    if [[ "$existed" == "1" ]]; then
      # 安装前存在 → 需还原。先从备份完整性开始校验。
      if ! verify_backup_entry "$dir" "$src" "$rel" "$pre"; then
        red "恢复中止：$src 的备份不可用，已停止后续恢复并保留全部备份。"
        failed=1; break
      fi

      case "$jstate" in
        RESTORED)
          if fp_equal "$cur" "$pre"; then
            continue
          fi
          red "恢复中止：$src 日志标记为已恢复，但当前与安装前指纹不一致，状态异常，拒绝继续。"
          failed=1; break
          ;;
        INTENT)
          if fp_equal "$cur" "$pre"; then
            # 上次原子替换已完成，仅“完成日志”未写入 → 标记 RESTORED 后跳过
            if ! journal_set "$dir" "$src" RESTORE RESTORED "$pre" "$post"; then
              red "恢复中止：无法持久化恢复日志（$src）。"; failed=1; break
            fi
            continue
          fi
          if [[ -n "$post" ]] && fp_equal "$cur" "$post"; then
            : # 尚未完成正式替换，可安全重试
          else
            red "恢复中止：$src 处于 INTENT，但当前既非安装前也非已记录的安装后状态（归属未知），保守拒绝。"
            failed=1; break
          fi
          ;;
        PENDING|"")
          if [[ -n "$post" ]]; then
            if ! fp_equal "$cur" "$post"; then
              red "恢复中止：$src 与已记录的安装后指纹不一致（可能被外部修改），禁止覆盖。"
              failed=1; break
            fi
          else
            if ! fp_equal "$cur" "$pre"; then
              red "恢复中止：$src 无安装后指纹且与安装前状态不一致（归属未知），保守拒绝恢复。"
              failed=1; break
            fi
          fi
          ;;
        *)
          red "恢复中止：$src 的恢复日志状态非法（${jstate:-空}），保守拒绝。"
          failed=1; break
          ;;
      esac

      # 先持久化恢复意图，再执行实际替换
      if ! journal_set "$dir" "$src" RESTORE INTENT "$pre" "$post"; then
        red "恢复中止：无法持久化恢复意图（$src）。"
        failed=1; break
      fi
      if ! restore_one_file "$dir" "$src" "$rel" "$pre"; then
        failed=1; break
      fi
      if ! journal_set "$dir" "$src" RESTORE RESTORED "$pre" "$post"; then
        red "恢复中止：无法持久化恢复完成状态（$src）。"
        failed=1; break
      fi
    else
      # 安装前不存在 → 需安全移除（仅当能证明是本事务创建的）。
      case "$jstate" in
        REMOVED)
          if [[ ! -e "$src" && ! -L "$src" ]]; then
            continue
          fi
          red "恢复中止：$src 日志标记为已移除，但目标仍存在，拒绝继续。"
          failed=1; break
          ;;
        INTENT)
          if [[ ! -e "$src" && ! -L "$src" ]]; then
            # 目标已不存在：仅当存在本事务“安装后指纹”证据
            # 时，才能按幂等标记 REMOVED；否则保守拒绝。
            # post=MISSING 则表示记录时目标即不存在，与当前状态一致。
            if [[ -z "$post" ]]; then
              red "恢复中止：$src 处于 INTENT 且已不存在，但缺少安装后指纹证据，无法判断移除归属，保守拒绝。"
              failed=1; break
            fi
            if ! journal_set "$dir" "$src" REMOVE REMOVED "$pre" "$post"; then
              red "恢复中止：无法持久化恢复日志（$src）。"; failed=1; break
            fi
            continue
          fi
          if [[ -n "$post" ]] && fp_equal "$cur" "$post"; then
            : # 可安全重试移除
          else
            red "恢复中止：$src 处于 INTENT 且仍存在，但无法证明归属本事务，保守拒绝删除。"
            failed=1; break
          fi
          ;;
        PENDING|"")
          if [[ ! -e "$src" && ! -L "$src" ]]; then
            if [[ -n "$post" && "$post" != "MISSING" ]]; then
              # 本事务曾记录其存在的安装后指纹，如今却消失，
              # 且没有持久化 INTENT 证据 → 删除原因归属不明，保守拒绝。
              red "恢复中止：$src 已不存在，但本事务曾记录其安装后指纹且无恢复意图证据，无法判断删除来源，保守拒绝。"
              failed=1; break
            fi
            # 无安装后记录或明确记录为 MISSING：当前已符合安装前不存在状态。
            if ! journal_set "$dir" "$src" REMOVE REMOVED "$pre" "$post"; then
              red "恢复中止：无法持久化恢复日志（$src）。"; failed=1; break
            fi
            continue
          fi
          if [[ -n "$post" ]] && fp_equal "$cur" "$post"; then
            : # 目标与本事务记录的安装后指纹一致，可安全移除
          else
            red "恢复中止：$src 无法证明由本事务创建（缺少安装后指纹或与记录不一致），拒绝删除。"
            failed=1; break
          fi
          ;;
        *)
          red "恢复中止：$src 的恢复日志状态非法（${jstate:-空}），保守拒绝。"
          failed=1; break
          ;;
      esac

      if ! journal_set "$dir" "$src" REMOVE INTENT "$pre" "$post"; then
        red "恢复中止：无法持久化恢复意图（$src）。"
        failed=1; break
      fi
      if ! rm -f -- "$src"; then
        red "恢复失败：无法安全移除 $src"
        failed=1; break
      fi
      if ! fsync_path "$(dirname "$src")"; then
        red "恢复失败：无法持久化父目录（$src）"
        failed=1; break
      fi
      if [[ -e "$src" || -L "$src" ]]; then
        red "恢复失败：$src 仍然存在"
        failed=1; break
      fi
      if ! journal_set "$dir" "$src" REMOVE REMOVED "$pre" "$post"; then
        red "恢复中止：无法持久化移除完成状态（$src）。"
        failed=1; break
      fi
    fi
  done

  if (( failed != 0 )); then
    red "文件恢复阶段失败：相关服务保持停止，备份与恢复日志均已保留。"
    return 1
  fi

  # 删除本次新建的空目录（仅当为空时；幂等）。
  # 不存在可能是尚未创建或已在上次恢复中删除，属于合法续跑状态；
  # 对仍存在的目录，任何 rmdir 失败都必须中止恢复。
  if [[ -f "$dirs" ]]; then
    local d
    while IFS= read -r d || [[ -n "$d" ]]; do
      if [[ "$d" != /* || "$d" == *..* ]]; then
        red "恢复失败：新建目录记录路径非法：$d"
        return 1
      fi
      if [[ ! -e "$d" && ! -L "$d" ]]; then
        continue
      fi
      if ! rmdir -- "$d"; then
        red "恢复失败：无法删除本事务新建目录：$d，相关服务保持停止。"
        return 1
      fi
    done < "$dirs" || {
      red "恢复失败：无法处理新建目录清单：$dirs"
      return 1
    }
  fi

  if ! systemctl daemon-reload >/dev/null 2>&1; then
    red "恢复失败：systemd daemon-reload 失败，相关服务保持停止。"
    return 1
  fi

  # 清理本次创建的 Xray 用户（仅当安装前不存在）
  if [[ "$xu_existed" == "0" ]] && getent passwd xray >/dev/null 2>&1; then
    local xray_pids=""
    if ! xray_pids="$(ps -u xray -o pid= 2>/dev/null)"; then
      red "恢复失败：无法检查 Xray 用户进程，不能安全删除账户，相关服务保持停止。"
      return 1
    fi
    if [[ -n "${xray_pids//[[:space:]]/}" ]]; then
      red "恢复失败：Xray 用户仍有进程，不能删除账户"
      return 1
    elif ! userdel xray; then
      red "恢复失败：无法删除 Xray 用户"
      return 1
    fi
  fi

  # 仅在 Xray 用户已不存在时删除同名用户组
  if [[ "$xg_existed" == "0" ]] &&
     ! getent passwd xray >/dev/null 2>&1 &&
     getent group xray >/dev/null 2>&1; then
    if ! groupdel xray; then
      red "恢复失败：无法删除 Xray 用户组"
      return 1
    fi
  fi

  # 先恢复全部受管服务的 enabled 状态；任何一步失败都不允许启动服务。
  if (( xray_owned )); then
    restore_enabled "$XRAY_SERVICE" "$xe" ||
      { red "恢复失败：无法恢复 $XRAY_SERVICE 的启用状态"; return 1; }
  fi
  if (( panel_owned )); then
    restore_enabled "$PANEL_SERVICE" "$ne" ||
      { red "恢复失败：无法恢复 $PANEL_SERVICE 的启用状态"; return 1; }
  fi

  # 在启动服务前核对 enabled 状态，避免命令返回成功但状态未达到预期。
  if (( xray_owned )); then
    if [[ "$xe" == "1" ]]; then
      if ! svc_enabled "$XRAY_SERVICE"; then
        red "恢复失败：$XRAY_SERVICE 未恢复到安装前的 enabled 状态，相关服务保持停止。"
        return 1
      fi
    elif svc_enabled "$XRAY_SERVICE"; then
      red "恢复失败：$XRAY_SERVICE 仍处于 enabled 状态，相关服务保持停止。"
      return 1
    fi
  fi
  if (( panel_owned )); then
    if [[ "$ne" == "1" ]]; then
      if ! svc_enabled "$PANEL_SERVICE"; then
        red "恢复失败：$PANEL_SERVICE 未恢复到安装前的 enabled 状态，相关服务保持停止。"
        return 1
      fi
    elif svc_enabled "$PANEL_SERVICE"; then
      red "恢复失败：$PANEL_SERVICE 仍处于 enabled 状态，相关服务保持停止。"
      return 1
    fi
  fi

  # 校验文件恢复结果：安装前存在→指纹必须与安装前一致；安装前不存在→必须确实不存在
  local cfp
  for ((i=0; i<${#S[@]}; i++)); do
    src="${S[$i]}"; existed="${E[$i]}"; pre="${P[$i]}"
    if [[ "$existed" == "1" ]]; then
      cfp="$(file_fingerprint "$src")"
      fp_equal "$cfp" "$pre" || { red "校验失败：$src 未恢复到安装前指纹"; failed=1; }
    else
      [[ ! -e "$src" && ! -L "$src" ]] || { red "校验失败：$src 仍然存在"; failed=1; }
    fi
  done

  if (( failed != 0 )); then
    red "文件恢复结果校验失败：相关服务保持停止，备份与恢复日志均已保留。"
    return 1
  fi

  # 所有前置恢复操作和文件结果校验成功后，才允许恢复 active 状态。
  # 服务恢复是幂等的：目标状态由 state.txt 中安装前 active/enabled 推导。
  if (( xray_owned )); then
    restore_active "$XRAY_SERVICE" "$xa" ||
      { red "恢复失败：无法恢复 $XRAY_SERVICE 的运行状态"; return 1; }
  fi
  if (( panel_owned )); then
    restore_active "$PANEL_SERVICE" "$na" ||
      { red "恢复失败：无法恢复 $PANEL_SERVICE 的运行状态"; return 1; }
  fi

  # 校验服务 active 与 enabled 是否都恢复到安装前状态（仅校验本事务管理的服务）
  if (( xray_owned )) && ! verify_service_state "$XRAY_SERVICE" "$xa" "$xe"; then
    red "校验失败：$XRAY_SERVICE 的运行或启用状态与安装前不一致"; failed=1
  fi
  if (( panel_owned )) && ! verify_service_state "$PANEL_SERVICE" "$na" "$ne"; then
    red "校验失败：$PANEL_SERVICE 的运行或启用状态与安装前不一致"; failed=1
  fi

  (( failed == 0 ))
}

# 自动回滚（安装进程内 EXIT trap 使用）
rollback_all(){
  [[ -n "${BACKUP_DIR:-}" && -d "${BACKUP_DIR:-}" ]] || return 1
  yellow "正在回滚本次安装……"
  # 先验证原始磁盘记录，避免 state_write 覆盖损坏字段后掩盖异常。
  local issues
  if ! issues="$(verify_transaction "$BACKUP_DIR")"; then
    red "回滚中止：事务元数据或备份校验失败，未执行恢复动作："
    echo "$issues"
    return 1
  fi
  if ! state_write "ROLLBACK_IN_PROGRESS" "rollback"; then
    red "无法持久化 ROLLBACK_IN_PROGRESS 状态，已中止自动回滚文件操作。"
    red "请保留备份目录并在确认状态后使用 --recover：$BACKUP_DIR"
    return 1
  fi
  if restore_transaction "$BACKUP_DIR"; then
    # 最终状态必须可靠落盘（含回读校验）；写入失败时绝不能报告回滚成功。
    if ! state_write "ROLLED_BACK" "rolled-back"; then
      red "回滚文件已完成，但无法持久化 ROLLED_BACK 状态（状态持久化失败）。"
      red "不能报告回滚成功，请保留并检查备份目录：$BACKUP_DIR"
      return 1
    fi
    green "回滚完成：已恢复安装前文件状态。备份保留在：$BACKUP_DIR"
    return 0
  else
    # ROLLBACK_FAILED 自身若也无法写入，必须明确报告状态持久化失败。
    if ! state_write "ROLLBACK_FAILED" "rollback-failed"; then
      red "回滚未能完全完成，且无法持久化 ROLLBACK_FAILED 状态（状态持久化失败）。"
      red "请保留并检查备份目录：$BACKUP_DIR"
      return 1
    fi
    red "回滚未能完全完成。请保留并检查备份目录：$BACKUP_DIR"
    return 1
  fi
}

cleanup_temp(){
  if [[ -n "${PASSWORD_FILE:-}" ]]; then
    rm -f -- "$PASSWORD_FILE" 2>/dev/null || true
  fi
  PASSWORD_FILE=""
  unset PANEL_PASSWORD PASSWORD2 REALITY_PRIVATE_KEY KEY_OUTPUT 2>/dev/null || true
}

on_exit(){
  local rc=$?
  trap - EXIT INT TERM
  cleanup_temp
  if (( rc != 0 && ROLLBACK_READY == 1 && INSTALL_COMMITTED == 0 )); then
    if ! rollback_all; then
      red "严重警告：安装失败，而且自动回滚未能完全完成！"
      red "请勿直接重新运行安装脚本。"
      red "请先执行：bash install-dual.sh --check"
      red "再执行：  bash install-dual.sh --recover"
    fi
  fi
  exit "$rc"
}

# =========================================================
# 安装互斥锁：基于 flock，进程退出自动释放
# =========================================================
acquire_lock(){
  command -v flock >/dev/null 2>&1 ||
    die "缺少 flock 命令（util-linux），无法安全加锁。"
  mkdir -p "$(dirname "$LOCK_FILE")"
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    die "检测到另一个安装或恢复任务正在运行。请等待其结束，或确认无残留后再试。"
  fi
}

# =========================================================
# 未完成 / 回滚失败事务扫描
# =========================================================
# 列出所有事务目录。刻意包含缺少状态文件的目录：
# 事务目录一经创建即应可识别，缺失状态属于异常，必须被发现而非静默忽略。
list_transactions(){
  local d
  shopt -s nullglob
  for d in "${BACKUP_PREFIX}"*; do
    [[ -d "$d" ]] && printf '%s\n' "$d"
  done
  shopt -u nullglob
}

# 打印所有未完成（IN_PROGRESS / ROLLBACK_IN_PROGRESS）、回滚失败，
# 或状态未知/状态文件缺失（异常目录）的事务。
find_unfinished_transactions(){
  local d status
  while IFS= read -r d; do
    [[ -n "$d" ]] || continue
    status="$(state_get "$d/$STATE_FILE" status)"
    case "$status" in
      COMMITTED|ROLLED_BACK|NOT_STARTED)
        ;;
      *)
        # 包括空状态（状态文件缺失/损坏）——一律视为需要关注的异常事务
        printf '%s\t%s\t%s\n' "$d" "${status:-UNKNOWN}" "$(state_get "$d/$STATE_FILE" current_stage)"
        ;;
    esac
  done < <(list_transactions)
}

# =========================================================
# 覆盖保护：分类诊断
# =========================================================
detect_existing(){
  local xray_bin="" panel_py=0 xray_conf=0 xray_conf_valid=0
  local xray_unit=0 panel_unit=0 panel_conf=0 auth_f=0 secret_f=0
  local xray_user=0 xray_group=0 xray_running=0 modules=0
  local -a found=()

  xray_bin="$(command -v xray 2>/dev/null || true)"
  local p
  for p in /usr/local/bin/xray /usr/bin/xray /usr/local/sbin/xray /usr/sbin/xray; do
    [[ -x "$p" ]] && { xray_bin="$p"; break; }
  done

  [[ -f "$PANEL_FILE" ]] && panel_py=1
  [[ -f "$PANEL_CONFIG" ]] && panel_conf=1
  [[ -f "$AUTH_FILE" ]] && auth_f=1
  [[ -f "$SECRET_FILE" ]] && secret_f=1
  [[ -f "$XRAY_CONFIG" ]] && xray_conf=1
  if (( xray_conf )); then
    python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$XRAY_CONFIG" >/dev/null 2>&1 && xray_conf_valid=1
  fi
  svc_exists "$XRAY_SERVICE" && xray_unit=1
  svc_exists "$PANEL_SERVICE" && panel_unit=1
  [[ -d "/etc/systemd/system/xray.service.d" || -d "/etc/systemd/system/xray@.service.d" ]] && modules=1

  getent passwd xray >/dev/null 2>&1 && xray_user=1
  getent group xray >/dev/null 2>&1 && xray_group=1

  local proc_comm proc_name
  for proc_comm in /proc/[0-9]*/comm; do
    [[ -r "$proc_comm" ]] || continue
    IFS= read -r proc_name < "$proc_comm" || continue
    if [[ "$proc_name" == "xray" ]]; then
      xray_running=1
      break
    fi
  done

  # 收集痕迹（Cloudflared 属于用户外部服务，绝不计入安装痕迹）
  (( panel_py ))        && found+=("面板程序 $PANEL_FILE")
  (( panel_conf ))      && found+=("面板配置 $PANEL_CONFIG")
  (( auth_f ))          && found+=("认证文件 $AUTH_FILE")
  (( secret_f ))        && found+=("密钥文件 $SECRET_FILE")
  (( xray_conf ))       && found+=("Xray 配置 $XRAY_CONFIG")
  (( xray_unit ))       && found+=("systemd 单元 $XRAY_SERVICE")
  (( panel_unit ))      && found+=("systemd 单元 $PANEL_SERVICE")
  (( modules ))        && found+=("Xray systemd drop-in 目录")
  [[ -n "$xray_bin" ]]  && found+=("Xray 程序 $xray_bin")
  (( xray_user ))       && found+=("Xray 用户")
  (( xray_group ))      && found+=("Xray 用户组")
  (( xray_running ))    && found+=("正在运行的 Xray 进程")

  if (( ${#found[@]} == 0 )); then
    green "未检测到已有部署痕迹，可进行全新安装。"
    return 0
  fi

  echo
  yellow "检测到已有安装痕迹："
  local item
  for item in "${found[@]}"; do
    red "  - $item"
  done
  echo

  # 分类诊断
  local complete=0
  if [[ -n "$xray_bin" ]] && (( xray_conf_valid )) && (( xray_unit )) &&
     (( panel_py )) && (( panel_unit )) && (( panel_conf )) && (( auth_f )) && (( secret_f )); then
    complete=1
  fi

  if (( complete )); then
    red "诊断结论：完整安装（A）。"
    red "系统已存在完整的 Xray + 面板部署，禁止重复安装，以免覆盖节点、密钥或账号。"
    red "如需检查或恢复，请使用：bash install-dual.sh --check / --recover"
  elif (( ${#found[@]} == 1 )) && (( xray_conf == 0 )) && (( panel_conf == 0 )) &&
       (( panel_py == 0 )) && (( auth_f == 0 )) && (( secret_f == 0 )); then
    red "诊断结论：残留文件（C）。"
    red "仅发现孤立的程序/服务/用户等痕迹，且缺少配置，可能是上一次安装或卸载的残留。"
    red "脚本不会自动删除这些文件。请先执行 --check 确认，必要时人工处理。"
  elif (( xray_conf || panel_conf || panel_py )); then
    red "诊断结论：部分安装（B）。"
    red "存在部分安装文件，可能是上一次安装中断。请勿直接重装。"
    red "请先执行：bash install-dual.sh --check"
    red "再执行：  bash install-dual.sh --recover"
  else
    red "诊断结论：未知部署（D）。"
    red "检测到无法明确归类的部署痕迹（例如存在 Xray 用户/服务但缺少匹配配置）。"
    red "为保守起见停止安装，请先执行 --check 人工确认。"
  fi
  echo
  die "为保护现有部署，安装已停止。"
}

# =========================================================
# 只读检查模式 --check
# =========================================================
check_mode(){
  local os_id="" os_ver="" arch="" xray_bin=""
  local xray_conf_valid=0 panel_syntax=0 panel_conf_valid=0
  local unfinished=0 rollback_failed=0 artifacts=0 complete=1

  echo "=================================================="
  echo "       部署状态检查（只读，不会修改系统）"
  echo "=================================================="

  # 系统版本与架构
  if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    os_id="${ID:-unknown}"; os_ver="${VERSION_ID:-unknown}"
    echo "[系统]      ${PRETTY_NAME:-$os_id $os_ver}"
  else
    echo "[系统]      无法识别（缺少 /etc/os-release）"
  fi
  arch="$(uname -m 2>/dev/null || echo unknown)"
  echo "[架构]      $arch"
  if [[ "$os_id" == "debian" && "${os_ver%%.*}" == "13" ]]; then
    echo "[版本]      匹配 Debian 13"
  else
    echo "[版本]      提示：目标系统为 Debian 13，当前为 $os_id $os_ver"
  fi

  # systemd
  if [[ -d /run/systemd/system ]]; then
    echo "[systemd]   已检测到 systemd"
  else
    echo "[systemd]   未检测到 systemd（/run/systemd/system 不存在）"
  fi

  # Xray
  xray_bin="$(command -v xray 2>/dev/null || true)"
  if [[ -n "$xray_bin" ]]; then
    echo "[Xray]      程序存在：$xray_bin"
    artifacts=1
  else
    echo "[Xray]      未找到 Xray 程序"
  fi

  if [[ -f "$XRAY_CONFIG" ]]; then
    artifacts=1
    if python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$XRAY_CONFIG" >/dev/null 2>&1; then
      xray_conf_valid=1
      echo "[Xray 配置] 存在且 JSON 有效：$XRAY_CONFIG"
    else
      echo "[Xray 配置] 存在但 JSON 无效：$XRAY_CONFIG"
    fi
  else
    echo "[Xray 配置] 不存在：$XRAY_CONFIG"
  fi

  if svc_exists "$XRAY_SERVICE"; then
    artifacts=1
    echo "[Xray 服务] 单元存在；is-active=$(svc_active "$XRAY_SERVICE" && echo yes || echo no) is-enabled=$(svc_enabled "$XRAY_SERVICE" && echo yes || echo no)"
  else
    echo "[Xray 服务] 未找到 $XRAY_SERVICE 单元"
  fi

  # 面板
  if [[ -f "$PANEL_FILE" ]]; then
    artifacts=1
    if python3 -c 'import sys; compile(open(sys.argv[1],encoding="utf-8").read(), sys.argv[1], "exec")' "$PANEL_FILE" >/dev/null 2>&1; then
      panel_syntax=1
      echo "[面板程序] 存在且语法正常：$PANEL_FILE"
    else
      echo "[面板程序] 存在但 Python 语法检查失败：$PANEL_FILE"
    fi
  else
    echo "[面板程序] 不存在：$PANEL_FILE"
  fi

  if [[ -f "$PANEL_CONFIG" ]]; then
    artifacts=1
    if python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$PANEL_CONFIG" >/dev/null 2>&1; then
      panel_conf_valid=1
      echo "[面板配置] 存在且 JSON 有效：$PANEL_CONFIG"
    else
      echo "[面板配置] 存在但 JSON 无效：$PANEL_CONFIG"
    fi
  else
    echo "[面板配置] 不存在：$PANEL_CONFIG"
  fi

  if svc_exists "$PANEL_SERVICE"; then
    artifacts=1
    echo "[面板服务] 单元存在；is-active=$(svc_active "$PANEL_SERVICE" && echo yes || echo no) is-enabled=$(svc_enabled "$PANEL_SERVICE" && echo yes || echo no)"
  else
    echo "[面板服务] 未找到 $PANEL_SERVICE 单元"
  fi

  # 认证文件
  local f perm
  for f in "$AUTH_FILE" "$SECRET_FILE"; do
    if [[ -f "$f" ]]; then
      artifacts=1
      perm="$(stat -c '%a' "$f" 2>/dev/null || echo '?')"
      if [[ "$perm" == "600" ]]; then
        echo "[认证文件] $f 存在，权限 $perm"
      else
        echo "[认证文件] $f 存在，但权限为 $perm（建议 600）"
      fi
    else
      echo "[认证文件] 不存在：$f"
    fi
  done

  # Cloudflared（外部服务，仅供查看）
  if svc_exists "$CLOUDFLARED_SERVICE"; then
    echo "[Cloudflared] 单元存在；is-active=$(svc_active "$CLOUDFLARED_SERVICE" && echo yes || echo no)（由用户自行管理）"
  else
    echo "[Cloudflared] 未找到 $CLOUDFLARED_SERVICE（属用户外部服务，不影响判定）"
  fi

  # 事务扫描
  local d status
  echo "[事务]      扫描 $BACKUP_PREFIX*"
  while IFS= read -r d; do
    [[ -n "$d" ]] || continue
    status="$(state_get "$d/$STATE_FILE" status)"
    case "$status" in
      IN_PROGRESS|ROLLBACK_IN_PROGRESS)
        unfinished=$((unfinished+1))
        echo "           - $d  状态=$status  阶段=$(state_get "$d/$STATE_FILE" current_stage)"
        ;;
      ROLLBACK_FAILED)
        rollback_failed=$((rollback_failed+1))
        echo "           - $d  状态=$status  阶段=$(state_get "$d/$STATE_FILE" current_stage)"
        ;;
      COMMITTED|ROLLED_BACK|NOT_STARTED)
        echo "           - $d  状态=$status"
        ;;
      *)
        # 状态未知或状态文件缺失：异常事务目录，必须显式报出而不是静默忽略
        unfinished=$((unfinished+1))
        echo "           - $d  状态未知或状态文件缺失（异常事务目录）"
        ;;
    esac
  done < <(list_transactions)

  # 残留临时文件
  local resid
  for resid in /run/node-panel-password.* /tmp/panel_dual.update.* "${XRAY_CONFIG}.new" "${PANEL_CONFIG}.new"; do
    [[ -e "$resid" ]] && echo "[残留]      发现临时文件：$resid"
  done

  # 完整性判定
  if [[ -n "$xray_bin" ]] && (( xray_conf_valid )) && svc_exists "$XRAY_SERVICE" &&
     [[ -f "$PANEL_FILE" ]] && (( panel_syntax )) && (( panel_conf_valid )) &&
     svc_exists "$PANEL_SERVICE" && [[ -f "$AUTH_FILE" ]] && [[ -f "$SECRET_FILE" ]]; then
    complete=1
  else
    complete=0
  fi

  echo
  echo "--------------------------------------------------"
  echo "检查结论："

  if (( rollback_failed > 0 )); then
    red "自动回滚失败，需要人工处理。"
    echo "  存在回滚失败的事务。请保留备份目录，执行 --recover 查看，必要时人工恢复。"
  elif (( unfinished > 0 )); then
    red "安装中断，存在未完成或异常的安装事务。"
    echo "  请先执行：bash install-dual.sh --recover"
    echo "  恢复完成前不要重新安装，也不要删除残留文件。"
    echo "  若事务缺少状态文件（异常事务目录），--recover 会拒绝自动恢复，需人工核对。"
  elif (( complete )); then
    if svc_active "$XRAY_SERVICE" && svc_active "$PANEL_SERVICE"; then
      yellow "已安装（完整），禁止重复安装。"
      echo "  如需升级面板，请使用 update.sh。"
    else
      red "已安装，但服务当前异常。"
      echo "  xray is-active=$(svc_active "$XRAY_SERVICE" && echo yes || echo no)，node-panel is-active=$(svc_active "$PANEL_SERVICE" && echo yes || echo no)"
      echo "  请查看：journalctl -u xray -u node-panel -n 50 --no-pager"
    fi
  elif (( artifacts )); then
    red "检测到不完整或不一致的部署状态。"
    echo "  存在部分安装痕迹但未构成完整部署，请勿直接重装。"
    echo "  建议先执行：bash install-dual.sh --recover（若有未完成事务），否则人工核对。"
  else
    green "未安装，可进行全新安装。"
  fi
  echo "--------------------------------------------------"
}

# =========================================================
# 恢复模式 --recover
# =========================================================
# 校验事务备份是否完整、可自动恢复。任何一项不满足都拒绝恢复：
#   1) 事务 ID 与状态字段，且清单必须已标记完整（manifest_complete=1）；
#   2) 清单字段数量/路径合法性（禁止信任未经验证的清单路径）；
#   3) 备份完整性（existed=1 必须有对应备份）；
#   4) “原本不存在”文件的记录（existed=0 必须有 .absent 标记）；
#   5) 清单路径不得重复、备份路径不得映射冲突、状态与指纹格式必须合法；
#   6) hashes.tsv 基本完整性（字段数量/路径/指纹格式/标记/是否受管/是否矛盾）。
verify_transaction(){
  local dir="$1"
  local st="$dir/$STATE_FILE"
  local manifest="$dir/$MANIFEST_FILE"
  local -a issues=()

  # 先检查决定服务/账户恢复行为的原始字段；失败时立即返回，不做任何恢复动作。
  if ! state_verify_fields "$st"; then
    issues+=("state.txt 字段缺失、重复、为空或非法（服务状态必须为 0 / 1）")
  fi
  if ! verify_binary_fields "$dir/$META_FILE" xray_user_existed xray_group_existed; then
    issues+=("meta.txt 缺失或字段损坏（用户/组字段必须唯一且为 0 / 1）")
  fi
  if (( ${#issues[@]} > 0 )); then
    printf '%s\n' "${issues[@]}"
    return 1
  fi
  [[ -f "$manifest" ]] || issues+=("缺少事务清单 manifest.tsv")

  # 1) 事务 ID 与状态字段 + 清单完整性标记
  local tid status mc
  if [[ -f "$st" ]]; then
    tid="$(state_get "$st" transaction_id)"
    status="$(state_get "$st" status)"
    mc="$(state_get "$st" manifest_complete)"
    [[ -n "$tid" ]] || issues+=("状态文件缺少 transaction_id")
    [[ -n "$status" ]] || issues+=("状态文件缺少 status 字段")
    case "$status" in
      IN_PROGRESS|ROLLBACK_IN_PROGRESS|ROLLBACK_FAILED|COMMITTED|ROLLED_BACK|NOT_STARTED) ;;
      *) issues+=("状态字段取值非法：${status:-空}") ;;
    esac
    [[ -z "$tid" || "$tid" == "$(basename "$dir")" ]] ||
      issues+=("事务 ID 与目录名不一致：$tid != $(basename "$dir")")
    # 清单未标记完整（缺失/为 0/非法）时不得自动恢复，需人工处理
    case "$mc" in
      1) ;;
      0|"") issues+=("事务清单未标记完整（manifest_complete=${mc:-缺失}），拒绝自动恢复") ;;
      *) issues+=("manifest_complete 取值非法：$mc") ;;
    esac
  fi

  # 2) 清单字段/路径合法性 + 3) 备份完整性（含内容校验）+ 4) 原本不存在文件的记录
  #    + 5) 路径重复与备份路径映射冲突；6) hashes.tsv 基本完整性。
  if [[ -f "$manifest" ]]; then
    file_has_final_newline "$manifest" || issues+=("事务清单为空或末行未以换行结束（可能截断）：$manifest")
    local raw s r e pre bak bfp n=0
    local -A seen_src=() seen_rel=()
    while IFS= read -r raw || [[ -n "$raw" ]]; do
      n=$((n+1))
      # 字段数量必须正确，且四列均非空；在 read 折叠空白分隔符之前检查空字段。
      if [[ "$(tab_count "$raw")" != "3" || "$raw" == $'\t'* ||
            "$raw" == *$'\t' || "$raw" == *$'\t\t'* ]]; then
        issues+=("清单第 $n 行字段数量错误或包含空字段（应恰好 4 个非空字段）"); continue
      fi
      IFS=$'\t' read -r s r e pre <<< "$raw"
      if [[ -z "$s" || "$s" != /* || "$s" == *..* ]]; then
        issues+=("清单路径非法（src）：${s:-空}"); continue
      fi
      if [[ -z "${r:-}" || "$r" == /* || "$r" == *..* ]]; then
        issues+=("清单相对路径非法（rel）：${r:-空}"); continue
      fi
      # 路径不得重复；备份相对路径不得冲突（一个备份路径只能映射一个源文件）
      if [[ -n "${seen_src[$s]:-}" ]]; then
        issues+=("清单路径重复（src）：$s")
      fi
      seen_src["$s"]=1
      if [[ -n "${seen_rel[$r]:-}" ]]; then
        issues+=("备份路径映射冲突（rel 重复）：$r")
      fi
      seen_rel["$r"]=1
      # 指纹格式必须合法
      fingerprint_ok "${pre:-}" || issues+=("清单指纹格式非法：$s")
      case "$e" in
        1)
          bak="$dir/$r"
          if [[ ! -e "$bak" && ! -L "$bak" ]]; then
            issues+=("备份不完整：缺少 $bak")
          else
            # 备份内容/类型/权限/属主必须与清单记录的安装前指纹一致，
            # 否则视为备份被损坏或篡改，拒绝自动恢复（绝不使用损坏备份）。
            bfp="$(file_fingerprint "$bak")"
            fp_equal "$bfp" "${pre:-}" ||
              issues+=("备份与安装前指纹不一致（可能损坏）：$bak")
          fi
          ;;
        0)
          [[ "$pre" == "MISSING" ]] || issues+=("清单记录矛盾（existed=0 但安装前指纹非 MISSING）：$s")
          [[ -e "$dir/$r.absent" || -L "$dir/$r.absent" ]] ||
            issues+=("缺少 absent 标记（原本不存在）：$dir/$r.absent")
          ;;
        *)
          issues+=("清单记录非法（existed=$e）：$s")
          ;;
      esac
    done < "$manifest"
    (( n > 0 )) || issues+=("事务清单没有任何记录")
  fi

  # 6) hashes.tsv 基本完整性：字段数量、路径合法、指纹格式、标记必须为 post、
  #    路径须受本事务管理；同一路径允许合法追加 post，hash_of 仍取最后一条。
  local hf="$dir/$HASH_FILE"
  if [[ -f "$hf" ]]; then
    file_has_final_newline "$hf" || issues+=("安装后指纹文件为空或末行未以换行结束（可能截断）：$hf")
    local hraw hs hfp htag m=0
    local -A seen_tag=()
    while IFS= read -r hraw || [[ -n "$hraw" ]]; do
      m=$((m+1))
      if [[ "$(tab_count "$hraw")" != "2" || "$hraw" == $'\t'* ||
            "$hraw" == *$'\t' || "$hraw" == *$'\t\t'* ]]; then
        issues+=("hashes.tsv 第 $m 行字段数量错误或包含空字段（应恰好 3 个非空字段）"); continue
      fi
      IFS=$'\t' read -r hs hfp htag <<< "$hraw"
      if [[ -z "$hs" || "$hs" != /* || "$hs" == *..* ]]; then
        issues+=("hashes.tsv 第 $m 行路径非法：${hs:-空}"); continue
      fi
      fingerprint_ok "${hfp:-}" || issues+=("hashes.tsv 指纹格式非法：$hs")
      [[ "${htag:-}" == "post" ]] || issues+=("hashes.tsv 标记非法（应为 post）：$hs")
      # 同一路径出现相互矛盾的记录（标记不一致）→ 拒绝恢复
      if [[ -n "${seen_tag[$hs]:-}" && "${seen_tag[$hs]}" != "${htag:-}" ]]; then
        issues+=("hashes.tsv 同一路径出现相互矛盾的记录：$hs")
      fi
      seen_tag["$hs"]="${htag:-}"
      # 指纹必须属于本事务管理的文件
      manifest_has "$dir" "$hs" ||
        issues+=("hashes.tsv 记录了不受本事务管理的路径：$hs")
    done < "$hf"
  elif [[ "$(state_get "$st" hashes_complete)" == "1" ]]; then
    issues+=("hashes_complete=1 但缺少 hashes.tsv")
  fi

  if (( ${#issues[@]} > 0 )); then
    printf '%s\n' "${issues[@]}"
    return 1
  fi
  return 0
}

recover_mode(){
  local -a tdirs=() tstatus=() tstage=()
  local d status stage
  while IFS=$'\t' read -r d status stage; do
    [[ -n "$d" ]] || continue
    tdirs+=("$d"); tstatus+=("$status"); tstage+=("$stage")
  done < <(find_unfinished_transactions)

  echo "=================================================="
  echo "       安装事务恢复（--recover）"
  echo "=================================================="

  if (( ${#tdirs[@]} == 0 )); then
    green "未发现未完成或回滚失败的事务，无需恢复。"
    d=""
    while IFS= read -r d; do
      [[ -n "$d" ]] || continue
      echo "  - $d  状态=$(state_get "$d/$STATE_FILE" status)"
    done < <(list_transactions)
    return 0
  fi

  echo "发现以下可恢复事务："
  local i
  for ((i=0; i<${#tdirs[@]}; i++)); do
    echo "  [$((i+1))] ${tdirs[$i]}"
    echo "        创建时间：$(state_get "${tdirs[$i]}/$STATE_FILE" created_at)"
    echo "        失败阶段：${tstage[$i]}"
    echo "        当前状态：${tstatus[$i]}"
  done
  echo

  local choice
  read -r -p "请选择要恢复的事务编号（回车取消）：" choice
  [[ -n "$choice" ]] || { yellow "已取消。"; return 0; }
  [[ "$choice" =~ ^[0-9]+$ ]] || die "无效编号。"
  local idx=$((choice-1))
  (( idx >= 0 && idx < ${#tdirs[@]} )) || die "编号超出范围。"

  local dir="${tdirs[$idx]}"
  local st="$dir/$STATE_FILE"

  echo
  yellow "事务目录：$dir"
  echo "状态：$(state_get "$st" status)"
  echo "阶段：$(state_get "$st" current_stage)"
  echo "创建时间：$(state_get "$st" created_at)"
  echo "安装前服务状态：xray(active=$(state_get "$st" xray_was_active), enabled=$(state_get "$st" xray_was_enabled)) "
  echo "                  node-panel(active=$(state_get "$st" node_panel_was_active), enabled=$(state_get "$st" node_panel_was_enabled))"
  echo "清单完整(manifest_complete)=$(state_get "$st" manifest_complete)  安装后指纹已记录(hashes_complete)=$(state_get "$st" hashes_complete)"
  echo "（指纹未记录完整时，恢复会对已改动但无记录的文件保守拒绝，不会猜测其归属。）"
  echo

  echo "正在校验备份完整性……"
  local issues
  if ! issues="$(verify_transaction "$dir")"; then
    red "该事务备份不完整或状态文件损坏，不能自动恢复："
    echo "$issues"
    red "请人工核对备份目录后手动处理。脚本不会猜测或强制恢复。"
    return 1
  fi
  green "备份完整性校验通过。"

  echo
  yellow "恢复将：停止相关服务 → 删除本事务新建的文件 → 还原事务内记录的文件 → 恢复服务状态。"
  yellow "不会影响 Cloudflared，也不会删除任何备份目录。"
  local CONFIRM
  read -r -p "确认执行恢复？输入 yes 继续：" CONFIRM
  if [[ ! "$CONFIRM" =~ ^([Yy]|[Yy][Ee][Ss])$ ]]; then
    yellow "已取消。"
    return 0
  fi

  # 标记为回滚进行中。先读回原有字段，避免 state_write 覆盖已记录的服务状态。
  BACKUP_DIR="$dir"
  TRANSACTION_ID="$(state_get "$st" transaction_id)"
  CREATED_AT="$(state_get "$st" created_at)"
  CURRENT_STAGE="recover"
  MANIFEST_COMPLETE="$(state_get "$st" manifest_complete)"
  HASHES_COMPLETE="$(state_get "$st" hashes_complete)"
  XRAY_WAS_ACTIVE="$(state_get "$st" xray_was_active)"
  XRAY_WAS_ENABLED="$(state_get "$st" xray_was_enabled)"
  NODEPANEL_WAS_ACTIVE="$(state_get "$st" node_panel_was_active)"
  NODEPANEL_WAS_ENABLED="$(state_get "$st" node_panel_was_enabled)"
  state_write "ROLLBACK_IN_PROGRESS" "recover" ||
    die "无法写入恢复状态（ROLLBACK_IN_PROGRESS），已中止恢复以避免状态不一致。"

  # verify=1：恢复前逐一校验目标文件与事务预期一致，不一致则停止，绝不覆盖。
  if restore_transaction "$dir" 1; then
    # 最终状态必须可靠落盘（含回读校验）；写入失败时绝不能报告恢复成功。
    if ! state_write "ROLLED_BACK" "rolled-back"; then
      red "恢复文件已完成，但无法持久化 ROLLED_BACK 状态（状态持久化失败）。"
      red "不能报告恢复成功，请人工核对：$dir"
      return 1
    fi
    green "恢复完成：事务已回滚成功，备份保留在：$dir"
    return 0
  else
    # ROLLBACK_FAILED 自身若也无法写入，必须明确报告状态持久化失败。
    if ! state_write "ROLLBACK_FAILED" "rollback-failed"; then
      red "恢复未能完全完成，且无法持久化 ROLLBACK_FAILED 状态（状态持久化失败）。"
      red "请人工核对：$dir"
      return 1
    fi
    red "恢复未能完全完成。已保留全部备份，请人工核对：$dir"
    return 1
  fi
}

# =========================================================
# 前置检查与模式分发
# =========================================================
echo

case "$MODE" in
  check)
    if [[ $EUID -ne 0 ]]; then
      yellow "提示：当前非 root，部分权限相关检查可能不准确。"
    fi
    check_mode
    exit 0
    ;;
  recover)
    [[ $EUID -eq 0 ]] || die "请使用 root 运行 --recover。"
    [[ -d /run/systemd/system ]] || die "未检测到 systemd。"
    acquire_lock
    recover_mode
    exit 0
    ;;
esac

# ---- 以下为正常安装流程 ----
[[ $EUID -eq 0 ]] || die "请使用 root 运行。"
[[ -d /run/systemd/system ]] || die "未检测到 systemd。"
[[ -f /etc/os-release ]] || die "无法识别系统。"

acquire_lock

# 注册安装事务 trap：正常错误退出、SIGINT、SIGTERM 均可触发回滚。
# 注意：SIGKILL 与断电无法被 trap 捕获，只能依靠持久化事务状态在下次运行时识别。
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# shellcheck disable=SC1091
. /etc/os-release
[[ "$ID" == "debian" ]] || die "本脚本仅针对 Debian。"
[[ "${VERSION_ID%%.*}" == "13" ]] ||
  yellow "提示：当前系统不是 Debian 13，尚未针对该版本验证。"

# 未完成事务必须先恢复，避免叠加损坏
UNFINISHED="$(find_unfinished_transactions || true)"
if [[ -n "$UNFINISHED" ]]; then
  red "检测到未完成或回滚失败的安装事务："
  printf '%s\n' "$UNFINISHED" | while IFS=$'\t' read -r d status stage; do
    red "  - $d  状态=$status  阶段=$stage"
  done
  red "为避免叠加损坏，安装已停止。"
  red "请先执行：bash install-dual.sh --check"
  red "再执行：  bash install-dual.sh --recover"
  red "确认恢复完成后再重新安装。"
  exit 1
fi

clear || true

echo "=================================================="
echo "       Xray 双节点 + Node Monitor 一键部署"
echo "=================================================="
echo "节点 A：VLESS + WebSocket + Cloudflare TLS"
echo "节点 B：VLESS + REALITY"
echo "面板：Node Monitor 双节点管理版"
echo "Cloudflared：由用户自行安装和配置"
echo "=================================================="
echo

# ------------------- 参数输入 -------------------

yellow "【1】面板设置"

while true; do
  read -r -s -p "设置面板密码（至少 16 位）：" PANEL_PASSWORD
  echo
  if (( ${#PANEL_PASSWORD} >= 16 )); then break; fi
  yellow "密码至少需要 16 位。"
done

read -r -s -p "再次输入面板密码：" PASSWORD2
echo
[[ "$PANEL_PASSWORD" == "$PASSWORD2" ]] || die "两次密码不一致。"
unset PASSWORD2

PANEL_PORT="$(ask "面板本地端口" "8765")"

yellow "【2】WebSocket 设置"

WS_PORT="$(ask "WS 源站端口（仅本机监听）" "10000")"
WS_HOST="$(ask "WS 客户端连接域名（Cloudflare 橙云域名）")"
WS_PUBLIC_PORT="$(ask "WS 客户端连接端口" "443")"
WS_PATH="$(ask "WS Path（留空自动生成）" "")"

yellow "【3】部署模式"

REALITY_ENABLED=0

echo "1. VLESS-WS + VLESS-REALITY"
echo "2. 仅部署 VLESS-WS（默认）"

while true; do
  read -r -p "请选择部署模式 [2]: " DEPLOY_MODE
  DEPLOY_MODE="${DEPLOY_MODE:-2}"

  case "$DEPLOY_MODE" in
    1)
      REALITY_ENABLED=1
      break
      ;;
    2)
      REALITY_ENABLED=0
      break
      ;;
    *)
      yellow "请输入 1 或 2。"
      ;;
  esac
done

# 预先初始化，避免 set -u 导致变量未定义
REALITY_PORT=""
REALITY_ADDR=""
REALITY_SNI=""
REALITY_TARGET=""

if (( REALITY_ENABLED )); then
  yellow "【4】REALITY 设置"

  REALITY_PORT="$(ask "REALITY 监听端口" "10086")"
  REALITY_ADDR="$(ask "REALITY 客户端连接地址（公网 IPv4 或域名）")"
  REALITY_SNI="$(ask "REALITY SNI / serverName")"
  REALITY_TARGET="$(ask "REALITY dest / target（例如 example.com:443）")"
fi

# ------------------- 参数验证 -------------------

for p in "$PANEL_PORT" "$WS_PORT" "$WS_PUBLIC_PORT"; do
  valid_port "$p" || die "端口格式错误：$p"
done

[[ "$PANEL_PORT" != "$WS_PORT" ]] || die "面板端口和 WS 端口不能相同。"

valid_host "$WS_HOST" || die "WS 域名格式错误。"

if (( REALITY_ENABLED )); then

  valid_port "$REALITY_PORT" || die "REALITY 端口格式错误。"

  [[ "$PANEL_PORT" != "$REALITY_PORT" ]] ||
    die "面板端口和 REALITY 端口不能相同。"

  [[ "$WS_PORT" != "$REALITY_PORT" ]] ||
    die "WS 和 REALITY 端口不能相同。"

  valid_host "$REALITY_ADDR" ||
    die "REALITY 地址格式错误（当前脚本不支持 IPv6）。"

  valid_host "$REALITY_SNI" ||
    die "REALITY SNI 格式错误。"

  [[ "$REALITY_TARGET" =~ ^([A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?):([0-9]{1,5})$ ]] ||
    die "REALITY target 格式错误，应为 hostname:port。"

  TARGET_PORT="${BASH_REMATCH[3]}"
  valid_port "$TARGET_PORT" ||
    die "REALITY target 端口必须在 1–65535 之间。"

fi

if [[ -z "$WS_PATH" ]]; then
  WS_PATH="/$(openssl rand -hex 16)"
fi

[[ "$WS_PATH" == /* ]] || die "WS Path 必须以 / 开头。"
[[ "$WS_PATH" != *[[:space:]\#\?]* ]] ||
  die "WS Path 不能包含空格、# 或 ?。"
echo
yellow "========== 配置确认 =========="
if (( REALITY_ENABLED )); then
  printf '%-25s %s\n' "部署模式：" "VLESS-WS + VLESS-REALITY"
fi
printf '%-25s %s\n' "面板监听：" "127.0.0.1:$PANEL_PORT"
printf '%-25s %s\n' "WS 源站：" "127.0.0.1:$WS_PORT"
printf '%-25s %s\n' "WS 公网入口：" "$WS_HOST:$WS_PUBLIC_PORT"
printf '%-25s %s\n' "WS Path：" "$WS_PATH"
if (( REALITY_ENABLED )); then
  printf '%-25s %s\n' "REALITY 监听：" "0.0.0.0:$REALITY_PORT"
  printf '%-25s %s\n' "REALITY 客户端地址：" "$REALITY_ADDR:$REALITY_PORT"
  printf '%-25s %s\n' "REALITY SNI：" "$REALITY_SNI"
  printf '%-25s %s\n' "REALITY Target：" "$REALITY_TARGET"
else
  printf '%-25s %s\n' "部署模式：" "仅 VLESS-WS"
fi

echo
yellow "确认开始安装？输入 yes 或 y 继续。"
read -r -p "> " CONFIRM

if [[ "$CONFIRM" =~ ^([Yy]|[Yy][Ee][Ss])$ ]]; then
  :
else
  die "已取消。"
fi

# ------------------- 覆盖保护、端口检查与事务备份 -------------------

green "安装前覆盖保护检查"
detect_existing

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$(mktemp -d "${BACKUP_PREFIX}${STAMP}.XXXXXX")"
TRANSACTION_ID="$(basename "$BACKUP_DIR")"
CREATED_AT="$(now_iso)"
chmod 700 "$BACKUP_DIR" || die "无法设置事务目录权限：$BACKUP_DIR"
mkdir -p "$BACKUP_DIR/files" || die "无法创建事务目录结构：$BACKUP_DIR"
: > "$BACKUP_DIR/$MANIFEST_FILE" || die "无法创建事务清单：$BACKUP_DIR"

# 事务目录创建后立即写入可识别的初始状态：
# 即使此后（创建清单、记录备份期间）被 SIGKILL，--check 与安装前置检查
# 也能发现该异常事务目录，而不会静默忽略。
MANIFEST_COMPLETE=0
state_write "IN_PROGRESS" "init" ||
  die "无法写入初始事务状态，安装中止（备份目录：$BACKUP_DIR）。"

for d in \
  "$PANEL_DIR" \
  /etc/node-panel \
  /usr/local/etc/xray \
  /etc/systemd/system/xray.service.d \
  /etc/systemd/system/xray@.service.d
do
  if [[ ! -d "$d" ]]; then
    TRACK_DIRS+=("$d")
    printf '%s\n' "$d" >> "$BACKUP_DIR/$DIRS_FILE"
  fi
done

# 记录安装前服务状态；覆盖保护通过时，服务应当不存在/未运行。
XRAY_WAS_ACTIVE="$(svc_active xray && echo 1 || echo 0)"
XRAY_WAS_ENABLED="$(svc_enabled xray && echo 1 || echo 0)"
NODEPANEL_WAS_ACTIVE="$(svc_active node-panel && echo 1 || echo 0)"
NODEPANEL_WAS_ENABLED="$(svc_enabled node-panel && echo 1 || echo 0)"

# 记录 Xray 用户/用户组是否原本存在，用于恢复时判断是否可删除
{
  printf 'xray_user_existed=%s\n' "$(getent passwd xray >/dev/null 2>&1 && echo 1 || echo 0)"
  printf 'xray_group_existed=%s\n' "$(getent group xray >/dev/null 2>&1 && echo 1 || echo 0)"
} > "$BACKUP_DIR/$META_FILE"
chmod 600 "$BACKUP_DIR/$META_FILE"

track_file "$XRAY_CONFIG" "files/xray-config.json"
track_file "${XRAY_CONFIG}.new" "files/xray-config.json.new"
track_file "$PANEL_FILE" "files/panel.py"
track_file "$PANEL_CONFIG" "files/panel-config.json"
track_file "${PANEL_CONFIG}.new" "files/panel-config.json.new"
track_file "$AUTH_FILE" "files/auth.json"
track_file "$SECRET_FILE" "files/secret.key"
track_file "$NODE_PANEL_UNIT" "files/node-panel.service"
track_file "/etc/systemd/system/xray.service" "files/xray.service"
track_file "/etc/systemd/system/xray@.service" "files/xray-template.service"
track_file "/etc/systemd/system/xray.service.d/10-donot_touch_single_conf.conf" "files/xray-single-dropin.service.conf"
track_file "/etc/systemd/system/xray@.service.d/10-donot_touch_single_conf.conf" "files/xray-template-single-dropin.service.conf"
track_file "$XRAY_RESTART_DROPIN" "files/xray-restart-dropin.service.conf"
track_file "/usr/local/lib/systemd/system/xray.service" "files/xray-local.service"
track_file "/usr/local/lib/systemd/system/xray@.service" "files/xray-local-template.service"
track_file "/lib/systemd/system/xray.service" "files/xray-lib.service"
track_file "/lib/systemd/system/xray@.service" "files/xray-lib-template.service"
track_file "/usr/local/bin/xray" "files/xray-bin"

# 清单记录完毕，标记清单完整；此后状态文件将携带 manifest_complete=1
MANIFEST_COMPLETE=1
set_stage "init"
ROLLBACK_READY=1

# 创建目录放在备份记录之后，失败时可清理新建的空目录。
mkdir -p "$PANEL_DIR" /etc/node-panel /usr/local/etc/xray
yellow "事务备份完成：$BACKUP_DIR"

# ------------------- 安装依赖 -------------------

set_stage "1/8 安装依赖"
green "[1/8] 安装依赖"

export DEBIAN_FRONTEND=noninteractive
apt-get update || die "APT 软件包索引更新失败。"

apt-get install -y --no-install-recommends \
  ca-certificates curl openssl python3 python3-qrcode python3-pil iproute2 \
  || die "系统依赖安装失败。"

# ------------------- 安装前端口占用检查 -------------------
PORTS_TO_CHECK=("$WS_PORT" "$PANEL_PORT")

if (( REALITY_ENABLED )); then
  PORTS_TO_CHECK+=("$REALITY_PORT")
fi

for p in "${PORTS_TO_CHECK[@]}"; do
  if port_in_use "$p"; then
    die "端口 $p 已被占用，请更换端口或先处理占用进程。"
  fi
done

# ------------------- 安装 Xray -------------------

set_stage "2/8 安装 Xray"
green "[2/8] 安装 Xray"

if ! command -v xray >/dev/null 2>&1; then
  XRAY_INSTALLER="$BACKUP_DIR/xray-install-release.sh"

  curl -fsSL "$XRAY_INSTALL" -o "$XRAY_INSTALLER" ||
    die "下载 Xray 官方安装器失败。"

  bash "$XRAY_INSTALLER" install --without-geodata --without-logfiles ||
    die "Xray 安装失败。"
fi

# 官方安装器会改写受管文件（/usr/local/bin/xray、xray.service 等）。
# 立即持久化这些文件的安装后指纹，避免此处被强杀后无法判定其归属。
# 指纹无法可靠落盘时立即中止，绝不在无记录的情况下继续修改受管文件。
if ! record_manifest_hashes "$BACKUP_DIR/$MANIFEST_FILE"; then
  die "无法持久化 Xray 安装后指纹，安装中止（备份目录：$BACKUP_DIR）。"
fi

XRAY_BIN="$(command -v xray || true)"
[[ -x "$XRAY_BIN" ]] || XRAY_BIN="/usr/local/bin/xray"
[[ -x "$XRAY_BIN" ]] || die "找不到 Xray。"

# ------------------- 生成身份 -------------------

set_stage "3/8 生成节点身份"
green "[3/8] 生成节点身份"

WS_UUID="$(python3 -c 'import uuid; print(uuid.uuid4())')"

REALITY_UUID=""
REALITY_SHORT_ID=""
REALITY_PRIVATE_KEY=""
REALITY_PUBLIC_KEY=""

if (( REALITY_ENABLED )); then

  REALITY_UUID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
  REALITY_SHORT_ID="$(openssl rand -hex 8)"

  KEY_OUTPUT="$("$XRAY_BIN" x25519 2>&1)" || die "REALITY 密钥生成失败。"

  REALITY_PRIVATE_KEY="$(
    printf '%s\n' "$KEY_OUTPUT" |
      sed -nE 's/^[[:space:]]*PrivateKey:[[:space:]]*//p' |
      head -n1
  )"

  REALITY_PUBLIC_KEY="$(
    printf '%s\n' "$KEY_OUTPUT" |
      sed -nE 's/^[[:space:]]*(Password \(PublicKey\)|Password|PublicKey):[[:space:]]*//p' |
      head -n1
  )"

  [[ -n "$REALITY_PRIVATE_KEY" ]] || die "未能读取 REALITY 私钥。"
  [[ -n "$REALITY_PUBLIC_KEY" ]] || die "未能读取 REALITY 公钥。"

  unset KEY_OUTPUT

fi

# ------------------- 生成 Xray 配置 -------------------

set_stage "4/8 生成 Xray 配置"
green "[4/8] 生成 Xray 配置"

python3 - "$XRAY_CONFIG" "$REALITY_ENABLED" "$WS_PORT" "$WS_PATH" "$WS_UUID" \
  "$REALITY_PORT" "$REALITY_UUID" "$REALITY_PRIVATE_KEY" \
  "$REALITY_SNI" "$REALITY_TARGET" "$REALITY_SHORT_ID" <<'PY'
import json, sys
from pathlib import Path

(
    output, reality_enabled, ws_port, ws_path, ws_uuid,
    reality_port, reality_uuid, private_key,
    sni, target, short_id
) = sys.argv[1:]

inbounds = [
    {
        "listen": "127.0.0.1",
        "port": int(ws_port),
        "protocol": "vless",
        "settings": {
            "clients": [{"id": ws_uuid}],
            "decryption": "none"
        },
        "streamSettings": {
            "network": "ws",
            "security": "none",
            "wsSettings": {"path": ws_path}
        }
    }
]

if reality_enabled == "1":
    inbounds.append({
        "listen": "0.0.0.0",
        "port": int(reality_port),
        "protocol": "vless",
        "settings": {
            "clients": [{
                "id": reality_uuid,
                "flow": "xtls-rprx-vision"
            }],
            "decryption": "none"
        },
        "streamSettings": {
            "network": "raw",
            "security": "reality",
            "realitySettings": {
                "show": False,
                "dest": target,
                "serverNames": [sni],
                "privateKey": private_key,
                "shortIds": [short_id]
            }
        }
    })

config = {
    "log": {"loglevel": "warning"},
    "inbounds": inbounds,
    "outbounds": [
        {"protocol": "freedom", "tag": "direct"},
        {"protocol": "blackhole", "tag": "blocked"}
    ]
}

path = Path(output)
tmp = path.with_name(path.name + ".new")
tmp.write_text(json.dumps(config, indent=2) + "\n")
tmp.chmod(0o640)
tmp.replace(path)
PY

unset REALITY_PRIVATE_KEY

chown root:xray "$XRAY_CONFIG" ||
  die "无法设置 Xray 配置文件所属用户和用户组。"

chmod 640 "$XRAY_CONFIG"

"$XRAY_BIN" run -test -config "$XRAY_CONFIG" ||
  die "Xray 配置检查失败。"

# 记录写入后的事务指纹，供 --recover 校验目标文件归属
record_hash "$XRAY_CONFIG"

# ------------------- 安装新版面板 -------------------

set_stage "5/8 安装面板"
green "[5/8] 安装 Node Monitor 面板"

curl -fsSL "$PANEL_URL" -o "$PANEL_FILE" ||
  die "下载 panel_dual.py 失败。请确认该文件已上传到 GitHub 仓库指定路径。"

grep -q 'PANEL_CONFIG = Path("/etc/node-panel/panel-config.json")' "$PANEL_FILE" ||
  die "下载到的文件不是预期的 panel_dual.py。"

record_hash "$PANEL_FILE"

# ------------------- 生成面板配置 -------------------

python3 - "$PANEL_CONFIG" "$REALITY_ENABLED" "$PANEL_PORT" \
  "$WS_PORT" "$WS_HOST" "$WS_PUBLIC_PORT" "$WS_PATH" \
  "$REALITY_PORT" "$REALITY_ADDR" "$REALITY_SNI" \
  "$REALITY_PUBLIC_KEY" "$REALITY_SHORT_ID" <<'PY'
import json, sys
from pathlib import Path

(
    output, reality_enabled, panel_port,
    ws_listen_port, ws_host, ws_port, ws_path,
    reality_listen_port, reality_address, reality_sni,
    reality_public_key, reality_short_id
) = sys.argv[1:]

cfg = {
    "panel_port": int(panel_port),
    "reality_enabled": reality_enabled == "1",
    "ws": {
        "listen_port": int(ws_listen_port),
        "host": ws_host,
        "port": int(ws_port),
        "path": ws_path
    }
}

if reality_enabled == "1":
    cfg["reality"] = {
        "listen_port": int(reality_listen_port),
        "address": reality_address,
        "port": int(reality_listen_port),
        "server_name": reality_sni,
        "public_key": reality_public_key,
        "short_id": reality_short_id,
        "fingerprint": "chrome",
        "network": "tcp",
        "flow": "xtls-rprx-vision"
    }

path = Path(output)
tmp = path.with_name(path.name + ".new")
tmp.write_text(json.dumps(cfg, indent=2) + "\n")
tmp.chmod(0o600)
tmp.replace(path)
PY

chmod 600 "$PANEL_CONFIG"

record_hash "$PANEL_CONFIG"

python3 -c 'import sys; compile(open(sys.argv[1], encoding="utf-8").read(), sys.argv[1], "exec")' "$PANEL_FILE" ||
  die "面板 Python 语法检查失败。"

# ------------------- 面板认证 -------------------

set_stage "6/8 创建面板认证信息"
green "[6/8] 创建面板认证信息"

# 密码先写入权限为 600 的临时文件，避免出现在进程命令行参数中。
PASSWORD_FILE="$(mktemp /run/node-panel-password.XXXXXX)"
chmod 600 "$PASSWORD_FILE"
printf '%s' "$PANEL_PASSWORD" > "$PASSWORD_FILE"

python3 - "$AUTH_FILE" "$SECRET_FILE" "$PASSWORD_FILE" <<'PY'
import hashlib, json, os, secrets, sys
from pathlib import Path

auth_file, secret_file, password_file = sys.argv[1:]
password = Path(password_file).read_text()
Path(password_file).unlink(missing_ok=True)

os.umask(0o077)

auth_path = Path(auth_file)
secret_path = Path(secret_file)

if auth_path.exists():
    os.chmod(auth_path, 0o600)
if secret_path.exists():
    os.chmod(secret_path, 0o600)

salt = secrets.token_bytes(16)
iterations = 310000
digest = hashlib.pbkdf2_hmac(
    "sha256", password.encode(), salt, iterations
).hex()

auth_path.write_text(json.dumps({
    "salt": salt.hex(),
    "iterations": iterations,
    "hash": digest
}) + "\n")

secret_path.write_bytes(secrets.token_bytes(32))

os.chmod(auth_path, 0o600)
os.chmod(secret_path, 0o600)
PY

rm -f "$PASSWORD_FILE"
PASSWORD_FILE=""
unset PANEL_PASSWORD

record_hash "$AUTH_FILE"
record_hash "$SECRET_FILE"

# ------------------- systemd 服务 -------------------

set_stage "7/8 创建服务"
green "[7/8] 创建面板 systemd 服务"

cat > "$NODE_PANEL_UNIT" <<EOF
[Unit]
Description=Lightweight Node Monitor Panel
After=network.target xray.service
Wants=xray.service

[Service]
Type=simple
User=root
WorkingDirectory=$PANEL_DIR
ExecStart=/usr/bin/python3 $PANEL_FILE
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
ReadWritePaths=/etc/node-panel /usr/local/etc/xray

[Install]
WantedBy=multi-user.target
EOF

record_hash "$NODE_PANEL_UNIT"

# Xray 服务自愈：使用 systemd drop-in 设置 Restart=on-failure，
# 不改写官方安装器生成的 xray.service，也不覆盖用户已有 drop-in。
mkdir -p "$(dirname "$XRAY_RESTART_DROPIN")"
if [[ -e "$XRAY_RESTART_DROPIN" ]]; then
  yellow "检测到已有 Xray drop-in，跳过写入以避免覆盖：$XRAY_RESTART_DROPIN"
else
  cat > "$XRAY_RESTART_DROPIN" <<'EOF'
[Service]
Restart=on-failure
RestartSec=5s
EOF
  chmod 644 "$XRAY_RESTART_DROPIN"
  record_hash "$XRAY_RESTART_DROPIN"
fi

systemctl daemon-reload
systemctl enable xray
systemctl enable --now xray
systemctl enable --now node-panel

# ------------------- 最终检查 -------------------

set_stage "8/8 最终检查"
green "[8/8] 最终检查"

# 所有受管文件均已就位，刷新安装后指纹，确保 --recover 校验覆盖全部清单条目。
# 指纹记录失败必须中止：绝不在关键指纹无法可靠记录时继续，更不得提交事务。
if ! record_manifest_hashes "$BACKUP_DIR/$MANIFEST_FILE"; then
  die "无法持久化受管文件的安装后指纹，安装中止（备份目录：$BACKUP_DIR）。"
fi
# 全部安装后指纹已记录，显式标记（与“备份清单完整”MANIFEST_COMPLETE 区分）。
HASHES_COMPLETE=1
if ! state_write "IN_PROGRESS" "8/8 最终检查"; then
  die "无法持久化 hashes_complete 标记，安装中止（备份目录：$BACKUP_DIR）。"
fi

"$XRAY_BIN" run -test -config "$XRAY_CONFIG" ||
  die "Xray 最终配置检查失败。"

systemctl restart xray
systemctl restart node-panel
sleep 2

systemctl is-active --quiet xray ||
  die "Xray 启动失败，请检查 journalctl -u xray。"
systemctl is-active --quiet node-panel ||
  die "Node Monitor 启动失败，请检查 journalctl -u node-panel。"

echo
echo "===== 服务状态 ====="
systemctl is-active xray
systemctl is-active node-panel

echo
echo "===== 监听端口 ====="

PORTS_TO_CHECK=("$WS_PORT" "$PANEL_PORT")

if (( REALITY_ENABLED )); then
  PORTS_TO_CHECK+=("$REALITY_PORT")
fi

for p in "${PORTS_TO_CHECK[@]}"; do
  if ! ss -H -lnt 2>/dev/null | awk -v p=":$p" '$4 ~ (p "$") {found=1} END {exit !found}'; then
    die "端口 $p 未监听，安装验收失败。"
  fi
  green "端口 $p 正在监听。"
done

echo
echo "===== 面板页面检查 ====="
USAGE_CODE="$(curl -o /dev/null -s -w '%{http_code}' "http://127.0.0.1:${PANEL_PORT}/usage")" || true
[[ "$USAGE_CODE" == "200" ]] ||
  die "面板 /usage 未返回预期的 HTTP 200（实际：${USAGE_CODE:-无响应}），请查看 node-panel 日志。"
green "面板 /usage 返回 HTTP 200。"

echo
echo "===== 关键文件与权限检查 ====="

for f in "$XRAY_CONFIG" "$PANEL_FILE" "$PANEL_CONFIG" "$AUTH_FILE" "$SECRET_FILE" "$NODE_PANEL_UNIT"; do
  [[ -f "$f" ]] || die "关键文件缺失：$f"
  green "存在：$f"
done

perm="$(stat -c '%a' "$PANEL_CONFIG" 2>/dev/null || echo '?')"
[[ "$perm" == "600" ]] || die "文件权限异常（应为 600）：$PANEL_CONFIG 当前为 $perm"
green "权限正常（600）：$PANEL_CONFIG"

# 认证文件：权限、属主、非空、内容必要属性都必须校验。
for f in "$AUTH_FILE" "$SECRET_FILE"; do
  perm="$(stat -c '%a' "$f" 2>/dev/null || echo '?')"
  [[ "$perm" == "600" ]] || die "认证文件权限异常（应为 600）：$f 当前为 $perm"
  owner="$(stat -c '%U:%G' "$f" 2>/dev/null || echo '?')"
  [[ "$owner" == "root:root" ]] || die "认证文件属主异常（应为 root:root）：$f 当前为 $owner"
  [[ -s "$f" ]] || die "认证文件为空：$f"
  green "认证文件属性正常（600 root:root 非空）：$f"
done

python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert all(k in d for k in ("salt","iterations","hash"))' "$AUTH_FILE" ||
  die "认证文件内容非法（缺少 salt/iterations/hash）：$AUTH_FILE"
green "认证文件内容有效：$AUTH_FILE"

# ------------------- 输出部署信息 -------------------

echo
green "=================================================="
green "                 部署完成"
green "=================================================="

echo "面板本地地址： http://127.0.0.1:$PANEL_PORT/usage"
echo
echo "WS 节点："
echo "  域名：$WS_HOST"
echo "  端口：$WS_PUBLIC_PORT"
echo "  源站：127.0.0.1:$WS_PORT"
echo "  UUID：$WS_UUID"
echo "  Path：$WS_PATH"
echo
if (( REALITY_ENABLED )); then
  echo "REALITY 节点："
  echo "  地址：$REALITY_ADDR"
  echo "  端口：$REALITY_PORT"
  echo "  UUID：$REALITY_UUID"
  echo "  SNI：$REALITY_SNI"
  echo "  Public Key：$REALITY_PUBLIC_KEY"
  echo "  Short ID：$REALITY_SHORT_ID"
  echo "  Flow：xtls-rprx-vision"
fi
echo
yellow "Cloudflare Tunnel 需要你自行配置："
echo "  $WS_HOST -> HTTP -> 127.0.0.1:$WS_PORT"
echo "  面板域名 -> HTTP -> 127.0.0.1:$PANEL_PORT"
echo
if (( REALITY_ENABLED )); then
  yellow "REALITY 需要 VPS 服务商允许 TCP $REALITY_PORT 入站。"
  yellow "如果运行在容器内，还需要宿主机映射该 TCP 端口。"
fi
yellow "本脚本没有安装、登录或配置 Cloudflared。"
echo
echo "查看日志："
echo "  journalctl -u xray -u node-panel -n 50 --no-pager"
echo
yellow "注意：请先将 panel_dual.py 上传到配置的 GitHub 仓库路径，再运行本脚本。"

# 所有检查通过，正式提交安装事务；之后不再自动回滚。
# 顺序至关重要：必须先成功写入磁盘状态并回读验证，才能关闭自动回滚。
# 若此处 state_write 失败，INSTALL_COMMITTED 仍为 0，on_exit 会照常触发回滚。
if ! state_write "COMMITTED" "done"; then
  die "无法写入 COMMITTED 事务状态，安装未提交，将执行回滚。"
fi
COMMITTED_STATUS="$(state_get "$BACKUP_DIR/$STATE_FILE" status)"
if [[ "$COMMITTED_STATUS" != "COMMITTED" ]]; then
  die "COMMITTED 状态回读校验失败（实际：${COMMITTED_STATUS:-空}），安装未提交，将执行回滚。"
fi
INSTALL_COMMITTED=1
ROLLBACK_READY=0
trap - EXIT INT TERM
cleanup_temp
green "安装事务已提交（status=COMMITTED）。备份保留在：$BACKUP_DIR"
