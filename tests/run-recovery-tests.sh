#!/usr/bin/env bash
# install.sh 安装事务恢复安全机制测试
#
# 用法：bash tests/run-recovery-tests.sh
#
# 测试通过“提取函数库（脚本顶层执行之前的部分）+ 打桩 systemd”的方式，
# 在隔离的临时目录中直接调用恢复/校验函数，不触碰真实系统状态。
# 本轮新增 T33–T43：交付时仅编写，尚未执行（不表示测试已通过）。
#
# shellcheck disable=SC2034
# SC2317：打桩函数由被 source 的函数库间接调用。
# shellcheck disable=SC2317
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/install.sh"
[[ -f "$SRC" ]] || SRC="$ROOT/install-dual.sh"
[[ -f "$SRC" ]] || { echo "缺少验收脚本"; exit 2; }
printf '测试目标：%s\n' "$SRC"
WORK="$(mktemp -d)" || exit 2
trap 'rm -rf -- "$WORK"' EXIT

PASS=0
FAIL=0
ok(){ PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
no(){ FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
expect(){ # expect <描述> <命令...> —— 期望失败(非0)
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then no "$desc（本应失败却成功）"; else ok "$desc"; fi
}
expect_ok(){ # expect_ok <描述> <命令...> —— 期望成功(0)
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$desc"; else no "$desc（本应成功却失败）"; fi
}

# ---- 提取函数库（顶层 case "$MODE" 之前的全部内容）----
# shellcheck disable=SC2016
LINE="$(grep -nF 'case "$MODE" in' "$SRC" | head -1 | cut -d: -f1)"
[[ -n "$LINE" ]] || { echo "无法定位函数库边界"; exit 2; }
sed -n "1,$((LINE-1))p" "$SRC" > "$WORK/lib.sh" || exit 2

# 安全默认值：每次加载都先隔离服务、账户命令与机器级 sync。
# 测试显式覆盖的 mock 仍生效；cp 等文件操作只针对 WORK 内路径。
# shellcheck disable=SC1091
lib(){
  set --
  source "$WORK/lib.sh" >/dev/null 2>&1 || return 2
  set +e
  stub_no_units
  fsync_path(){ return 0; }
  NODE_PANEL_UNIT="$WORK/mock-node-panel.service"
}

write_state(){ # write_state <dir> <status> <manifest_complete> [hashes_complete]
  printf 'xray_user_existed=0\nxray_group_existed=0\n' > "$1/meta.txt"
  cat > "$1/state.txt" <<EOF
transaction_id=$(basename "$1")
created_at=2026-01-01T00:00:00Z
updated_at=2026-01-01T00:00:00Z
status=$2
current_stage=init
xray_was_active=0
xray_was_enabled=0
node_panel_was_active=0
node_panel_was_enabled=0
manifest_complete=$3
hashes_complete=${4:-0}
EOF
}

stub_no_units(){
  svc_exists(){ return 1; }
  svc_active(){ return 1; }
  svc_enabled(){ return 1; }
  systemctl(){ return 0; }
  getent(){ return 1; }
  ps(){ return 0; }
  userdel(){ return 0; }
  groupdel(){ return 0; }
}
stub_panel_stop_fail(){
  svc_exists(){ return 0; }
  svc_active(){ return 1; }
  svc_enabled(){ return 1; }
  systemctl(){ return 1; }
}
stub_panel_still_active(){
  svc_exists(){ return 0; }
  svc_active(){ return 0; }
  svc_enabled(){ return 1; }
  systemctl(){ return 0; }
}
stub_panel_disable_fail(){
  svc_exists(){ return 0; }
  svc_active(){ return 1; }
  svc_enabled(){ return 1; }
  systemctl(){ case "$1" in stop) return 0 ;; disable) return 1 ;; *) return 0 ;; esac; }
}
stub_rolled_back_write_fail(){
  restore_transaction(){ return 0; }
  state_write(){ [[ "$1" == "ROLLED_BACK" ]] && return 1; return 0; }
}
stub_rollback_failed_write_fail(){
  restore_transaction(){ return 1; }
  state_write(){ [[ "$1" == "ROLLBACK_FAILED" ]] && return 1; return 0; }
}

expect_content(){
  local desc="$1" f="$2" want="$3" got
  got="$(cat "$f" 2>/dev/null || true)"
  if [[ "$got" == "$want" ]]; then ok "$desc"; else no "$desc（期望 $want，实际 ${got:-空}）"; fi
}
expect_journal_state(){
  local desc="$1" d="$2" src="$3" want="$4" got
  got="$(journal_state "$d" "$src")"
  if [[ "$got" == "$want" ]]; then ok "$desc"; else no "$desc（期望 $want，实际 ${got:-空}）"; fi
}
expect_file_absent(){
  local desc="$1" f="$2"
  if [[ ! -e "$f" && ! -L "$f" ]]; then ok "$desc"; else no "$desc（目标仍存在）"; fi
}
expect_file_present(){
  local desc="$1" f="$2"
  if [[ -e "$f" || -L "$f" ]]; then ok "$desc"; else no "$desc（目标不存在）"; fi
}
copy_func(){
  eval "$(declare -f "$1" | sed "1s/^[^ ]*/$2/")"
}

# =========================================================
# T0 state_write：字段缺失 / 非法状态必须返回非 0
# =========================================================
test_state_write_validation(){
  echo "T0 state_write 字段与失败校验"
  lib
  [[ "$(id -u)" == 0 ]] || { echo "  跳过（需 root）"; return; }
  BACKUP_DIR="$WORK/t0"; mkdir -p "$BACKUP_DIR"
  TRANSACTION_ID="t0"; CREATED_AT="2026-01-01T00:00:00Z"; MANIFEST_COMPLETE=1
  expect_ok "合法字段写入成功" state_write IN_PROGRESS init
  expect "非法状态返回非 0" state_write BOGUS init
  CREATED_AT=""
  expect "缺少 created_at 返回非 0" state_write IN_PROGRESS init
  CREATED_AT="2026-01-01T00:00:00Z"
  CURRENT_STAGE=""
  expect "缺少 stage 返回非 0" state_write IN_PROGRESS ""
}

# =========================================================
# T1 verify_transaction：manifest_complete 必须为 1
# =========================================================
test_manifest_complete(){
  echo "T1 verify_transaction 校验 manifest_complete"
  lib
  local d="$WORK/t1"
  mkdir -p "$d/files"
  printf '%s\t%s\t%s\t%s\n' "$WORK/t1-new" "files/n" 0 MISSING > "$d/manifest.tsv"
  : > "$d/files/n.absent"
  write_state "$d" IN_PROGRESS 0
  expect "manifest_complete=0 拒绝恢复" verify_transaction "$d"
  write_state "$d" IN_PROGRESS ""
  expect "manifest_complete 缺失 拒绝恢复" verify_transaction "$d"
  write_state "$d" IN_PROGRESS 2
  expect "manifest_complete 非法 拒绝恢复" verify_transaction "$d"
  write_state "$d" IN_PROGRESS 1
  expect_ok "manifest_complete=1 允许恢复" verify_transaction "$d"
}

