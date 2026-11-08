#!/usr/bin/env bash
# T44-T48 are sourced by run-recovery-tests.sh.
# Account/service changes are mocks; Python tests use synthetic keys/temp outputs.
# shellcheck disable=SC2034,SC2317

test_safety_process_snapshot(){
  echo 'T44 process snapshot: empty/busy/error/rc1/missing'
  local mode i=0 d
  for mode in empty busy error rc1 missing; do
    lib
    i=$((i+1)); d="$WORK/t44-$i"
    boundary_fixture "$d"
    PS_MODE="$mode"
    ps(){
      printf 'ps %s\n' "$*" >> "$CALLS"
      [[ "$*" == '-e -o user:256= -o pid=' ]] || return 98
      case "$PS_MODE" in
        empty) printf 'root 1\nnobody 2\n'; return 0 ;;
        busy) printf 'root 1\nxray 123\n'; return 0 ;;
        error) return 2 ;;
        rc1) return 1 ;;
        missing) return 127 ;;
      esac
    }
    expect_ok "$mode fixture valid" verify_transaction "$d"
    if [[ "$mode" == empty ]]; then
      expect_ok 'successful snapshot with no xray process permits recovery' restore_transaction "$d"
      expect_ok 'created user deleted' grep -q '^userdel xray$' "$CALLS"
      expect_ok 'created group deleted' grep -q '^groupdel xray$' "$CALLS"
    else
      expect "$mode blocks recovery" restore_transaction "$d"
      if grep -Eq '^(userdel|groupdel) ' "$CALLS"; then no "$mode must not delete accounts"; else ok "$mode must not delete accounts"; fi
    fi
    expect_ok "$mode full-snapshot query reached" grep -q '^ps -e -o user:256= -o pid=$' "$CALLS"
    boundary_no_start "$mode no inactive service started"
  done
}

test_safety_file_sync_order(){
  echo 'T45 file data sync before rename; sync failure keeps original target'
  local mode i=0 d before
  for mode in success failure; do
    lib
    i=$((i+1)); d="$WORK/t45-$i"
    build_journal_case "$d"
    before="$(file_fingerprint "$JH_A")"
    ORDER="$d/order"; : > "$ORDER"
    SYNC_MODE="$mode"
    fsync_path(){
      if [[ "$1" == *'/.node-panel-restore.'* ]]; then
        printf 'sync-file\n' >> "$ORDER"
        [[ "$SYNC_MODE" == success ]]
      else
        printf 'sync-dir\n' >> "$ORDER"
        return 0
      fi
    }
    mv(){ printf 'rename\n' >> "$ORDER"; command mv "$@"; }
    if [[ "$mode" == success ]]; then
      expect_ok 'regular-file restore succeeds' restore_one_file "$d" "$JH_A" files/fa "$JH_PRE"
      expect_content 'file sync -> rename -> directory sync' "$ORDER" $'sync-file\nrename\nsync-dir'
      expect_content 'pre-install content restored' "$JH_A" orig
    else
      expect 'temporary data sync failure aborts' restore_one_file "$d" "$JH_A" files/fa "$JH_PRE"
      expect_content 'no rename after failed file sync' "$ORDER" sync-file
      expect_ok 'target fingerprint unchanged' fp_equal "$before" "$(file_fingerprint "$JH_A")"
      expect_file_present 'backup retained' "$d/files/fa"
      if compgen -G "$d/.node-panel-restore.*" >/dev/null; then no 'failed temporary file not cleaned'; else ok 'failed temporary file cleaned'; fi
    fi
  done
}

test_safety_symlink_sync(){
  echo 'T46 present/dangling symlink: never sync or follow target'
  local mode i=0 d link target before pre
  for mode in present dangling; do
    lib
    i=$((i+1)); d="$WORK/t46-$i"
    mkdir -p "$d/files"
    link="$d/link"; target="$d/target"
    [[ "$mode" != present ]] || printf 'untouched-target' > "$target"
    before="$(file_fingerprint "$target")"
    ln -s "$target" "$link"
    pre="$(file_fingerprint "$link")"
    cp -a "$link" "$d/files/link"
    rm -- "$link"; printf 'installed' > "$link"
    ORDER="$d/order"; : > "$ORDER"
    fsync_path(){
      if [[ -L "$1" || "$1" == "$target" ]]; then
        printf 'followed-link\n' >> "$ORDER"; return 1
      fi
      printf 'sync-dir\n' >> "$ORDER"; return 0
    }
    expect_ok "$mode link restore succeeds" restore_one_file "$d" "$link" files/link "$pre"
    expect_ok "$mode restored as link" test -L "$link"
    expect_ok "$mode link destination unchanged" test "$(readlink "$link")" = "$target"
    expect_content "$mode parent sync only" "$ORDER" sync-dir
    expect_ok "$mode target untouched" fp_equal "$before" "$(file_fingerprint "$target")"
  done
}

