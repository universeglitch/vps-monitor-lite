#!/usr/bin/env bash
# T33–T43: sourced by run-recovery-tests.sh, using its assertions and extracted library.
# Execution status at delivery: NOT RUN. No real systemctl/account commands are used.
# shellcheck disable=SC2034,SC2317

# All managed paths are temporary. Only the ownership lookup maps the hardcoded
# Xray unit name to a temporary unit; actual restore paths remain under WORK.
boundary_fixture(){
  local d="$1"
  build_journal_case "$d"
  NODE_PANEL_UNIT="$d/panel.service"
  BOUNDARY_XRAY_UNIT="$d/xray.service"
  printf 'panel-before' > "$NODE_PANEL_UNIT"
  printf 'xray-before' > "$BOUNDARY_XRAY_UNIT"
  local p xp
  p="$(file_fingerprint "$NODE_PANEL_UNIT")"
  xp="$(file_fingerprint "$BOUNDARY_XRAY_UNIT")"
  cp -a "$NODE_PANEL_UNIT" "$d/files/panel-unit"
  cp -a "$BOUNDARY_XRAY_UNIT" "$d/files/xray-unit"
  printf '%s\tfiles/panel-unit\t1\t%s\n' "$NODE_PANEL_UNIT" "$p" >> "$d/manifest.tsv"
  printf '%s\tfiles/xray-unit\t1\t%s\n' "$BOUNDARY_XRAY_UNIT" "$xp" >> "$d/manifest.tsv"
  printf 'panel-installed' > "$NODE_PANEL_UNIT"
  printf 'xray-installed' > "$BOUNDARY_XRAY_UNIT"
  printf '%s\t%s\tpost\n' "$NODE_PANEL_UNIT" "$(file_fingerprint "$NODE_PANEL_UNIT")" >> "$d/hashes.tsv"
  printf '%s\t%s\tpost\n' "$BOUNDARY_XRAY_UNIT" "$(file_fingerprint "$BOUNDARY_XRAY_UNIT")" >> "$d/hashes.tsv"
  copy_func manifest_has boundary_real_manifest_has
  manifest_has(){
    local path="$2"
    [[ "$path" != /etc/systemd/system/xray.service ]] || path="$BOUNDARY_XRAY_UNIT"
    boundary_real_manifest_has "$1" "$path"
  }
  CALLS="$d/calls"
  : > "$CALLS"
  MISSING_UNIT=""; FAIL_RELOAD=0; FAIL_START=""; FAIL_ENABLE=""
  declare -gA MOCK_ACTIVE=() MOCK_ENABLED=()
  svc_exists(){ printf 'exists %s\n' "$1" >> "$CALLS"; [[ "$1" != "$MISSING_UNIT" ]]; }
  svc_active(){ printf 'active %s\n' "$1" >> "$CALLS"; [[ "${MOCK_ACTIVE[$1]:-0}" == 1 ]]; }
  svc_enabled(){ printf 'enabled %s\n' "$1" >> "$CALLS"; [[ "${MOCK_ENABLED[$1]:-0}" == 1 ]]; }
  systemctl(){
    printf 'systemctl %s\n' "$*" >> "$CALLS"
    case "$1" in
      daemon-reload) [[ "$FAIL_RELOAD" == 0 ]] ;;
      stop) MOCK_ACTIVE["$2"]=0 ;;
      start)
        [[ "$2" != "$FAIL_START" ]] || return 1
        MOCK_ACTIVE["$2"]=1
        printf 'started %s\n' "$2" >> "$CALLS" ;;
      enable)
        [[ "$2" != "$FAIL_ENABLE" ]] || return 1
        MOCK_ENABLED["$2"]=1 ;;
      disable) MOCK_ENABLED["$2"]=0 ;;
      *) return 97 ;;
    esac
  }
  MOCK_USER=1
  getent(){
    printf 'getent %s\n' "$*" >> "$CALLS"
    case "$1" in passwd) [[ "$MOCK_USER" == 1 ]] ;; group) return 0 ;; *) return 97 ;; esac
  }
  ps(){ printf 'ps %s\n' "$*" >> "$CALLS"; return 0; }
  userdel(){ printf 'userdel %s\n' "$*" >> "$CALLS"; MOCK_USER=0; }
  groupdel(){ printf 'groupdel %s\n' "$*" >> "$CALLS"; return 0; }
  # Avoid machine-wide sync in tests; persistent failure paths are injected explicitly.
  fsync_path(){ return 0; }
  BACKUP_DIR="$d"
  TRANSACTION_ID="$(basename "$d")"
  CREATED_AT="2026-01-01T00:00:00Z"
  MANIFEST_COMPLETE=1; HASHES_COMPLETE=1
  XRAY_WAS_ACTIVE=0; XRAY_WAS_ENABLED=0
  NODEPANEL_WAS_ACTIVE=0; NODEPANEL_WAS_ENABLED=0
}