# =========================================================
# T2 verify_service_state：单元缺失时的判定
# =========================================================
test_service_unit_missing(){
  echo "T2 verify_service_state 单元缺失判定"
  lib
  stub_no_units
  expect "安装前 active 且单元缺失 → 失败" verify_service_state "xray.service" 1 0
  expect "安装前 enabled 且单元缺失 → 失败" verify_service_state "xray.service" 0 1
  expect "安装前 active+enabled 且单元缺失 → 失败" verify_service_state "xray.service" 1 1
  expect_ok "安装前 inactive+disabled 且单元缺失 → 符合预期" verify_service_state "xray.service" 0 0
}

# =========================================================
# T3 权限变化必须被识别
# =========================================================
test_perm_change(){
  echo "T3 权限变化拒绝恢复"
  lib
  local d="$WORK/t3" f="$WORK/t3-file"
  mkdir -p "$d/files"
  printf 'content' > "$f"; chmod 600 "$f"
  cp -a "$f" "$d/files/f"
  local fp; fp="$(file_fingerprint "$f")"
  printf '%s\t%s\tpost\n' "$f" "$fp" > "$d/hashes.tsv"
  printf '%s\t%s\t%s\t%s\n' "$f" "files/f" 1 "$fp" > "$d/manifest.tsv"
  expect_ok "指纹一致时校验通过" verify_recovery_targets "$d"
  chmod 644 "$f"
  expect "内容未变但权限改变 → 拒绝恢复" verify_recovery_targets "$d"
}

# =========================================================
# T4 属主变化必须被识别
# =========================================================
test_owner_change(){
  echo "T4 属主变化拒绝恢复"
  [[ "$(id -u)" == 0 ]] || { echo "  跳过（需 root）"; return; }
  lib
  local d="$WORK/t4" f="$WORK/t4-file"
  mkdir -p "$d/files"
  printf 'content' > "$f"; chmod 600 "$f"
  cp -a "$f" "$d/files/f"
  local fp; fp="$(file_fingerprint "$f")"
  printf '%s\t%s\tpost\n' "$f" "$fp" > "$d/hashes.tsv"
  printf '%s\t%s\t%s\t%s\n' "$f" "files/f" 1 "$fp" > "$d/manifest.tsv"
  expect_ok "指纹一致时校验通过" verify_recovery_targets "$d"
  chown nobody "$f" 2>/dev/null || { echo "  跳过（无法切换属主）"; return; }
  expect "内容未变但属主改变 → 拒绝恢复" verify_recovery_targets "$d"
}

# =========================================================
# T5 符号链接目标变化必须被识别
# =========================================================
test_symlink_change(){
  echo "T5 符号链接目标变化拒绝恢复"
  lib
  local d="$WORK/t5" a="$WORK/t5-a" b="$WORK/t5-b" sl="$WORK/t5-link"
  mkdir -p "$d/files"
  printf 'a' > "$a"; printf 'b' > "$b"
  ln -s "$a" "$sl"
  local fp; fp="$(file_fingerprint "$sl")"
  printf '%s\t%s\tpost\n' "$sl" "$fp" > "$d/hashes.tsv"
  printf '%s\t%s\t%s\t%s\n' "$sl" "files/l" 1 "$fp" > "$d/manifest.tsv"
  expect_ok "指纹一致时校验通过" verify_recovery_targets "$d"
  ln -sfn "$b" "$sl"
  expect "符号链接目标改变 → 拒绝恢复" verify_recovery_targets "$d"
}

# =========================================================
# T6 初次检查后、恢复前目标被修改 → 立即停止且不覆盖
# =========================================================
test_change_between_check_and_restore(){
  echo "T6 恢复前目标被外部修改 → 停止且不覆盖"
  lib
  stub_no_units
  stop_services_off(){ :; }
  local d="$WORK/t6" victim="$WORK/t6-victim" other="$WORK/t6-other"
  mkdir -p "$d/files"
  write_state "$d" ROLLBACK_IN_PROGRESS 1 0
  printf 'victim-orig' > "$victim"
  printf 'other-orig' > "$other"
  cp -a "$victim" "$d/files/v"
  cp -a "$other" "$d/files/o"
  {
    printf '%s\t%s\tpost\n' "$victim" "$(file_fingerprint "$victim")"
    printf '%s\t%s\tpost\n' "$other" "$(file_fingerprint "$other")"
  } > "$d/hashes.tsv"
  {
    printf '%s\t%s\t%s\t%s\n' "$victim" "files/v" 1 "$(file_fingerprint "$victim")"
    printf '%s\t%s\t%s\t%s\n' "$other" "files/o" 1 "$(file_fingerprint "$other")"
  } > "$d/manifest.tsv"
  expect_ok "注入修改前夹具通过前置校验" verify_transaction "$d"
  VICTIM="$victim"
  cp(){
    if [[ -z "${CP_CALLED:-}" ]]; then
      CP_CALLED=1
      printf 'external-change' > "$VICTIM"
    fi
    command cp "$@"
  }
  expect "恢复过程中发现外部修改 → 返回非 0" restore_transaction "$d" 1
  if [[ "$(cat "$victim")" == "external-change" ]]; then
    ok "外部修改未被覆盖"
  else
    no "外部修改被覆盖（当前内容：$(cat "$victim")）"
  fi
}

# =========================================================
# T6b 无外部修改时恢复应成功（对照）
# =========================================================
test_restore_ok_control(){
  echo "T6b 无外部修改时恢复成功（对照）"
  lib
  stub_no_units
  local d="$WORK/t6b" f="$WORK/t6b-file"
  mkdir -p "$d/files"
  write_state "$d" ROLLBACK_IN_PROGRESS 1 0
  printf 'orig' > "$f"
  cp -a "$f" "$d/files/f"
  local fp; fp="$(file_fingerprint "$f")"
  printf '%s\t%s\tpost\n' "$f" "$fp" > "$d/hashes.tsv"
  printf '%s\t%s\t%s\t%s\n' "$f" "files/f" 1 "$fp" > "$d/manifest.tsv"
  printf 'changed-by-install' > "$f"
  if restore_transaction "$d" 1 >/dev/null 2>&1; then
    no "目标与预期不一致却恢复成功"
  else
    ok "目标与预期不一致时拒绝恢复"
  fi
  cp -a "$d/files/f" "$f"
  expect_ok "目标与预期一致时恢复成功" restore_transaction "$d" 1
  if [[ "$(cat "$f")" == "orig" ]]; then ok "恢复内容正确"; else no "恢复内容错误"; fi
}

