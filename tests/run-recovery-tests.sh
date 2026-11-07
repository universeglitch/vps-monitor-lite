#!/usr/bin/env bash
# install-dual.sh 安装事务恢复安全机制测试
#
# 用法：bash tests/run-recovery-tests.sh
#
# 测试通过“提取函数库（脚本顶层执行之前的部分）+ 打桩 systemd”的方式，
# 在隔离的临时目录中直接调用恢复/校验函数，不触碰真实系统状态。
#
# shellcheck disable=SC2034  # 部分变量由被 source 的函数库消费
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/install-dual.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
ok(){ PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
no(){ FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
expect(){ # expect <描述> <命令...>  —— 期望失败(非0)
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then no "$desc（本应失败却成功）"; else ok "$desc"; fi
}
expect_ok(){ # expect_ok <描述> <命令...> —— 期望成功(0)
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$desc"; else no "$desc（本应成功却失败）"; fi
}

# ---- 提取函数库（顶层 case "$MODE" 之前的全部内容）----
# shellcheck disable=SC2016  # 此处需要字面量 "$MODE"
LINE="$(grep -nF 'case "$MODE" in' "$SRC" | head -1 | cut -d: -f1)"
[[ -n "$LINE" ]] || { echo "无法定位函数库边界"; exit 2; }
sed -n "1,$((LINE-1))p" "$SRC" > "$WORK/lib.sh"

# 载入函数库（清空位置参数，避免触发参数解析分支）
# shellcheck disable=SC1091
lib(){ set --; source "$WORK/lib.sh" >/dev/null 2>&1; set +e; }

write_state(){ # write_state <dir> <status> <manifest_complete> [hashes_complete]
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

# 打桩：默认无 systemd 单元、所有 systemctl 调用成功
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
  printf '%s\t%s\n' "$f" "$fp" > "$d/hashes.tsv"
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
  printf '%s\t%s\n' "$f" "$fp" > "$d/hashes.tsv"
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
  printf '%s\t%s\n' "$sl" "$fp" > "$d/hashes.tsv"
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
  printf 'victim-orig' > "$victim"
  printf 'other-orig'  > "$other"
  cp -a "$victim" "$d/files/v"
  cp -a "$other"  "$d/files/o"
  {
    printf '%s\t%s\n' "$victim" "$(file_fingerprint "$victim")"
    printf '%s\t%s\n' "$other"  "$(file_fingerprint "$other")"
  } > "$d/hashes.tsv"
  {
    printf '%s\t%s\t%s\t%s\n' "$victim" "files/v" 1 "$(file_fingerprint "$victim")"
    printf '%s\t%s\t%s\t%s\n' "$other"  "files/o" 1 "$(file_fingerprint "$other")"
  } > "$d/manifest.tsv"

  # 在第一次 cp 时（此时初次校验已通过）修改另一个目标文件，模拟恢复过程中的外部修改
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
  printf 'orig' > "$f"
  cp -a "$f" "$d/files/f"
  local fp; fp="$(file_fingerprint "$f")"
  printf '%s\t%s\n' "$f" "$fp" > "$d/hashes.tsv"
  printf '%s\t%s\t%s\t%s\n' "$f" "files/f" 1 "$fp" > "$d/manifest.tsv"

  printf 'changed-by-install' > "$f"
  if restore_transaction "$d" 1 >/dev/null 2>&1; then
    # 注意：恢复前已验证目标与预期一致，但此处目标已被改动，应当失败
    no "目标与预期不一致却恢复成功"
  else
    ok "目标与预期不一致时拒绝恢复"
  fi

  # 目标与预期一致时应当恢复成功
  printf 'orig' > "$f"
  expect_ok "目标与预期一致时恢复成功" restore_transaction "$d" 1
  if [[ "$(cat "$f")" == "orig" ]]; then ok "恢复内容正确"; else no "恢复内容错误"; fi
}

# =========================================================
# T7 安装中途修改部分受管文件后立即中断 → 新 Shell --recover 安全恢复
#   a：安装前已存在，安装中被改写，且已记录安装后指纹（post 与当前一致）
#   b：安装前已存在，安装中未改动（无 post 记录，当前 == 安装前）
#   c：安装前不存在，安装中新建，且已记录安装后指纹
#   模拟安装中途（hashes_complete=0）被 SIGKILL，新 Shell 独立恢复应成功。
# =========================================================
test_interrupted_install_recover(){
  echo "T7 安装中途中断（部分已改并已记录指纹）→ --recover 安全恢复"
  lib
  stub_no_units
  local d="$WORK/t7" a="$WORK/t7-a" b="$WORK/t7-b" c="$WORK/t7-c"
  mkdir -p "$d/files"

  # 安装前状态
  printf 'A-orig' > "$a"
  printf 'B-orig' > "$b"
  local preA preB
  preA="$(file_fingerprint "$a")"
  preB="$(file_fingerprint "$b")"

  # 备份（安装前快照）
  cp -a "$a" "$d/files/fa"
  cp -a "$b" "$d/files/fb"
  : > "$d/files/fc.absent"

  # 安装过程：a 被改写、c 被新建（均按设计在写入后立即记录 post 指纹）
  printf 'A-installed' > "$a"
  printf 'C-installed' > "$c"
  local postA postC
  postA="$(file_fingerprint "$a")"
  postC="$(file_fingerprint "$c")"
  {
    printf '%s\t%s\tpost\n' "$a" "$postA"
    printf '%s\t%s\tpost\n' "$c" "$postC"
  } > "$d/hashes.tsv"

  # b 未改动 → 无 post 记录
  {
    printf '%s\t%s\t%s\t%s\n' "$a" "files/fa" 1 "$preA"
    printf '%s\t%s\t%s\t%s\n' "$b" "files/fb" 1 "$preB"
    printf '%s\t%s\t%s\t%s\n' "$c" "files/fc" 0 "MISSING"
  } > "$d/manifest.tsv"

  write_state "$d" IN_PROGRESS 1 0   # 清单完整，但指纹未记录完整（中断）

  expect_ok "清单完整，备份校验通过" verify_transaction "$d"
  expect_ok "安装后指纹与当前一致 → 目标校验通过" verify_recovery_targets "$d"
  expect_ok "新 Shell 独立恢复成功" restore_transaction "$d" 1

  if [[ "$(cat "$a")" == "A-orig" ]]; then ok "已改文件 a 还原为安装前内容"; else no "a 未还原（$(cat "$a" 2>/dev/null)）"; fi
  if [[ "$(cat "$b")" == "B-orig" ]]; then ok "未改文件 b 保持安装前内容"; else no "b 被破坏"; fi
  if [[ ! -e "$c" ]]; then ok "新建文件 c 被删除"; else no "c 未被删除"; fi
}

# =========================================================
# T8 安装中断后，某文件在记录 post 指纹之后又被外部修改 → 拒绝恢复且不覆盖
# =========================================================
test_interrupted_then_external_modify(){
  echo "T8 中断后又遭外部修改 → 拒绝恢复且不覆盖"
  lib
  stub_no_units
  local d="$WORK/t8" a="$WORK/t8-a"
  mkdir -p "$d/files"
  printf 'A-orig' > "$a"
  local preA; preA="$(file_fingerprint "$a")"
  cp -a "$a" "$d/files/fa"

  # 安装改写 a，并记录 post
  printf 'A-installed' > "$a"
  printf '%s\t%s\tpost\n' "$a" "$(file_fingerprint "$a")" > "$d/hashes.tsv"
  printf '%s\t%s\t%s\t%s\n' "$a" "files/fa" 1 "$preA" > "$d/manifest.tsv"

  # 外部修改（与已记录的 post 不一致）
  printf 'A-external' > "$a"

  expect "与已记录安装后指纹不一致 → 拒绝" verify_recovery_targets "$d"
  expect "恢复被拒绝" restore_transaction "$d" 1
  if [[ "$(cat "$a")" == "A-external" ]]; then ok "外部修改未被覆盖"; else no "外部修改被覆盖（$(cat "$a")）"; fi
}

# =========================================================
# T9 已改动但无 post 记录且与安装前不一致 → 归属未知，保守拒绝（不猜测）
#   同时验证未改动文件（当前 == 安装前）仍可判定为安全。
# =========================================================
test_unrecorded_change_conservative(){
  echo "T9 已改动但无指纹记录 → 归属未知，保守拒绝"
  lib
  stub_no_units
  local d="$WORK/t9" a="$WORK/t9-a" b="$WORK/t9-b"
  mkdir -p "$d/files"
  printf 'A-orig' > "$a"
  printf 'B-orig' > "$b"
  local preA preB
  preA="$(file_fingerprint "$a")"
  preB="$(file_fingerprint "$b")"
  cp -a "$a" "$d/files/fa"
  cp -a "$b" "$d/files/fb"

  # a 被改动但未记录 post（模拟写入后、记录前被强杀）
  printf 'A-changed' > "$a"
  # b 未改动
  : > "$d/hashes.tsv"

  {
    printf '%s\t%s\t%s\t%s\n' "$a" "files/fa" 1 "$preA"
    printf '%s\t%s\t%s\t%s\n' "$b" "files/fb" 1 "$preB"
  } > "$d/manifest.tsv"

  expect "缺 post 且与安装前不一致 → 拒绝" verify_recovery_targets "$d"
  expect "恢复被拒绝" restore_transaction "$d" 1
  if [[ "$(cat "$a")" == "A-changed" ]]; then ok "归属未知文件未被覆盖"; else no "归属未知文件被覆盖"; fi

  # 仅保留未改动文件时，应判定为安全（当前 == 安装前）
  printf '%s\t%s\t%s\t%s\n' "$b" "files/fb" 1 "$preB" > "$d/manifest.tsv"
  expect_ok "未改动文件（当前==安装前）判定为安全" verify_recovery_targets "$d"
}

# =========================================================
# T10 state_write 持久化 / 校验 hashes_complete（与 manifest_complete 区分）
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

  # hashes_complete 缺失 / 非法 → 回读校验必须失败
  sed '/^hashes_complete=/d' "$BACKUP_DIR/state.txt" > "$WORK/t10-missing"
  expect "缺少 hashes_complete 回读校验失败" state_verify_fields "$WORK/t10-missing"
  sed 's/^hashes_complete=.*/hashes_complete=7/' "$BACKUP_DIR/state.txt" > "$WORK/t10-bad"
  expect "hashes_complete 非法回读校验失败" state_verify_fields "$WORK/t10-bad"
}

# =========================================================
echo "=================================================="
echo " install-dual.sh 恢复安全机制测试"
echo "=================================================="
test_state_write_validation
test_manifest_complete
test_service_unit_missing
test_perm_change
test_owner_change
test_symlink_change
test_change_between_check_and_restore
test_restore_ok_control
test_interrupted_install_recover
test_interrupted_then_external_modify
test_unrecorded_change_conservative
test_hashes_complete_flag
echo "--------------------------------------------------"
printf '结果：\033[32m%d 通过\033[0m，\033[31m%d 失败\033[0m\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