test_safety_private_key_fd(){
  echo 'T47 actual Python generator: FD transport, same config, both deployment modes'
  lib
  expect_ok 'actual config and secret transport regression' command python3 - "$SRC" "$WORK/t47" <<'PYTEST'
import json, os, pathlib, subprocess, sys
src = pathlib.Path(sys.argv[1]).read_text()
d = pathlib.Path(sys.argv[2]); d.mkdir()
start = src.index('python3 - "$XRAY_CONFIG"')
end = src.index('\nPY\n', start) + len('\nPY\n')
block = src[start:end]
assert '"$REALITY_PRIVATE_KEY" \\' not in block
assert '3<<<"$REALITY_PRIVATE_KEY"' in block
key = 'synthetic-regression-key-NOT-A-REAL-SECRET'
ws = {'listen':'127.0.0.1','port':10000,'protocol':'vless',
      'settings':{'clients':[{'id':'ws-test-uuid'}],'decryption':'none'},
      'streamSettings':{'network':'ws','security':'none','wsSettings':{'path':'/test'}}}
reality = {'listen':'0.0.0.0','port':10086,'protocol':'vless',
           'settings':{'clients':[{'id':'reality-test-uuid','flow':'xtls-rprx-vision'}],'decryption':'none'},
           'streamSettings':{'network':'raw','security':'reality','realitySettings':{
               'show':False,'dest':'example.com:443','xver':0,'serverNames':['example.com'],
               'privateKey':key,'shortIds':['0123456789abcdef']}}}
for enabled in ('0', '1'):
    cfg = d / ('config-'+enabled+'.json')
    argv = d / ('argv-'+enabled); environ = d / ('env-'+enabled)
    prefix = '''set -Eeuo pipefail
umask 077
XRAY_CONFIG=$1
ARGS_LOG=$2
ENV_LOG=$3
REALITY_ENABLED=$4
WS_PORT=10000
WS_PATH=/test
WS_UUID=ws-test-uuid
REALITY_PORT=10086
REALITY_UUID=reality-test-uuid
REALITY_SNI=example.com
REALITY_TARGET=example.com:443
REALITY_SHORT_ID=0123456789abcdef
REALITY_PRIVATE_KEY=synthetic-regression-key-NOT-A-REAL-SECRET
python3(){
  printf '%s\\0' "$@" > "$ARGS_LOG"
  env > "$ENV_LOG"
  command python3 "$@"
}
'''
    env = os.environ.copy(); env.pop('REALITY_PRIVATE_KEY', None)
    subprocess.run(['bash','-s','--',str(cfg),str(argv),str(environ),enabled],
                   input=prefix+block,text=True,env=env,check=True,capture_output=True)
    assert key.encode() not in argv.read_bytes(), 'key leaked in argv'
    assert key.encode() not in environ.read_bytes(), 'key exported'
    expected = {'log':{'loglevel':'warning'},'inbounds':[ws]+([reality] if enabled=='1' else []),
                'outbounds':[{'protocol':'freedom','tag':'direct'},{'protocol':'blackhole','tag':'blocked'}]}
    assert json.loads(cfg.read_text()) == expected, 'configuration changed'
    assert cfg.stat().st_mode & 0o777 == 0o640
    assert not cfg.with_name(cfg.name+'.new').exists()
PYTEST
}

test_safety_help_and_sources(){
  echo 'T48 current filename in help; both download sources unchanged'
  lib
  local out
  out="$(bash "$SRC" --help)"
  expect_ok 'help uses current entrypoint' grep -q 'bash install.sh --recover' <<< "$out"
  if grep -q 'install-dual.sh' <<< "$out"; then no 'help retains archived entrypoint'; else ok 'help has no archived entrypoint'; fi
  if grep -q 'install-dual.sh' "$SRC"; then no 'installer retains stale filename'; else ok 'installer has no stale filename'; fi
  expect_ok 'panel URL unchanged' grep -Fxq 'PANEL_URL="https://raw.githubusercontent.com/universeglitch/vps-monitor-lite/main/panel_dual.py"' "$SRC"
  expect_ok 'Xray URL unchanged' grep -Fxq 'XRAY_INSTALL="https://github.com/XTLS/Xray-install/raw/main/install-release.sh"' "$SRC"
}