# =========================================================
# T7 安装中途中断（部分已改并已记录指纹）→ 独立恢复
# =========================================================
test_interrupted_install_recover(){
  echo "T7 安装中途中断（部分已改并已记录指纹）→ --recover 安全恢复"
  lib
  stub_no_units
  local d="$WORK/t7" a="$WORK/t7-a" b="$WORK/t7-b" c="$WORK/t7-c"
  mkdir -p "$d/files"
  printf 'A-orig' > "$a"
  printf 'B-orig' > "$b"
  local preA preB
  preA="$(file_fingerprint "$a")"
  preB="$(file_fingerprint "$b")"
  cp -a "$a" "$d/files/fa"
  cp -a "$b" "$d/files/fb"
  : > "$d/files/fc.absent"
  printf 'A-installed' > "$a"
  printf 'C-installed' > "$c"
  local postA postC
  postA="$(file_fingerprint "$a")"
  postC="$(file_fingerprint "$c")"
  {
    printf '%s\t%s\tpost\n' "$a" "$postA"
    printf '%s\t%s\tpost\n' "$c" "$postC"
  } > "$d/hashes.tsv"
  {
    printf '%s\t%s\t%s\t%s\n' "$a" "files/fa" 1 "$preA"
    printf '%s\t%s\t%s\t%s\n' "$b" "files/fb" 1 "$preB"
    printf '%s\t%s\t%s\t%s\n' "$c" "files/fc" 0 "MISSING"
  } > "$d/manifest.tsv"
  write_state "$d" IN_PROGRESS 1 0
  expect_ok "清单完整，备份校验通过" verify_transaction "$d"
  expect_ok "安装后指纹与当前一致 → 目标校验通过" verify_recovery_targets "$d"
  expect_ok "新 Shell 独立恢复成功" restore_transaction "$d" 1
  if [[ "$(cat "$a")" == "A-orig" ]]; then ok "已改文件 a 还原为安装前内容"; else no "a 未还原（$(cat "$a" 2>/dev/null)）"; fi
  if [[ "$(cat "$b")" == "B-orig" ]]; then ok "未改文件 b 保持安装前内容"; else no "b 被破坏"; fi
  if [[ ! -e "$c" ]]; then ok "新建文件 c 被删除"; else no "c 未被删除"; fi
}

# =========================================================
# T8 中断后又遭外部修改 → 拒绝恢复且不覆盖
# =========================================================
test_interrupted_then_external_modify(){
  echo "T8 中断后又遭外部修改 → 拒绝恢复且不覆盖"
  lib
  stub_no_units
  local d="$WORK/t8" a="$WORK/t8-a"
  mkdir -p "$d/files"
  write_state "$d" ROLLBACK_IN_PROGRESS 1 0
  printf 'A-orig' > "$a"
  local preA; preA="$(file_fingerprint "$a")"
  cp -a "$a" "$d/files/fa"
  printf 'A-installed' > "$a"
  printf '%s\t%s\tpost\n' "$a" "$(file_fingerprint "$a")" > "$d/hashes.tsv"
  printf '%s\t%s\t%s\t%s\n' "$a" "files/fa" 1 "$preA" > "$d/manifest.tsv"
  expect_ok "外部修改前夹具通过前置校验" verify_transaction "$d"
  printf 'A-external' > "$a"
  expect "与已记录安装后指纹不一致 → 拒绝" verify_recovery_targets "$d"
  expect "恢复被拒绝" restore_transaction "$d" 1
  if [[ "$(cat "$a")" == "A-external" ]]; then ok "外部修改未被覆盖"; else no "外部修改被覆盖（$(cat "$a")）"; fi
}

# =========================================================
# T9 已改动但无 post 记录且与安装前不一致 → 保守拒绝
# =========================================================
test_unrecorded_change_conservative(){
  echo "T9 已改动但无指纹记录 → 归属未知，保守拒绝"
  lib
  stub_no_units
  local d="$WORK/t9" a="$WORK/t9-a" b="$WORK/t9-b"
  mkdir -p "$d/files"
  write_state "$d" ROLLBACK_IN_PROGRESS 1 0
  printf 'A-orig' > "$a"
  printf 'B-orig' > "$b"
  local preA preB
  preA="$(file_fingerprint "$a")"
  preB="$(file_fingerprint "$b")"
  cp -a "$a" "$d/files/fa"
  cp -a "$b" "$d/files/fb"
  printf 'A-changed' > "$a"
  # 无安装后记录用文件不存在表示，不制造损坏的空记录文件。
  {
    printf '%s\t%s\t%s\t%s\n' "$a" "files/fa" 1 "$preA"
    printf '%s\t%s\t%s\t%s\n' "$b" "files/fb" 1 "$preB"
  } > "$d/manifest.tsv"
  expect_ok "无 post 记录夹具通过完整性校验" verify_transaction "$d"
  expect "缺 post 且与安装前不一致 → 拒绝" verify_recovery_targets "$d"
  expect "恢复被拒绝" restore_transaction "$d" 1
  if [[ "$(cat "$a")" == "A-changed" ]]; then ok "归属未知文件未被覆盖"; else no "归属未知文件被覆盖"; fi
  printf '%s\t%s\t%s\t%s\n' "$b" "files/fb" 1 "$preB" > "$d/manifest.tsv"
  expect_ok "未改动文件（当前==安装前）判定为安全" verify_recovery_targets "$d"
}

# =========================================================
# T10 hashes_complete 标记持久化与校验
# =========================================================
test_hashes_complete_flag(){
  echo "T10 hashes_complete 标记持久化与校验"
  lib
  BACKUP_DIR="$WORK/t10"; mkdir -p "$BACKUP_DIR"
  TRANSACTION_ID="t10"; CREATED_AT="2026-01-01T00:00:00Z"
  MANIFEST_COMPLETE=1; HASHES_COMPLETE=0
  expect_ok "写入（清单完整、指纹未记录）成功" state_write IN_PROGRESS init
  if [[ "$(state_get "$BACKUP_DIR/state.txt" manifest_complete)" == "1" &&
        "$(state_get "$BACKUP_DIR/state.txt" hashes_complete)" == "0" ]]; then
    ok "两标记分别持久化：manifest_complete=1 / hashes_complete=0"
  else
    no "标记持久化不正确"
  fi
  HASHES_COMPLETE=1
  expect_ok "写入（指纹记录完整）成功" state_write IN_PROGRESS init
  if [[ "$(state_get "$BACKUP_DIR/state.txt" hashes_complete)" == "1" ]]; then
    ok "hashes_complete=1 已持久化"
  else
    no "hashes_complete=1 未持久化"
  fi
  sed '/^hashes_complete=/d' "$BACKUP_DIR/state.txt" > "$WORK/t10-missing"
  expect "缺少 hashes_complete 回读校验失败" state_verify_fields "$WORK/t10-missing"
  sed 's/^hashes_complete=.*/hashes_complete=7/' "$BACKUP_DIR/state.txt" > "$WORK/t10-bad"
  expect "hashes_complete 非法回读校验失败" state_verify_fields "$WORK/t10-bad"
}