boundary_set_field(){
  local f="$1" key="$2" value="$3"
  awk -v k="$key" -v v="$value" 'index($0,k "=")==1 {$0=k "=" v} {print}' "$f" > "$f.edit"
  mv -- "$f.edit" "$f"
}

boundary_no_start(){
  if grep -q '^systemctl start ' "$CALLS"; then no "$1"; else ok "$1"; fi
}

boundary_no_effects(){
  expect_content "$1: no service/account calls" "$CALLS" ""
  expect_content "$1: data unchanged" "$JH_A" installed
  expect_content "$1: Panel unit unchanged" "$NODE_PANEL_UNIT" panel-installed
  expect_content "$1: Xray unit unchanged" "$BOUNDARY_XRAY_UNIT" xray-installed
  expect_file_absent "$1: no restore journal" "$BACKUP_DIR/restore-journal.tsv"
}

# T33/T34: exercise full transaction, not only verify_service_state.
test_boundary_missing_units_allowed(){
  echo 'T33/T34 missing Panel/Xray unit with inactive+disabled: full rollback allowed'
  local unit i=0 d
  for unit in node-panel.service xray.service; do
    lib
    i=$((i+1)); d="$WORK/t33-$i"
    boundary_fixture "$d"
    MISSING_UNIT="$unit"
    expect_ok "$unit missing inactive/disabled: complete rollback" rollback_all
    expect_content 'data restored' "$JH_A" orig
    expect_content 'Panel unit restored' "$NODE_PANEL_UNIT" panel-before
    expect_content 'Xray unit restored' "$BOUNDARY_XRAY_UNIT" xray-before
    expect_ok 'ROLLED_BACK persisted' test "$(state_get "$d/state.txt" status)" = ROLLED_BACK
    boundary_no_start 'inactive services never started'
    expect_file_present 'backup retained' "$d/files/fa"
  done
}

# T35/T36: active and enabled independently, for both missing units.
test_boundary_missing_units_rejected(){
  echo 'T35/T36 missing unit with prior active or enabled: reject'
  local unit key prefix i=0 d
  for unit in node-panel.service xray.service; do
    prefix=xray; [[ "$unit" != node-panel.service ]] || prefix=node_panel
    for key in "${prefix}_was_active" "${prefix}_was_enabled"; do
      lib
      i=$((i+1)); d="$WORK/t35-$i"
      boundary_fixture "$d"
      MISSING_UNIT="$unit"
      boundary_set_field "$d/state.txt" "$key" 1
      expect_ok "$key fixture otherwise valid" verify_transaction "$d"
      expect "$key + missing unit: refuse" restore_transaction "$d"
      expect_content 'data unchanged' "$JH_A" installed
      expect_content 'Panel target unchanged' "$NODE_PANEL_UNIT" panel-installed
      expect_content 'Xray target unchanged' "$BOUNDARY_XRAY_UNIT" xray-installed
      expect_file_absent 'no file journal' "$d/restore-journal.tsv"
      boundary_no_start 'no start after missing-unit rejection'
    done
  done
}

