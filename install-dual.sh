#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# =========================================================
# Debian 13: Xray VLESS-WS + VLESS-REALITY + Node Monitor
# Installs panel_dual.py from the configured GitHub raw URL.
# Cloudflared is intentionally not installed or configured.
# =========================================================

PANEL_URL="https://raw.githubusercontent.com/universeglitch/vps-monitor-lite/main/panel_dual.py"
XRAY_INSTALL="https://github.com/XTLS/Xray-install/raw/main/install-release.sh"

PANEL_DIR="/opt/node-panel"
PANEL_FILE="$PANEL_DIR/panel.py"
XRAY_CONFIG="/usr/local/etc/xray/config.json"
PANEL_CONFIG="/etc/node-panel/panel-config.json"
AUTH_FILE="/etc/node-panel/auth.json"
SECRET_FILE="/etc/node-panel/secret.key"

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

# =========================================================
# 安全安装事务：覆盖保护、备份、失败回滚
# =========================================================
INSTALL_COMMITTED=0
ROLLBACK_READY=0
BACKUP_DIR=""
PASSWORD_FILE=""
declare -a TRACK_SRC=()
declare -a TRACK_BAK=()
declare -a TRACK_DIRS=()

svc_active(){ systemctl is-active --quiet "$1" 2>/dev/null; }
svc_enabled(){ systemctl is-enabled --quiet "$1" 2>/dev/null; }

port_in_use(){
  local port="$1"
  ss -H -lnt 2>/dev/null | awk -v p=":$port" '$4 ~ (p "$") {found=1} END {exit !found}'
}

track_file(){
  local src="$1"
  local rel="$2"
  local dst="$BACKUP_DIR/$rel"
  TRACK_SRC+=("$src")
  TRACK_BAK+=("$dst")
  mkdir -p "$(dirname "$dst")"
  if [[ -e "$src" || -L "$src" ]]; then
    cp -a -- "$src" "$dst"
  else
    : > "$dst.absent"
  fi
}