# =========================================================
# T11 恢复中途被中断 → 新 Shell 断点续跑
# =========================================================
test_interrupt_mid_recovery(){
  echo "T11 恢复中途被中断 → 新 Shell 断点续跑"
  lib
  stub_no_units
  copy_func restore_one_file real_restore_one_file
  local d="$WORK/t11" a="$WORK/t11-a" b="$WORK/t11-b" c="$WORK/t11-c"
  mkdir -p "$d/files"
  printf 'A-orig' > "$a"; printf 'B-orig' > "$b"; printf 'C-orig' > "$c"
  local preA preB preC
  preA="$(file_fingerprint "$a")"; preB="$(file_fingerprint "$b")"; preC="$(file_fingerprint "$c")"
  cp -a "$a" "$d/files/fa"; cp -a "$b" "$d/files/fb"; cp -a "$c" "$d/files/fc"
  printf 'A-inst' > "$a"; printf 'B-inst' > "$b"; printf 'C-inst' > "$c"
  {
    printf '%s\t%s\tpost\n' "$a" "$(file_fingerprint "$a")"
    printf '%s\t%s\tpost\n' "$b" "$(file_fingerprint "$b")"
    printf '%s\t%s\tpost\n' "$c" "$(file_fingerprint "$c")"
  } > "$d/hashes.tsv"
  {
    printf '%s\t%s\t%s\t%s\n' "$c" "files/fc" 1 "$preC"
    printf '%s\t%s\t%s\t%s\n' "$b" "files/fb" 1 "$preB"
    printf '%s\t%s\t%s\t%s\n' "$a" "files/fa" 1 "$preA"
  } > "$d/manifest.tsv"
  write_state "$d" ROLLBACK_IN_PROGRESS 1 1
  local rc=0
  (
    n=0
    restore_one_file(){
      n=$((n+1))
      (( n >= 3 )) && exit 137
      real_restore_one_file "$@"
    }
    restore_transaction "$d" 1
  ) || rc=$?
  if (( rc != 0 )); then ok "首次运行被中断（返回非 0）"; else no "首次运行未被中断"; fi
  expect_journal_state "A 日志为 RESTORED" "$d" "$a" "RESTORED"
  expect_journal_state "B 日志为 RESTORED" "$d" "$b" "RESTORED"
  expect_journal_state "C 日志停留在 INTENT" "$d" "$c" "INTENT"
  expect_ok "中断后再次恢复成功" restore_transaction "$d" 1
  expect_content "A 恢复为安装前内容" "$a" "A-orig"
  expect_content "B 恢复为安装前内容" "$b" "B-orig"
  expect_content "C 恢复为安装前内容" "$c" "C-orig"
  if [[ "$(journal_state "$d" "$a")" == "RESTORED" &&
        "$(journal_state "$d" "$b")" == "RESTORED" &&
        "$(journal_state "$d" "$c")" == "RESTORED" ]]; then
    ok "全部文件日志为 RESTORED"
  else
    no "存在未标记 RESTORED 的文件"
  fi
  expect_file_present "备份 fa 保留" "$d/files/fa"
  expect_file_present "备份 fb 保留" "$d/files/fb"
  expect_file_present "备份 fc 保留" "$d/files/fc"
}

# =========================================================
# T12 原子替换前中断 → 安全重试
# =========================================================
test_interrupt_before_atomic_replace(){
  echo "T12 原子替换前中断 → 安全重试"
  lib
  stub_no_units
  local d="$WORK/t12" a="$WORK/t12-a"
  mkdir -p "$d/files"
  printf 'A-orig' > "$a"
  local pre; pre="$(file_fingerprint "$a")"
  cp -a "$a" "$d/files/fa"
  printf 'A-inst' > "$a"
  local post; post="$(file_fingerprint "$a")"
  printf '%s\t%s\tpost\n' "$a" "$post" > "$d/hashes.tsv"
  printf '%s\t%s\t%s\t%s\n' "$a" "files/fa" 1 "$pre" > "$d/manifest.tsv"
  write_state "$d" ROLLBACK_IN_PROGRESS 1 1
  TRANSACTION_ID="t12txn"
  expect_ok "写入 INTENT 日志" journal_set "$d" "$a" RESTORE INTENT "$pre" "$post"
  printf 'partial-temp' > "$(dirname "$a")/.node-panel-restore.$TRANSACTION_ID.6789"
  expect_ok "再次恢复安全重试成功" restore_transaction "$d" 1
  expect_content "目标恢复为安装前内容" "$a" "A-orig"
  expect_journal_state "日志更新为 RESTORED" "$d" "$a" "RESTORED"
  if compgen -G "$(dirname "$a")/.node-panel-restore.*" >/dev/null; then
    no "遗留临时文件未清理"
  else
    ok "遗留临时文件已清理"
  fi
  expect_file_present "备份保留" "$d/files/fa"
}

# =========================================================
# T13 原子替换后、RESTORED 写入前中断 → 补记 RESTORED
# =========================================================
test_interrupt_after_replace_before_journal(){
  echo "T13 原子替换后、RESTORED 写入前中断 → 补记 RESTORED"
  lib
  stub_no_units
  local d="$WORK/t13" a="$WORK/t13-a"
  mkdir -p "$d/files"
  printf 'A-orig' > "$a"
  local pre; pre="$(file_fingerprint "$a")"
  cp -a "$a" "$d/files/fa"
  printf 'A-inst' > "$a"
  local post; post="$(file_fingerprint "$a")"
  printf '%s\t%s\tpost\n' "$a" "$post" > "$d/hashes.tsv"
  printf '%s\t%s\t%s\t%s\n' "$a" "files/fa" 1 "$pre" > "$d/manifest.tsv"
  write_state "$d" ROLLBACK_IN_PROGRESS 1 1
  cp -a "$d/files/fa" "$a"
  expect_ok "写入 INTENT 日志" journal_set "$d" "$a" RESTORE INTENT "$pre" "$post"
  restore_one_file(){ return 9; }
  expect_ok "再次恢复成功（识别已完成替换）" restore_transaction "$d" 1
  expect_content "目标内容保持安装前" "$a" "A-orig"
  expect_journal_state "日志补记为 RESTORED" "$d" "$a" "RESTORED"
}