# T37: file copy failure, daemon-reload failure, and enable restoration failure.
# Active=1 for BOTH services so a missing guard would actually attempt start.
test_boundary_prerequisite_failures(){
  echo 'T37a/b/c prerequisites fail: never systemctl start'
  local mode i=0 d
  for mode in file reload enabled; do
    lib
    i=$((i+1)); d="$WORK/t37-$i"
    boundary_fixture "$d"
    boundary_set_field "$d/state.txt" xray_was_active 1
    boundary_set_field "$d/state.txt" node_panel_was_active 1
    boundary_set_field "$d/state.txt" xray_was_enabled 1
    boundary_set_field "$d/state.txt" node_panel_was_enabled 1
    expect_ok "$mode fixture valid" verify_transaction "$d"
    case "$mode" in
      file)
        restore_one_file(){ printf 'restore_one_file failed\n' >> "$CALLS"; return 1; } ;;
      reload) FAIL_RELOAD=1 ;;
      enabled) FAIL_ENABLE=node-panel.service ;;
    esac
    expect "$mode failure returns nonzero" restore_transaction "$d"
    boundary_no_start "$mode failure blocks ALL starts"
    case "$mode" in
      file)
        expect_ok 'reached injected file failure' grep -q '^restore_one_file failed$' "$CALLS"
        expect_content 'first target unchanged' "$BOUNDARY_XRAY_UNIT" xray-installed ;;
      reload)
        expect_ok 'reached daemon-reload' grep -q '^systemctl daemon-reload$' "$CALLS"
        expect_content 'file restored before reload failure' "$JH_A" orig ;;
      enabled)
        expect_ok 'Xray enable restoration attempted first' grep -q '^systemctl enable xray.service$' "$CALLS"
        expect_ok 'reached Panel enable failure' grep -q '^systemctl enable node-panel.service$' "$CALLS" ;;
    esac
  done
}

# T38: test both entry paths with actual state_write failing before rename.
test_boundary_rollback_status_failure(){
  echo 'T38 ROLLBACK_IN_PROGRESS persistence fails: no target changes'
  local entry i=0 d before bp bx
  for entry in rollback recover; do
    lib
    i=$((i+1)); d="$WORK/t38-$i"
    boundary_fixture "$d"
    before="$(file_fingerprint "$JH_A")"
    bp="$(file_fingerprint "$NODE_PANEL_UNIT")"
    bx="$(file_fingerprint "$BOUNDARY_XRAY_UNIT")"
    fsync_path(){
      if [[ "$1" == *'/.state.txt.tmp.'* ]]; then
        printf 'injected state fsync failure\n' > "$BACKUP_DIR/state-failure-reached"
        return 1
      fi
      return 0
    }
    if [[ "$entry" == rollback ]]; then
      expect 'automatic rollback state write fails' rollback_all
    else
      BACKUP_PREFIX="$WORK/t38-$i"
      # die exits: isolate interactive entry while keeping file-backed evidence.
      expect 'standalone recover state write fails' bash_boundary_recover
    fi
    expect_file_present 'state persistence failure actually reached' "$d/state-failure-reached"
    expect_ok "$entry data fingerprint preserved" fp_equal "$before" "$(file_fingerprint "$JH_A")"
    expect_ok "$entry Panel fingerprint preserved" fp_equal "$bp" "$(file_fingerprint "$NODE_PANEL_UNIT")"
    expect_ok "$entry Xray fingerprint preserved" fp_equal "$bx" "$(file_fingerprint "$BOUNDARY_XRAY_UNIT")"
    boundary_no_effects "$entry failed state write"
    expect_ok 'original status retained' test "$(state_get "$d/state.txt" status)" = ROLLBACK_IN_PROGRESS
  done
}