rollback_all(){
  local failed=0 i src bak xray_pids
  yellow "正在回滚本次安装……"
  systemctl stop node-panel.service >/dev/null 2>&1 || true
  systemctl stop xray.service >/dev/null 2>&1 || true
  
  for ((i=${#TRACK_SRC[@]}-1; i>=0; i--)); do
    src="${TRACK_SRC[$i]}"; bak="${TRACK_BAK[$i]}"
    if [[ -f "$bak.absent" ]]; then
      if ! rm -f -- "$src"; then
        red "回滚失败：无法删除新建文件 $src"; failed=1
      fi
    elif [[ -e "$bak" || -L "$bak" ]]; then
      if ! mkdir -p "$(dirname "$src")" || ! cp -a --remove-destination "$bak" "$src"; then
        red "回滚失败：无法恢复 $src"; failed=1
      fi
    fi
  done

  systemctl daemon-reload >/dev/null 2>&1 || failed=1
  # 清理本次安装创建的 Xray 用户和用户组
  if getent passwd xray >/dev/null 2>&1; then
    if ! xray_pids="$(ps -u xray -o pid= 2>/dev/null)"; then
      red "回滚失败：无法检查 Xray 用户进程"
      failed=1
    elif [[ -n "${xray_pids//[[:space:]]/}" ]]; then
      red "回滚失败：Xray 用户仍有进程，不能删除账户"
      failed=1
    elif ! userdel xray; then
      red "回滚失败：无法删除 Xray 用户"
      failed=1
    fi
  fi

  # 仅在 Xray 用户已经不存在时删除同名用户组
  if ! getent passwd xray >/dev/null 2>&1 &&
     getent group xray >/dev/null 2>&1; then
    if ! groupdel xray; then
      red "回滚失败：无法删除 Xray 用户组"
      failed=1
    fi
  fi

  # 恢复 Xray 原来的启用状态
  if [[ "$XRAY_WAS_ENABLED" == "1" ]]; then
    systemctl enable xray.service >/dev/null 2>&1 || failed=1
  fi

  # 恢复面板原来的启用状态
  if [[ "$NODEPANEL_WAS_ENABLED" == "1" ]]; then
    systemctl enable node-panel.service >/dev/null 2>&1 || failed=1
  fi

  # 恢复 Xray 原来的运行状态
  if [[ "$XRAY_WAS_ACTIVE" == "1" ]]; then
    systemctl start xray.service >/dev/null 2>&1 || failed=1
  fi

  # 恢复面板原来的运行状态
  if [[ "$NODEPANEL_WAS_ACTIVE" == "1" ]]; then
    systemctl start node-panel.service >/dev/null 2>&1 || failed=1
  fi

  for ((i=${#TRACK_DIRS[@]}-1; i>=0; i--)); do
    rmdir -- "${TRACK_DIRS[$i]}" 2>/dev/null || true
  done

  if (( failed == 0 )); then
    green "回滚完成：已恢复安装前文件状态。备份保留在：$BACKUP_DIR"
    return 0
  else
    red "回滚未能完全完成。请保留并检查备份目录：$BACKUP_DIR"
    return 1
  fi
}

cleanup_temp(){
  [[ -n "${PASSWORD_FILE:-}" ]] && rm -f -- "$PASSWORD_FILE" 2>/dev/null || true
  PASSWORD_FILE=""
  unset PANEL_PASSWORD PASSWORD2 REALITY_PRIVATE_KEY KEY_OUTPUT
}
on_exit(){
  local rc=$?
  trap - EXIT INT TERM
  cleanup_temp
  if (( rc != 0 && ROLLBACK_READY == 1 && INSTALL_COMMITTED == 0 )); then
  if ! rollback_all; then
    red "严重警告：安装失败，而且自动回滚未能完全完成！"
    red "请勿直接重新运行安装脚本。"
    red "请保留备份目录并检查：$BACKUP_DIR"
  fi
fi
  exit "$rc"
}

trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

detect_existing(){
  local found=0 item
  local -a paths=(
    "$XRAY_CONFIG" "$PANEL_FILE" "$PANEL_CONFIG" "$AUTH_FILE" "$SECRET_FILE"
    "/etc/systemd/system/node-panel.service"
    "/etc/systemd/system/xray.service" "/etc/systemd/system/xray@.service"
    "/usr/local/lib/systemd/system/xray.service" "/usr/local/lib/systemd/system/xray@.service"
    "/lib/systemd/system/xray.service" "/lib/systemd/system/xray@.service"
    "/etc/systemd/system/xray.service.d"
    "/etc/systemd/system/xray@.service.d"
    "/var/log/xray"
  )

  for item in "${paths[@]}"; do
    if [[ -e "$item" || -L "$item" ]]; then
      red "检测到已有部署文件：$item"
      found=1
    fi
  done

  if getent passwd xray >/dev/null 2>&1; then
    red "检测到已有 Xray 用户"
    found=1
  fi

  if getent group xray >/dev/null 2>&1; then
    red "检测到已有 Xray 用户组"
    found=1
  fi

  local xray_bin=""
  xray_bin="$(command -v xray 2>/dev/null || true)"

  for item in /usr/local/bin/xray /usr/bin/xray /usr/local/sbin/xray /usr/sbin/xray; do
    if [[ -x "$item" ]]; then
      xray_bin="$item"
      break
    fi
  done

  if [[ -n "$xray_bin" ]]; then
    red "检测到已有 Xray 程序：$xray_bin"
    found=1
  fi

  if systemctl cat xray.service >/dev/null 2>&1; then
    red "检测到已有 xray.service"
    found=1
  fi

  if systemctl cat node-panel.service >/dev/null 2>&1; then
    red "检测到已有 node-panel.service"
    found=1
  fi

  (( found == 0 )) || die "发现已有 Xray / Node Monitor 部署痕迹。为防止覆盖现有服务，安装已停止。"
}
[[ $EUID -eq 0 ]] || die "请使用 root 运行。"
[[ -d /run/systemd/system ]] || die "未检测到 systemd。"
[[ -f /etc/os-release ]] || die "无法识别系统。"

. /etc/os-release
[[ "$ID" == "debian" ]] || die "本脚本仅针对 Debian。"
[[ "${VERSION_ID%%.*}" == "13" ]] ||
  yellow "提示：当前系统不是 Debian 13，尚未针对该版本验证。"

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

yellow "【3】REALITY 设置"

REALITY_PORT="$(ask "REALITY 监听端口" "10086")"
REALITY_ADDR="$(ask "REALITY 客户端连接地址（公网 IPv4 或域名）")"
REALITY_SNI="$(ask "REALITY SNI / serverName")"
REALITY_TARGET="$(ask "REALITY dest / target（例如 example.com:443）")"

# ------------------- 参数验证 -------------------

for p in "$PANEL_PORT" "$WS_PORT" "$WS_PUBLIC_PORT" "$REALITY_PORT"; do
  valid_port "$p" || die "端口格式错误：$p"
done

[[ "$PANEL_PORT" != "$WS_PORT" ]] || die "面板端口和 WS 端口不能相同。"
[[ "$PANEL_PORT" != "$REALITY_PORT" ]] || die "面板端口和 REALITY 端口不能相同。"
[[ "$WS_PORT" != "$REALITY_PORT" ]] || die "WS 和 REALITY 端口不能相同。"

valid_host "$WS_HOST" || die "WS 域名格式错误。"
valid_host "$REALITY_ADDR" || die "REALITY 地址格式错误（当前脚本不支持 IPv6）。"
valid_host "$REALITY_SNI" || die "REALITY SNI 格式错误。"

[[ "$REALITY_TARGET" =~ ^([A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?):([0-9]{1,5})$ ]] ||
  die "REALITY target 格式错误，应为 hostname:port。"
TARGET_PORT="${BASH_REMATCH[3]}"
valid_port "$TARGET_PORT" || die "REALITY target 端口必须在 1–65535 之间。"

if [[ -z "$WS_PATH" ]]; then
  WS_PATH="/$(openssl rand -hex 16)"
fi
[[ "$WS_PATH" == /* ]] || die "WS Path 必须以 / 开头。"
[[ "$WS_PATH" != *[[:space:]\#\?]* ]] || die "WS Path 不能包含空格、# 或 ?。"

echo
yellow "========== 配置确认 =========="
printf '%-25s %s\n' "面板监听：" "127.0.0.1:$PANEL_PORT"
printf '%-25s %s\n' "WS 源站：" "127.0.0.1:$WS_PORT"
printf '%-25s %s\n' "WS 公网入口：" "$WS_HOST:$WS_PUBLIC_PORT"
printf '%-25s %s\n' "WS Path：" "$WS_PATH"
printf '%-25s %s\n' "REALITY 监听：" "0.0.0.0:$REALITY_PORT"
printf '%-25s %s\n' "REALITY 客户端地址：" "$REALITY_ADDR:$REALITY_PORT"
printf '%-25s %s\n' "REALITY SNI：" "$REALITY_SNI"
printf '%-25s %s\n' "REALITY Target：" "$REALITY_TARGET"

echo
yellow "确认开始安装？输入 YES 继续。"
read -r -p "> " CONFIRM
[[ "$CONFIRM" == "YES" ]] || die "已取消。"

# ------------------- 覆盖保护、端口检查与事务备份 -------------------

green "安装前覆盖保护检查"
detect_existing

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$(mktemp -d "/var/backups/node-panel-install-${STAMP}.XXXXXX")"
mkdir -p "$BACKUP_DIR/files"
chmod 700 "$BACKUP_DIR"

for d in \
  "$PANEL_DIR" \
  /etc/node-panel \
  /usr/local/etc/xray \
  /etc/systemd/system/xray.service.d \
  /etc/systemd/system/xray@.service.d
do
  [[ -d "$d" ]] || TRACK_DIRS+=("$d")
done

# 记录安装前服务状态；覆盖保护通过时，服务应当不存在/未运行。
XRAY_WAS_ACTIVE="$(svc_active xray && echo 1 || echo 0)"
XRAY_WAS_ENABLED="$(svc_enabled xray && echo 1 || echo 0)"
NODEPANEL_WAS_ACTIVE="$(svc_active node-panel && echo 1 || echo 0)"
NODEPANEL_WAS_ENABLED="$(svc_enabled node-panel && echo 1 || echo 0)"

track_file "$XRAY_CONFIG" "files/xray-config.json"
track_file "${XRAY_CONFIG}.new" "files/xray-config.json.new"
track_file "$PANEL_FILE" "files/panel.py"
track_file "$PANEL_CONFIG" "files/panel-config.json"
track_file "${PANEL_CONFIG}.new" "files/panel-config.json.new"
track_file "$AUTH_FILE" "files/auth.json"
track_file "$SECRET_FILE" "files/secret.key"
track_file "/etc/systemd/system/node-panel.service" "files/node-panel.service"
track_file "/etc/systemd/system/xray.service" "files/xray.service"
track_file "/etc/systemd/system/xray@.service" "files/xray-template.service"
track_file "/etc/systemd/system/xray.service.d/10-donot_touch_single_conf.conf" "files/xray-single-dropin.service.conf"
track_file "/etc/systemd/system/xray@.service.d/10-donot_touch_single_conf.conf" "files/xray-template-single-dropin.service.conf"
track_file "/usr/local/lib/systemd/system/xray.service" "files/xray-local.service"
track_file "/usr/local/lib/systemd/system/xray@.service" "files/xray-local-template.service"
track_file "/lib/systemd/system/xray.service" "files/xray-lib.service"
track_file "/lib/systemd/system/xray@.service" "files/xray-lib-template.service"
track_file "/usr/local/bin/xray" "files/xray-bin"

cat > "$BACKUP_DIR/state.txt" <<EOF
stamp=$STAMP
xray_was_active=$XRAY_WAS_ACTIVE
xray_was_enabled=$XRAY_WAS_ENABLED
node_panel_was_active=$NODEPANEL_WAS_ACTIVE
node_panel_was_enabled=$NODEPANEL_WAS_ENABLED
EOF
chmod 600 "$BACKUP_DIR/state.txt"
ROLLBACK_READY=1

# 创建目录放在备份记录之后，失败时可清理新建的空目录。
mkdir -p "$PANEL_DIR" /etc/node-panel /usr/local/etc/xray
yellow "事务备份完成：$BACKUP_DIR"

# ------------------- 安装依赖 -------------------

green "[1/8] 安装依赖"

export DEBIAN_FRONTEND=noninteractive
apt-get update || die "APT 软件包索引更新失败。"

apt-get install -y --no-install-recommends \
  ca-certificates curl openssl python3 python3-qrcode python3-pil iproute2 \
  || die "系统依赖安装失败。"
# ------------------- 安装前端口占用检查 -------------------

for p in "$WS_PORT" "$REALITY_PORT" "$PANEL_PORT"; do
  if port_in_use "$p"; then
    die "端口 $p 已被占用，请更换端口或先处理占用进程。"
  fi
done
# ------------------- 安装 Xray -------------------

green "[2/8] 安装 Xray"

if ! command -v xray >/dev/null 2>&1; then
  bash -c "$(curl -fsSL "$XRAY_INSTALL")" @ install --without-geodata --without-logfiles
fi

XRAY_BIN="$(command -v xray || true)"
[[ -x "$XRAY_BIN" ]] || XRAY_BIN="/usr/local/bin/xray"
[[ -x "$XRAY_BIN" ]] || die "找不到 Xray。"

# ------------------- 生成身份 -------------------

green "[3/8] 生成节点身份"

WS_UUID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
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

# ------------------- 生成 Xray 配置 -------------------

green "[4/8] 生成 Xray 双入站配置"

python3 - "$XRAY_CONFIG" "$WS_PORT" "$WS_PATH" "$WS_UUID" \
  "$REALITY_PORT" "$REALITY_UUID" "$REALITY_PRIVATE_KEY" \
  "$REALITY_SNI" "$REALITY_TARGET" "$REALITY_SHORT_ID" <<'PY'
import json, sys
from pathlib import Path

(
    output, ws_port, ws_path, ws_uuid,
    reality_port, reality_uuid, private_key,
    sni, target, short_id
) = sys.argv[1:]

config = {
    "log": {"loglevel": "warning"},
    "inbounds": [
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
        },
        {
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
                    "xver": 0,
                    "serverNames": [sni],
                    "privateKey": private_key,
                    "shortIds": [short_id]
                }
            }
        }
    ],
    "outbounds": [
        {"protocol": "freedom", "tag": "direct"},
        {"protocol": "blackhole", "tag": "blocked"}
    ]
}

path = Path(output)
tmp.write_text(json.dumps(config, indent=2) + "\n")
tmp.chmod(0o640)
tmp.replace(path)
PY

unset REALITY_PRIVATE_KEY
chown root:xray "$XRAY_CONFIG" 2>/dev/null || true
chmod 640 "$XRAY_CONFIG"

"$XRAY_BIN" run -test -config "$XRAY_CONFIG" || die "Xray 配置检查失败。"

# ------------------- 安装新版双节点面板 -------------------

green "[5/8] 安装 Node Monitor 双节点版"

curl -fsSL "$PANEL_URL" -o "$PANEL_FILE" ||
  die "下载 panel_dual.py 失败。请确认该文件已上传到 GitHub 仓库指定路径。"

grep -q 'PANEL_CONFIG = Path("/etc/node-panel/panel-config.json")' "$PANEL_FILE" ||
  die "下载到的文件不是预期的 panel_dual.py。"

# panel_dual.py 使用嵌套配置结构，和 Xray 入站端口保持一致。
python3 - "$PANEL_CONFIG" "$PANEL_PORT" \
  "$WS_PORT" "$WS_HOST" "$WS_PUBLIC_PORT" "$WS_PATH" \
  "$REALITY_PORT" "$REALITY_ADDR" "$REALITY_SNI" \
  "$REALITY_PUBLIC_KEY" "$REALITY_SHORT_ID" <<'PY'
import json, sys
from pathlib import Path

(
    output, panel_port,
    ws_listen_port, ws_host, ws_port, ws_path,
    reality_listen_port, reality_address, reality_sni,
    reality_public_key, reality_short_id
) = sys.argv[1:]

cfg = {
    "panel_port": int(panel_port),
    "ws": {
        "listen_port": int(ws_listen_port),
        "host": ws_host,
        "port": int(ws_port),
        "path": ws_path
    },
    "reality": {
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
}

path = Path(output)
tmp.write_text(json.dumps(cfg, indent=2) + "\n")
tmp.chmod(0o600)
tmp.replace(path)
PY

chmod 600 "$PANEL_CONFIG"
python3 -c 'import sys; compile(open(sys.argv[1], encoding="utf-8").read(), sys.argv[1], "exec")' "$PANEL_FILE" ||
  die "面板 Python 语法检查失败。"

# ------------------- 面板认证 -------------------

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

salt = secrets.token_bytes(16)
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

# ------------------- systemd 服务 -------------------

green "[7/8] 创建面板 systemd 服务"

cat > /etc/systemd/system/node-panel.service <<EOF
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

systemctl daemon-reload
systemctl enable xray
systemctl enable --now xray
systemctl enable --now node-panel

# ------------------- 最终检查 -------------------

green "[8/8] 最终检查"

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
for p in "$WS_PORT" "$REALITY_PORT" "$PANEL_PORT"; do
  if ! ss -H -lnt 2>/dev/null | awk -v p=":$p" '$4 ~ (p "$") {found=1} END {exit !found}'; then
    die "端口 $p 未监听，安装验收失败。"
  fi
  green "端口 $p 正在监听。"
done

echo
echo "===== 面板页面检查 ====="
curl -fsS "http://127.0.0.1:${PANEL_PORT}/usage" >/dev/null ||
  die "面板 /usage 页面检查失败，请查看 node-panel 日志。"
green "面板 HTTP 页面响应正常。"

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
echo "REALITY 节点："
echo "  地址：$REALITY_ADDR"
echo "  端口：$REALITY_PORT"
echo "  UUID：$REALITY_UUID"
echo "  SNI：$REALITY_SNI"
echo "  Public Key：$REALITY_PUBLIC_KEY"
echo "  Short ID：$REALITY_SHORT_ID"
echo "  Flow：xtls-rprx-vision"
echo
yellow "Cloudflare Tunnel 需要你自行配置："
echo "  $WS_HOST -> HTTP -> 127.0.0.1:$WS_PORT"
echo "  面板域名 -> HTTP -> 127.0.0.1:$PANEL_PORT"
echo
yellow "REALITY 需要 VPS 服务商允许 TCP $REALITY_PORT 入站。"
yellow "如果运行在容器内，还需要宿主机映射该 TCP 端口。"
yellow "本脚本没有安装、登录或配置 Cloudflared。"
echo
echo "查看日志："
echo "  journalctl -u xray -u node-panel -n 50 --no-pager"
echo
yellow "注意：请先将 panel_dual.py 上传到配置的 GitHub 仓库路径，再运行本脚本。"


# 所有检查通过，正式提交安装事务；之后不再自动回滚。
INSTALL_COMMITTED=1
ROLLBACK_READY=0
trap - EXIT INT TERM
cleanup_temp
green "安装事务已提交。备份目录：$BACKUP_DIR"