# =========================================================
# T14 外部修改 → 保留 A、不覆盖 B、不盲改 C
# =========================================================
test_external_modify_during_recovery(){
  echo "T14 恢复过程中外部修改 → 保留 A、不覆盖 B、不盲改 C"
  lib
  stub_no_units
  local d="$WORK/t14" a="$WORK/t14-a" b="$WORK/t14-b" c="$WORK/t14-c"
  mkdir -p "$d/files"
  printf 'A-orig' > "$a"; printf 'B-orig' > "$b"; printf 'C-orig' > "$c"
  local preA preB preC
  preA="$(file_fingerprint "$a")"; preB="$(file_fingerprint "$b")"; preC="$(file_fingerprint "$c")"
  cp -a "$a" "$d/files/fa"; cp -a "$b" "$d/files/fb"; cp -a "$c" "$d/files/fc"
  printf 'A-inst' > "$a"; printf 'B-inst' > "$b"; printf 'C-inst' > "$c"
  local postA postB postC
  postA="$(file_fingerprint "$a")"; postB="$(file_fingerprint "$b")"; postC="$(file_fingerprint "$c")"
  {
    printf '%s\t%s\tpost\n' "$a" "$postA"
    printf '%s\t%s\tpost\n' "$b" "$postB"
    printf '%s\t%s\tpost\n' "$c" "$postC"
  } > "$d/hashes.tsv"
  {
    printf '%s\t%s\t%s\t%s\n' "$c" "files/fc" 1 "$preC"
    printf '%s\t%s\t%s\t%s\n' "$b" "files/fb" 1 "$preB"
    printf '%s\t%s\t%s\t%s\n' "$a" "files/fa" 1 "$preA"
  } > "$d/manifest.tsv"
  write_state "$d" ROLLBACK_IN_PROGRESS 1 1
  cp -a "$d/files/fa" "$a"
  expect_ok "写入 A 为 RESTORED" journal_set "$d" "$a" RESTORE RESTORED "$preA" "$postA"
  printf 'B-external' > "$b"
  expect "再次恢复必须拒绝（B 外部修改）" restore_transaction "$d" 1
  expect_content "A 保持已恢复内容" "$a" "A-orig"
  expect_content "B 外部修改未被覆盖" "$b" "B-external"
  expect_content "C 未被盲目覆盖" "$c" "C-inst"
  expect_journal_state "A 日志保持 RESTORED" "$d" "$a" "RESTORED"
  expect_file_present "备份 fa 保留" "$d/files/fa"
  expect_file_present "备份 fb 保留" "$d/files/fb"
  expect_file_present "备份 fc 保留" "$d/files/fc"
}

# =========================================================
# T15 备份损坏 → 拒绝恢复且不覆盖目标
# =========================================================
test_corrupt_backup(){
  echo "T15 备份损坏 → 拒绝恢复且不覆盖目标"
  lib
  stub_no_units
  local d="$WORK/t15" a="$WORK/t15-a"
  mkdir -p "$d/files"
  printf 'A-orig' > "$a"
  local pre; pre="$(file_fingerprint "$a")"
  cp -a "$a" "$d/files/fa"
  printf 'A-inst' > "$a"
  printf '%s\t%s\tpost\n' "$a" "$(file_fingerprint "$a")" > "$d/hashes.tsv"
  printf '%s\t%s\t%s\t%s\n' "$a" "files/fa" 1 "$pre" > "$d/manifest.tsv"
  write_state "$d" ROLLBACK_IN_PROGRESS 1 1
  printf 'CORRUPT' >> "$d/files/fa"
  expect "备份损坏 → verify_transaction 拒绝" verify_transaction "$d"
  expect "备份损坏 → restore_transaction 拒绝" restore_transaction "$d" 1
  expect_content "目标未被覆盖（仍为安装后内容）" "$a" "A-inst"
  expect_file_present "损坏备份仍保留（未被删除）" "$d/files/fa"
}

# =========================================================
# T16 备份权限变化 → 拒绝恢复
# =========================================================
test_backup_perm_changed(){
  echo "T16 备份权限变化 → 拒绝恢复"
  lib
  stub_no_units
  local d="$WORK/t16" a="$WORK/t16-a"
  mkdir -p "$d/files"
  printf 'A-orig' > "$a"; chmod 600 "$a"
  local pre; pre="$(file_fingerprint "$a")"
  cp -a "$a" "$d/files/fa"
  printf 'A-inst' > "$a"
  printf '%s\t%s\tpost\n' "$a" "$(file_fingerprint "$a")" > "$d/hashes.tsv"
  printf '%s\t%s\t%s\t%s\n' "$a" "files/fa" 1 "$pre" > "$d/manifest.tsv"
  write_state "$d" ROLLBACK_IN_PROGRESS 1 1
  expect_ok "备份完好时 verify_transaction 通过" verify_transaction "$d"
  chmod 644 "$d/files/fa"
  expect "备份权限变化 → verify_transaction 拒绝" verify_transaction "$d"
  expect "备份权限变化 → restore_transaction 拒绝" restore_transaction "$d" 1
  expect_content "目标未被覆盖" "$a" "A-inst"
  expect_file_present "备份保留" "$d/files/fa"
}

# =========================================================
# T17 恢复日志写入失败 / fsync 失败 → 停止危险操作
# =========================================================
test_persistence_failure(){
  echo "T17 持久化失败 → 停止危险操作、不误报成功"
  lib
  stub_no_units
  local d="$WORK/t17" a="$WORK/t17-a"
  mkdir -p "$d/files"
  printf 'A-orig' > "$a"
  local pre; pre="$(file_fingerprint "$a")"
  cp -a "$a" "$d/files/fa"
  printf 'A-inst' > "$a"
  printf '%s\t%s\tpost\n' "$a" "$(file_fingerprint "$a")" > "$d/hashes.tsv"
  printf '%s\t%s\t%s\t%s\n' "$a" "files/fa" 1 "$pre" > "$d/manifest.tsv"
  write_state "$d" ROLLBACK_IN_PROGRESS 1 1
  journal_set(){ return 1; }
  expect "恢复日志写入失败 → 恢复中止（非 0）" restore_transaction "$d" 1
  expect_content "写入失败时目标未被修改" "$a" "A-inst"
  expect_journal_state "未产生错误的恢复日志" "$d" "$a" ""
  lib; stub_no_units
  fsync_path(){ return 1; }
  expect "fsync 失败 → 恢复中止（非 0）" restore_transaction "$d" 1
  expect_content "fsync 失败时目标未被修改" "$a" "A-inst"
  expect_journal_state "fsync 失败未留下半写日志" "$d" "$a" ""
}

# =========================================================
# T18 原本不存在文件移除中断 → 依证据判定续跑或拒绝
# =========================================================
test_nonexistent_removal_interrupt(){
  echo "T18 原本不存在文件移除中断 → 依证据判定续跑或拒绝"
  lib
  stub_no_units
  local d="$WORK/t18" c="$WORK/t18-c"
  mkdir -p "$d/files"
  : > "$d/files/fc.absent"
  printf 'C-inst' > "$c"
  local post; post="$(file_fingerprint "$c")"
  printf '%s\t%s\tpost\n' "$c" "$post" > "$d/hashes.tsv"
  printf '%s\t%s\t%s\t%s\n' "$c" "files/fc" 0 "MISSING" > "$d/manifest.tsv"
  write_state "$d" ROLLBACK_IN_PROGRESS 1 1
  rm -f "$c"
  expect_ok "写入 INTENT（含安装后指纹证据）" journal_set "$d" "$c" REMOVE INTENT "MISSING" "$post"
  expect_ok "有证据时续跑成功" restore_transaction "$d" 1
  expect_journal_state "日志标记 REMOVED" "$d" "$c" "REMOVED"
  expect_file_absent "目标确实不存在" "$c"
  lib; stub_no_units
  local d2="$WORK/t18b" c2="$WORK/t18b-c"
  mkdir -p "$d2/files"
  : > "$d2/files/fc.absent"
  printf '%s\t%s\t%s\t%s\n' "$c2" "files/fc" 0 "MISSING" > "$d2/manifest.tsv"
  write_state "$d2" ROLLBACK_IN_PROGRESS 1 0
  expect_ok "无 post 夹具通过完整性校验" verify_transaction "$d2"
  expect_ok "写入 INTENT（无安装后指纹证据）" journal_set "$d2" "$c2" REMOVE INTENT "MISSING" ""
  expect "无证据时拒绝续跑" restore_transaction "$d2" 1
  expect_journal_state "未错误标记 REMOVED" "$d2" "$c2" "INTENT"
  expect_file_absent "目标确实不存在（未误判为成功）" "$c2"
}