bash_boundary_recover(){ ( recover_mode <<< $'1\nyes\n' ); }

# T39: each metadata key: missing, duplicate same value, empty, invalid.
# Also missing/empty/truncated file. Verify absence of *all* service/account calls.
test_boundary_metadata_corrupt(){
  echo 'T39 meta.txt corruption blocks every recovery action'
  local key variant i=0 d
  for key in xray_user_existed xray_group_existed; do
    for variant in missing duplicate empty invalid; do
      lib
      i=$((i+1)); d="$WORK/t39-$i"
      boundary_fixture "$d"
      expect_ok 'valid metadata fixture' verify_transaction "$d"
      case "$variant" in
        missing) sed "/^${key}=/d" "$d/meta.txt" > "$d/meta.edit"; mv "$d/meta.edit" "$d/meta.txt" ;;
        duplicate) printf '%s=0\n' "$key" >> "$d/meta.txt" ;;
        empty) boundary_set_field "$d/meta.txt" "$key" '' ;;
        invalid) boundary_set_field "$d/meta.txt" "$key" 2 ;;
      esac
      expect "$key/$variant direct restore refuses" restore_transaction "$d"
      boundary_no_effects "$key/$variant"
      expect "$key/$variant automatic rollback refuses" rollback_all
      boundary_no_effects "$key/$variant rollback"
    done
  done
  for variant in absent empty truncated; do
    lib
    i=$((i+1)); d="$WORK/t39-$i"
    boundary_fixture "$d"
    case "$variant" in
      absent) rm -- "$d/meta.txt" ;;
      empty) : > "$d/meta.txt" ;;
      truncated) printf 'xray_user_existed=0\nxray_group_existed=0' > "$d/meta.txt" ;;
    esac
    expect "meta $variant refuses" restore_transaction "$d"
    boundary_no_effects "meta $variant"
  done
}

# T40: every service bit, missing/duplicate/empty/invalid; direct restore AND
# rollback_all, proving invalid state is not normalized by state_write.
test_boundary_state_corrupt(){
  echo 'T40 state.txt service fields corrupt: no service operation or state rewrite'
  local key variant i=0 d before
  for key in xray_was_active xray_was_enabled node_panel_was_active node_panel_was_enabled; do
    for variant in missing duplicate empty invalid; do
      lib
      i=$((i+1)); d="$WORK/t40-$i"
      boundary_fixture "$d"
      case "$variant" in
        missing) sed "/^${key}=/d" "$d/state.txt" > "$d/state.edit"; mv "$d/state.edit" "$d/state.txt" ;;
        duplicate) printf '%s=0\n' "$key" >> "$d/state.txt" ;;
        empty) boundary_set_field "$d/state.txt" "$key" '' ;;
        invalid) boundary_set_field "$d/state.txt" "$key" invalid ;;
      esac
      before="$(file_fingerprint "$d/state.txt")"
      expect "$key/$variant refuses" restore_transaction "$d"
      boundary_no_effects "$key/$variant"
      expect "$key/$variant automatic rollback refuses" rollback_all
      boundary_no_effects "$key/$variant rollback"
      expect_ok 'corrupt state not overwritten' fp_equal "$before" "$(file_fingerprint "$d/state.txt")"
    done
  done
}