# =========================================================
# T19 临时文件清理仅限本事务
# =========================================================
test_temp_cleanup_scope(){
  echo "T19 临时文件清理仅限本事务"
  lib
  local d="$WORK/t19" td="$WORK/t19-d"; mkdir -p "$d/files" "$td"
  local a="$td/a"
  printf 'orig' > "$a"
  local pre; pre="$(file_fingerprint "$a")"
  cp -a "$a" "$d/files/fa"
  printf 'installed' > "$a"
  TRANSACTION_ID="t19txn"
  printf 'own-temp' > "$td/.node-panel-restore.$TRANSACTION_ID.111"
  printf 'other-txn' > "$td/.node-panel-restore.othertxn.222"
  printf 'other-prog' > "$td/.node-panel-restore.manual-notes"
  mkdir -p "$td/.node-panel-restore.$TRANSACTION_ID.dir"
  printf 'link-target' > "$WORK/t19-linktarget"
  ln -s "$WORK/t19-linktarget" "$td/.node-panel-restore.$TRANSACTION_ID.link"
  expect_ok "本事务恢复成功" restore_one_file "$d" "$a" "files/fa" "$pre"
  expect_content "目标恢复为安装前内容" "$a" "orig"
  expect_file_absent "本事务遗留临时文件已清理" "$td/.node-panel-restore.$TRANSACTION_ID.111"
  expect_file_present "其他事务临时文件保留" "$td/.node-panel-restore.othertxn.222"
  expect_file_present "其他程序同前缀文件保留" "$td/.node-panel-restore.manual-notes"
  expect_file_present "本事务前缀目录保留（不删目录）" "$td/.node-panel-restore.$TRANSACTION_ID.dir"
  expect_file_present "本事务前缀符号链接保留" "$td/.node-panel-restore.$TRANSACTION_ID.link"
  expect_file_present "符号链接目标未被删除" "$WORK/t19-linktarget"
}

# =========================================================
# T20 service 停止失败 → 立即中止文件恢复
# =========================================================
test_stop_failure_aborts(){
  echo "T20 systemctl stop 失败 → 中止文件恢复"
  lib
  local d="$WORK/t20" td="$WORK/t20-d"; mkdir -p "$d/files" "$td"
  local a="$td/panel.conf"
  printf 'orig' > "$a"
  local pre; pre="$(file_fingerprint "$a")"
  cp -a "$a" "$d/files/fa"
  printf 'installed' > "$a"
  printf '%s\t%s\tpost\n' "$a" "$(file_fingerprint "$a")" > "$d/hashes.tsv"
  {
    printf '%s\t%s\t%s\t%s\n' "$NODE_PANEL_UNIT" "files/u" 0 "MISSING"
    printf '%s\t%s\t%s\t%s\n' "$a" "files/fa" 1 "$pre"
  } > "$d/manifest.tsv"
  : > "$d/files/u.absent"
  write_state "$d" ROLLBACK_IN_PROGRESS 1 1
  expect_ok "停止失败前夹具合法" verify_transaction "$d"
  stub_panel_stop_fail
  expect "stop 失败 → 恢复中止（非 0）" restore_transaction "$d" 1
  expect_content "停止失败时目标文件未被恢复" "$a" "installed"
}

# =========================================================
# T21 停止后服务仍 active → 立即中止文件恢复
# =========================================================
test_still_active_aborts(){
  echo "T21 停止后仍 active → 中止文件恢复"
  lib
  local d="$WORK/t21" td="$WORK/t21-d"; mkdir -p "$d/files" "$td"
  local a="$td/panel.conf"
  printf 'orig' > "$a"
  local pre; pre="$(file_fingerprint "$a")"
  cp -a "$a" "$d/files/fa"
  printf 'installed' > "$a"
  printf '%s\t%s\tpost\n' "$a" "$(file_fingerprint "$a")" > "$d/hashes.tsv"
  {
    printf '%s\t%s\t%s\t%s\n' "$NODE_PANEL_UNIT" "files/u" 0 "MISSING"
    printf '%s\t%s\t%s\t%s\n' "$a" "files/fa" 1 "$pre"
  } > "$d/manifest.tsv"
  : > "$d/files/u.absent"
  write_state "$d" ROLLBACK_IN_PROGRESS 1 1
  expect_ok "active 校验前夹具合法" verify_transaction "$d"
  stub_panel_still_active
  expect "停止后仍 active → 恢复中止（非 0）" restore_transaction "$d" 1
  expect_content "仍 active 时目标文件未被恢复" "$a" "installed"
}

# =========================================================
# T22 systemctl disable 失败 → 必须明确中止
# =========================================================
test_disable_failure_aborts(){
  echo "T22 systemctl disable 失败 → 中止文件恢复"
  lib
  local d="$WORK/t22" td="$WORK/t22-d"; mkdir -p "$d/files" "$td"
  local a="$td/panel.conf"
  printf 'orig' > "$a"
  local pre; pre="$(file_fingerprint "$a")"
  cp -a "$a" "$d/files/fa"
  printf 'installed' > "$a"
  printf '%s\t%s\tpost\n' "$a" "$(file_fingerprint "$a")" > "$d/hashes.tsv"
  {
    printf '%s\t%s\t%s\t%s\n' "$NODE_PANEL_UNIT" "files/u" 0 "MISSING"
    printf '%s\t%s\t%s\t%s\n' "$a" "files/fa" 1 "$pre"
  } > "$d/manifest.tsv"
  : > "$d/files/u.absent"
  write_state "$d" ROLLBACK_IN_PROGRESS 1 1
  expect_ok "disable 失败前夹具合法" verify_transaction "$d"
  stub_panel_disable_fail
  expect "disable 失败 → 恢复中止（非 0）" restore_transaction "$d" 1
  expect_content "disable 失败时目标文件未被恢复" "$a" "installed"
}

# =========================================================
# T23 最终状态 ROLLED_BACK 写入失败 → 非 0 且明确报告
# =========================================================
test_rolled_back_write_failure(){
  echo "T23 ROLLED_BACK 写入失败 → 返回非 0 并明确报告"
  lib
  local d="$WORK/t23"; build_journal_case "$d"
  BACKUP_DIR="$d"
  stub_rolled_back_write_fail
  local out rc=0
  out="$(rollback_all 2>&1)" || rc=$?
  if (( rc != 0 )); then ok "ROLLED_BACK 写入失败 → 返回非 0"; else no "ROLLED_BACK 写入失败却返回 0"; fi
  if [[ "$out" == *"无法持久化 ROLLED_BACK"* ]]; then ok "明确报告 ROLLED_BACK 持久化失败"; else no "未明确报告 ROLLED_BACK 持久化失败"; fi
  if [[ "$out" == *"回滚完成"* ]]; then no "写入失败却仍报告回滚成功"; else ok "未误报回滚成功"; fi
}

# =========================================================
# T24 ROLLBACK_FAILED 自身写入失败 → 非 0 且明确报告
# =========================================================
test_rollback_failed_write_failure(){
  echo "T24 ROLLBACK_FAILED 写入失败 → 返回非 0 并明确报告"
  lib
  local d="$WORK/t24"; build_journal_case "$d"
  BACKUP_DIR="$d"
  stub_rollback_failed_write_fail
  local out rc=0
  out="$(rollback_all 2>&1)" || rc=$?
  if (( rc != 0 )); then ok "ROLLBACK_FAILED 写入失败 → 返回非 0"; else no "ROLLBACK_FAILED 写入失败却返回 0"; fi
  if [[ "$out" == *"无法持久化 ROLLBACK_FAILED"* ]]; then ok "明确报告 ROLLBACK_FAILED 持久化失败"; else no "未明确报告 ROLLBACK_FAILED 持久化失败"; fi
}

# =========================================================
# 构造最小合法事务，设置 JH_A / JH_PRE / JH_POST
# =========================================================
build_journal_case(){
  local d="$1"
  mkdir -p "$d/files"
  JH_A="$d/a"
  printf 'orig' > "$JH_A"
  JH_PRE="$(file_fingerprint "$JH_A")"
  cp -a "$JH_A" "$d/files/fa"
  printf 'installed' > "$JH_A"
  JH_POST="$(file_fingerprint "$JH_A")"
  printf '%s\t%s\tpost\n' "$JH_A" "$JH_POST" > "$d/hashes.tsv"
  printf '%s\t%s\t%s\t%s\n' "$JH_A" "files/fa" 1 "$JH_PRE" > "$d/manifest.tsv"
  write_state "$d" ROLLBACK_IN_PROGRESS 1 1
}

# =========================================================
# T25 journal 列数错误 → 拒绝恢复
# =========================================================
test_journal_bad_column_count(){
  echo "T25 journal 列数错误 → 拒绝恢复"
  lib
  stub_no_units
  local d="$WORK/t25"; build_journal_case "$d"
  printf '1\t%s\tRESTORE\tPENDING\t%s\t%s\n' "$JH_A" "$JH_PRE" "$JH_POST" > "$d/restore-journal.tsv"
  expect_ok "合法六列日志通过校验" verify_restore_journal "$d"
  printf '1\t%s\tRESTORE\tPENDING\t%s\n' "$JH_A" "$JH_PRE" > "$d/restore-journal.tsv"
  expect "journal 列数错误 → verify_restore_journal 拒绝" verify_restore_journal "$d"
  expect "journal 列数错误 → 恢复被拒绝" restore_transaction "$d" 1
  expect_content "拒绝恢复时目标未被改动" "$JH_A" "installed"
}

# =========================================================
# T26 journal 路径重复 → 拒绝恢复
# =========================================================
test_journal_duplicate_path(){
  echo "T26 journal 路径重复 → 拒绝恢复"
  lib
  local d="$WORK/t26"; build_journal_case "$d"
  {
    printf '1\t%s\tRESTORE\tPENDING\t%s\t%s\n' "$JH_A" "$JH_PRE" "$JH_POST"
    printf '1\t%s\tRESTORE\tPENDING\t%s\t%s\n' "$JH_A" "$JH_PRE" "$JH_POST"
  } > "$d/restore-journal.tsv"
  expect "journal 路径重复 → 拒绝" verify_restore_journal "$d"
}

# =========================================================
# T27 journal action 与 manifest 不一致 → 拒绝恢复
# =========================================================
test_journal_action_mismatch(){
  echo "T27 journal action 与 manifest 不一致 → 拒绝恢复"
  lib
  local d="$WORK/t27"; build_journal_case "$d"
  printf '1\t%s\tREMOVE\tPENDING\t%s\t%s\n' "$JH_A" "$JH_PRE" "$JH_POST" > "$d/restore-journal.tsv"
  expect "action 与清单不一致 → 拒绝" verify_restore_journal "$d"
}

# =========================================================
# T28 journal 字段非法或不一致 → 拒绝恢复
# =========================================================
test_journal_field_mismatch(){
  echo "T28 journal 字段非法或不一致 → 拒绝恢复"
  lib
  local d="$WORK/t28"; build_journal_case "$d"
  printf '2\t%s\tRESTORE\tPENDING\t%s\t%s\n' "$JH_A" "$JH_PRE" "$JH_POST" > "$d/restore-journal.tsv"
  expect "版本非法 → 拒绝" verify_restore_journal "$d"
  printf '1\t%s\tRESTORE\tBOGUS\t%s\t%s\n' "$JH_A" "$JH_PRE" "$JH_POST" > "$d/restore-journal.tsv"
  expect "状态非法 → 拒绝" verify_restore_journal "$d"
  printf '1\t%s\tFOO\tPENDING\t%s\t%s\n' "$JH_A" "$JH_PRE" "$JH_POST" > "$d/restore-journal.tsv"
  expect "动作非法 → 拒绝" verify_restore_journal "$d"
  printf '1\t%s\tRESTORE\tPENDING\t%s\t%s\n' "$JH_A" "FILE:deadbeef|perm=644|owner=root:root" "$JH_POST" > "$d/restore-journal.tsv"
  expect "pre 与清单指纹不一致 → 拒绝" verify_restore_journal "$d"
  printf '1\t%s\tRESTORE\tPENDING\t%s\t%s\n' "$JH_A" "$JH_PRE" "FILE:cafebabe|perm=644|owner=root:root" > "$d/restore-journal.tsv"
  expect "post 与 hashes.tsv 不一致 → 拒绝" verify_restore_journal "$d"
}

# =========================================================
# T29 manifest 重复路径与映射冲突 → 拒绝恢复
# =========================================================
test_manifest_duplicate_path(){
  echo "T29 manifest 重复路径与映射冲突 → 拒绝恢复"
  lib
  local d="$WORK/t29"; mkdir -p "$d/files"
  local a="$d/a"; printf 'orig' > "$a"
  local pre; pre="$(file_fingerprint "$a")"
  cp -a "$a" "$d/files/fa"; cp -a "$a" "$d/files/fa2"
  {
    printf '%s\t%s\t%s\t%s\n' "$a" "files/fa" 1 "$pre"
    printf '%s\t%s\t%s\t%s\n' "$a" "files/fa2" 1 "$pre"
  } > "$d/manifest.tsv"
  write_state "$d" ROLLBACK_IN_PROGRESS 1 0
  expect "manifest 重复 src → verify_transaction 拒绝" verify_transaction "$d"
  lib
  local d2="$WORK/t29b"; mkdir -p "$d2/files"
  local x="$d2/x"
  printf 'same' > "$x"
  cp -a "$x" "$d2/y"
  cp -a "$x" "$d2/files/bak"
  local fpx; fpx="$(file_fingerprint "$x")"
  {
    printf '%s\t%s\t%s\t%s\n' "$x" "files/bak" 1 "$fpx"
    printf '%s\t%s\t%s\t%s\n' "$d2/y" "files/bak" 1 "$fpx"
  } > "$d2/manifest.tsv"
  write_state "$d2" ROLLBACK_IN_PROGRESS 1 0
  expect "备份路径映射冲突（rel 重复）→ 拒绝" verify_transaction "$d2"
}