# T41: valid records followed by a truncated final record; newline-only loss is
# also damage even if the fields look complete. Empty/excess/missing columns
# and blank records are separately rejected. Never silently drop the last line.
test_boundary_tsv_corrupt(){
  echo 'T41 manifest/hashes truncated and incomplete record rejection'
  local name variant i=0 d f line
  for name in manifest hashes; do
    for variant in no-newline partial empty-field extra-column missing-column blank; do
      lib
      i=$((i+1)); d="$WORK/t41-$i"
      boundary_fixture "$d"
      f="$d/$name.tsv"
      expect_ok 'uncorrupted fixture validates' verify_transaction "$d"
      line="$(tail -n 1 -- "$f")"
      case "$variant" in
        no-newline) printf '%s' "$(cat "$f")" > "$f.edit"; mv "$f.edit" "$f" ;;
        partial) printf '%s\t' "$JH_A" >> "$f" ;;
        empty-field)
          if [[ "$name" == manifest ]]; then
            printf '%s\t\t0\tMISSING\n' "$d/new" >> "$f"
          else
            printf '%s\t\tpost\n' "$JH_A" >> "$f"
          fi ;;
        extra-column) printf '%s\textra\n' "$line" >> "$f" ;;
        missing-column) printf '%s\n' "$JH_A" >> "$f" ;;
        blank) printf '\n' >> "$f" ;;
      esac
      expect "$name/$variant verify rejects" verify_transaction "$d"
      expect "$name/$variant restore rejects" restore_transaction "$d"
      boundary_no_effects "$name/$variant"
    done
  done
}

# T42: a successful Xray start followed by failed Panel start must propagate
# to rollback_all and persist ROLLBACK_FAILED, never ROLLED_BACK.
test_boundary_partial_service_failure(){
  echo 'T42 Xray restored but Panel start fails: whole rollback fails'
  lib
  local d="$WORK/t42"
  boundary_fixture "$d"
  boundary_set_field "$d/state.txt" xray_was_active 1
  boundary_set_field "$d/state.txt" node_panel_was_active 1
  XRAY_WAS_ACTIVE=1; NODEPANEL_WAS_ACTIVE=1
  FAIL_START=node-panel.service
  local out rc=0
  out="$(rollback_all 2>&1)" || rc=$?
  if (( rc != 0 )); then ok 'overall nonzero'; else no 'partial failure incorrectly returned zero'; fi
  expect_ok 'Xray start succeeded' grep -q '^started xray.service$' "$CALLS"
  expect_ok 'Panel start reached and failed' grep -q '^systemctl start node-panel.service$' "$CALLS"
  if grep -q '^started node-panel.service$' "$CALLS"; then no 'Panel incorrectly started'; else ok 'Panel did not start'; fi
  expect_ok 'failure status persisted' test "$(state_get "$d/state.txt" status)" = ROLLBACK_FAILED
  if [[ "$out" == *'回滚完成'* ]]; then no 'false rollback-success report'; else ok 'no false rollback-success report'; fi
  expect_content 'file recovery retained' "$JH_A" orig
  expect_file_present 'backup retained' "$d/files/fa"
}

# T43: action ranges match; multiple post records retain latest-record semantics.
test_boundary_journal_and_post(){
  echo 'T43 SERVICE rejected; repeated post records accepted and latest wins'
  lib
  local d="$WORK/t43"
  boundary_fixture "$d"
  expect 'journal_set rejects SERVICE' journal_set "$d" "$JH_A" SERVICE PENDING "$JH_PRE" "$JH_POST"
  expect_file_absent 'SERVICE rejection did not create journal' "$d/restore-journal.tsv"
  printf '1\t%s\tSERVICE\tPENDING\t%s\t%s\n' "$JH_A" "$JH_PRE" "$JH_POST" > "$d/restore-journal.tsv"
  expect 'verify_restore_journal rejects SERVICE' verify_restore_journal "$d"
  rm -- "$d/restore-journal.tsv"
  # Append using the production writer, not a replacement implementation.
  printf 'stage-two' > "$JH_A"
  expect_ok 'append second post via record_hash' record_hash "$JH_A"
  local latest; latest="$(file_fingerprint "$JH_A")"
  expect_ok 'multiple post records validate' verify_transaction "$d"
  expect_ok 'hash_of returns latest' test "$(hash_of "$d" "$JH_A")" = "$latest"
  expect_ok 'latest post permits complete rollback' rollback_all
  expect_content 'original content restored' "$JH_A" orig
}