# =========================================================
# T30 hashes.tsv 矛盾 / 列数错误 → 拒绝；多条 post → 接受
# =========================================================
test_hashes_contradiction(){
  echo "T30 hashes.tsv 矛盾记录 → 拒绝恢复"
  lib
  local d="$WORK/t30"; mkdir -p "$d/files"
  local a="$d/a"; printf 'orig' > "$a"
  local pre; pre="$(file_fingerprint "$a")"
  cp -a "$a" "$d/files/fa"
  printf 'installed' > "$a"
  local post; post="$(file_fingerprint "$a")"
  printf '%s\t%s\t%s\t%s\n' "$a" "files/fa" 1 "$pre" > "$d/manifest.tsv"
  write_state "$d" ROLLBACK_IN_PROGRESS 1 1
  {
    printf '%s\t%s\tpost\n' "$a" "$post"
    printf '%s\t%s\tpre\n' "$a" "FILE:deadbeef|perm=644|owner=root:root|mtime=1.0"
  } > "$d/hashes.tsv"
  expect "同一路径矛盾记录 → 拒绝" verify_transaction "$d"
  printf '%s\t%s\n' "$a" "$post" > "$d/hashes.tsv"
  expect "hashes.tsv 字段数量错误 → 拒绝" verify_transaction "$d"
  {
    printf '%s\t%s\tpost\n' "$a" "FILE:1111|perm=644|owner=root:root|mtime=1.0"
    printf '%s\t%s\tpost\n' "$a" "$post"
  } > "$d/hashes.tsv"
  expect_ok "同一路径多条 post → 接受（以最后一条为准）" verify_transaction "$d"
}

# =========================================================
# T31 mtime 校验与旧格式兼容
# =========================================================
test_mtime_change(){
  echo "T31 mtime 校验与旧格式兼容"
  lib
  local old="FILE:abc|perm=644|owner=root:root"
  local new="FILE:abc|perm=644|owner=root:root|mtime=123.000000000"
  local newer="FILE:abc|perm=644|owner=root:root|mtime=999.000000000"
  expect_ok "旧格式（无 mtime）与含 mtime 指纹兼容相等" fp_equal "$old" "$new"
  expect "mtime 不同 → 判定不相等" fp_equal "$new" "$newer"
  local d="$WORK/t31" a="$WORK/t31-a"
  mkdir -p "$d/files"
  printf 'content' > "$a"
  cp -a "$a" "$d/files/fa"
  local fp; fp="$(file_fingerprint "$a")"
  printf '%s\t%s\tpost\n' "$a" "$fp" > "$d/hashes.tsv"
  printf '%s\t%s\t%s\t%s\n' "$a" "files/fa" 1 "$fp" > "$d/manifest.tsv"
  expect_ok "指纹一致时校验通过" verify_recovery_targets "$d"
  touch -d '2001-01-01 00:00:00' "$a"
  expect "仅 mtime 变化 → 拒绝恢复" verify_recovery_targets "$d"
}

# =========================================================
# T32 特殊文件类型安全拒绝
# =========================================================
test_special_file_rejected(){
  echo "T32 特殊文件类型安全拒绝"
  lib
  local d="$WORK/t32" td="$WORK/t32-d"; mkdir -p "$d/files" "$td"
  local fifo="$td/pipe"
  if ! mkfifo "$fifo" 2>/dev/null; then echo "  跳过（无法创建 FIFO）"; return; fi
  local fp; fp="$(file_fingerprint "$fifo")"
  if [[ "$fp" == SPECIAL:* ]]; then ok "特殊文件类型指纹标注 SPECIAL（不误判 MISSING）"; else no "特殊文件类型指纹错误（$fp）"; fi
  expect "restore_one_file 拒绝特殊文件类型" restore_one_file "$d" "$fifo" "files/pipe" "$fp"
  if [[ -p "$fifo" ]]; then ok "特殊文件未被误删"; else no "特殊文件被误删/修改"; fi
}

# T33–T43 复用本文件断言、合法事务夹具与提取出的 install.sh 函数库。
# shellcheck disable=SC1091
source "$ROOT/tests/recovery-boundary-tests.sh" || exit 2

# 每个用例放在独立子 Shell，防止 cp/restore_one_file/state_write 等 mock 泄漏。
# 意外退出也计入失败，不会因为缺少结果文件而误报通过。
run_test(){
  local result="$WORK/result-$1" p f rc=0
  (
    PASS=0; FAIL=0
    "$1"
    printf '%s %s\n' "$PASS" "$FAIL" > "$result"
  ) || rc=$?
  if [[ -f "$result" ]]; then
    read -r p f < "$result"
    PASS=$((PASS+p)); FAIL=$((FAIL+f))
  else
    no "$1 意外退出（rc=$rc）"
  fi
}

echo "=================================================="
echo " install.sh 恢复安全机制测试"
echo "=================================================="
run_test test_state_write_validation
run_test test_manifest_complete
run_test test_service_unit_missing
run_test test_perm_change
run_test test_owner_change
run_test test_symlink_change
run_test test_change_between_check_and_restore
run_test test_restore_ok_control
run_test test_interrupted_install_recover
run_test test_interrupted_then_external_modify
run_test test_unrecorded_change_conservative
run_test test_hashes_complete_flag
run_test test_interrupt_mid_recovery
run_test test_interrupt_before_atomic_replace
run_test test_interrupt_after_replace_before_journal
run_test test_external_modify_during_recovery
run_test test_corrupt_backup
run_test test_backup_perm_changed
run_test test_persistence_failure
run_test test_nonexistent_removal_interrupt
run_test test_temp_cleanup_scope
run_test test_stop_failure_aborts
run_test test_still_active_aborts
run_test test_disable_failure_aborts
run_test test_rolled_back_write_failure
run_test test_rollback_failed_write_failure
run_test test_journal_bad_column_count
run_test test_journal_duplicate_path
run_test test_journal_action_mismatch
run_test test_journal_field_mismatch
run_test test_manifest_duplicate_path
run_test test_hashes_contradiction
run_test test_mtime_change
run_test test_special_file_rejected
run_test test_boundary_missing_units_allowed
run_test test_boundary_missing_units_rejected
run_test test_boundary_prerequisite_failures
run_test test_boundary_rollback_status_failure
run_test test_boundary_metadata_corrupt
run_test test_boundary_state_corrupt
run_test test_boundary_tsv_corrupt
run_test test_boundary_partial_service_failure
run_test test_boundary_journal_and_post
echo "--------------------------------------------------"
printf '结果：\033[32m%d 通过\033[0m，\033[31m%d 失败\033[0m\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
