cat > /root/node-stack-installer.txt <<'SCRIPT'
#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# =========================================================
# Debian 13: Xray VLESS-WS + VLESS-REALITY + Node Monitor
# Cloudflared is intentionally not installed or configured.
# =========================================================

REPO="https://raw.githubusercontent.com/universeglitch/vps-monitor-lite/main"
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

[[ "$WS_HOST" =~ ^[A-Za-z0-9.-]+$ ]] || die "WS 域名格式错误。"
[[ "$REALITY_ADDR" =~ ^[A-Za-z0-9.-]+$ ]] || die "REALITY 地址格式错误。"
[[ "$REALITY_SNI" =~ ^[A-Za-z0-9.-]+$ ]] || die "REALITY SNI 格式错误。"
[[ "$REALITY_TARGET" =~ ^[A-Za-z0-9.-]+:[0-9]{1,5}$ ]] || die "REALITY target 格式错误。"

[[ "$WS_PATH" == "" || "$WS_PATH" == /* ]] ||
  die "WS Path 必须以 / 开头。"

[[ -n "$WS_PATH" ]] || WS_PATH="/$(openssl rand -hex 16)"

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

# ------------------- 备份 -------------------

STAMP="$(date +%Y%m%d-%H%M%S)"

if [[ -s "$XRAY_CONFIG" ]]; then
  cp -a "$XRAY_CONFIG" "$XRAY_CONFIG.bak-$STAMP"
  yellow "已备份旧 Xray 配置。"
fi

if [[ -f "$PANEL_FILE" ]]; then
  cp -a "$PANEL_FILE" "$PANEL_FILE.bak-$STAMP"
fi

# ------------------- 安装依赖 -------------------

green "[1/8] 安装依赖"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl openssl python3 \
  python3-qrcode python3-pil

# ------------------- 安装 Xray -------------------

green "[2/8] 安装 Xray"

if ! command -v xray >/dev/null 2>&1; then
  bash -c "$(curl -fsSL "$XRAY_INSTALL")" @ install --without-geodata
fi

XRAY_BIN="$(command -v xray || true)"
[[ -x "$XRAY_BIN" ]] || XRAY_BIN="/usr/local/bin/xray"
[[ -x "$XRAY_BIN" ]] || die "找不到 Xray。"

# ------------------- 生成身份 -------------------

green "[3/8] 生成节点身份"

WS_UUID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
REALITY_UUID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
REALITY_SHORT_ID="$(openssl rand -hex 8)"

KEY_OUTPUT="$("$XRAY_BIN" x25519 2>&1)" ||
  die "REALITY 密钥生成失败。"

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

mkdir -p /usr/local/etc/xray

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
tmp = path.with_suffix(".json.new")
tmp.write_text(json.dumps(config, indent=2) + "\n")
tmp.replace(path)
PY

unset REALITY_PRIVATE_KEY

chown root:xray "$XRAY_CONFIG" 2>/dev/null || true
chmod 640 "$XRAY_CONFIG"

"$XRAY_BIN" run -test -config "$XRAY_CONFIG" ||
  die "Xray 配置检查失败。"

# ------------------- 安装面板 -------------------

green "[5/8] 下载并升级 Node Monitor"

mkdir -p "$PANEL_DIR" /etc/node-panel

curl -fsSL "$REPO/panel.py" -o "$PANEL_FILE"

# ------------------- 修改面板支持双节点 -------------------

python3 - "$PANEL_FILE" "$PANEL_CONFIG" \
  "$PANEL_PORT" "$WS_HOST" "$WS_PUBLIC_PORT" "$WS_PORT" \
  "$REALITY_ADDR" "$REALITY_PORT" "$REALITY_SNI" \
  "$REALITY_UUID" "$REALITY_SHORT_ID" "$REALITY_PUBLIC_KEY" <<'PATCH'
import json, re, sys
from pathlib import Path

(
    panel_path, config_path, panel_port,
    ws_host, ws_public_port, ws_port,
    reality_addr, reality_port, reality_sni,
    reality_uuid, reality_short_id, reality_public_key
) = sys.argv[1:]

p = Path(panel_path)
src = p.read_text(encoding="utf-8")

cfg = {
    "panel_port": int(panel_port),
    "ws_host": ws_host,
    "ws_public_port": int(ws_public_port),
    "ws_port": int(ws_port),
    "reality_addr": reality_addr,
    "reality_port": int(reality_port),
    "reality_sni": reality_sni,
    "reality_uuid": reality_uuid,
    "reality_short_id": reality_short_id,
    "reality_public_key": reality_public_key
}

Path(config_path).write_text(json.dumps(cfg, indent=2) + "\n")

# 加载额外面板配置
anchor = 'XRAY_CONFIG = Path("/usr/local/etc/xray/config.json")'
if anchor not in src:
    raise SystemExit("面板源码版本不匹配：XRAY_CONFIG 不存在。")

src = src.replace(
    anchor,
    anchor + '\nPANEL_CONFIG = Path("/etc/node-panel/panel-config.json")\n'
    'PANEL_CFG = json.loads(PANEL_CONFIG.read_text())',
    1
)

src = re.sub(
    r'HOST = "[^"]*"\s*\nPORT = \d+',
    'HOST = PANEL_CFG["ws_host"]\nPORT = PANEL_CFG["ws_public_port"]',
    src,
    count=1
)

# 替换单节点 URI 生成函数
start = src.find("def get_uri():")
end = src.find("def lookup_visitor_network(ip):", start)

if start < 0 or end < 0:
    raise SystemExit("无法定位节点 URI 函数，停止修改。")

new_function = r'''
def get_uris():
    data = json.loads(XRAY_CONFIG.read_text())
    result = {"ws": None, "reality": None}

    for inbound in data.get("inbounds", []):
        if inbound.get("protocol") != "vless":
            continue

        port = inbound.get("port")
        clients = inbound.get("settings", {}).get("clients", [])
        stream = inbound.get("streamSettings", {})

        if not clients:
            continue

        uid = clients[0].get("id")

        if port == PANEL_CFG["ws_port"] and stream.get("network") == "ws":
            path = stream.get("wsSettings", {}).get("path")
            if path:
                query = urlencode({
                    "encryption": "none",
                    "security": "tls",
                    "sni": HOST,
                    "fp": "chrome",
                    "type": "ws",
                    "host": HOST,
                    "path": path
                }, quote_via=quote)

                result["ws"] = (
                    f"vless://{uid}@{HOST}:{PORT}"
                    f"?{query}#VLESS-WS"
                )

        if port == PANEL_CFG["reality_port"] and stream.get("security") == "reality":
            reality = stream.get("realitySettings", {})
            names = reality.get("serverNames", [])
            short_ids = reality.get("shortIds", [])
            flow = clients[0].get("flow", "")

            if names and short_ids:
                query = urlencode({
                    "encryption": "none",
                    "security": "reality",
                    "sni": names[0],
                    "fp": "chrome",
                    "pbk": PANEL_CFG["reality_public_key"],
                    "sid": short_ids[0],
                    "type": "tcp",
                    "flow": flow
                }, quote_via=quote)

                result["reality"] = (
                    f"vless://{uid}@{PANEL_CFG['reality_addr']}:"
                    f"{PANEL_CFG['reality_port']}?{query}#VLESS-REALITY"
                )

    if not result["ws"] or not result["reality"]:
        raise RuntimeError("WS 或 REALITY 入站配置不完整")

    return result


def get_uri(node="ws"):
    result = get_uris()
    if node not in result:
        raise RuntimeError("未知节点类型")
    return result[node]


'''

src = src[:start] + new_function + src[end:]

# 动态读取两个节点端口
src = src.replace(
    '"node_a": port_active(10000)',
    '"node_a": port_active(PANEL_CFG["ws_port"])'
)
src = src.replace(
    '"node_b": port_active(10086)',
    '"node_b": port_active(PANEL_CFG["reality_port"])'
)

# 替换节点卡片 HTML
old_html = '''<h2 style="font-size:19px">NekoBox 节点配置</h2>
 <div class="sub">当前部署的 VLESS + WS + TLS 节点</div>
 <div class="grid">
 <div class="tile" style="text-align:center">
 <img class="qr" id="qr" alt="节点二维码">
 <div class="sub" style="margin:10px 0 0">扫码导入节点</div>
 </div>
 <div class="tile">
 <div class="sub">配置仅在登录后读取，二维码和分享链接由服务器动态生成。</div>
 <div class="actions">
 <button id="copy">复制分享链接</button>
 <a class="btn secondary" id="download">下载配置</a>
 </div>
 <div class="error" id="configError"></div>
 </div></div>'''

new_html = '''<h2 style="font-size:19px">NekoBox 节点配置</h2>
<div class="sub">两个节点独立生成二维码、分享链接和配置文件</div>
<div class="grid">
 <div class="tile" style="text-align:center">
  <strong>节点 A · VLESS + WS + TLS</strong>
  <div class="sub">Cloudflare Tunnel</div>
  <img class="qr" id="qrWs" alt="WS 二维码">
  <div class="actions">
   <button id="copyWs">复制 WS 链接</button>
   <a class="btn secondary" id="downloadWs">下载 WS 配置</a>
  </div>
 </div>
 <div class="tile" style="text-align:center">
  <strong>节点 B · VLESS + REALITY</strong>
  <div class="sub">VPS 公网直连</div>
  <img class="qr" id="qrReality" alt="REALITY 二维码">
  <div class="actions">
   <button id="copyReality">复制 REALITY 链接</button>
   <a class="btn secondary" id="downloadReality">下载 REALITY 配置</a>
  </div>
 </div>
</div>
<div class="error" id="configError"></div>'''

if old_html not in src:
    raise SystemExit("无法定位原版节点 HTML，停止修改。")

src = src.replace(old_html, new_html, 1)

# 替换单节点前端 JS
js_start = src.find("let shareUri=null;")
js_end = src.find("const browserTargets=[", js_start)

if js_start < 0 or js_end < 0:
    raise SystemExit("无法定位原版节点 JS，停止修改。")

new_js = r'''let shareUris={};

async function copyUri(node){
  const uri=shareUris[node];
  if(!uri){
    $('configError').textContent='配置尚未加载';
    return;
  }

  try{
    if(navigator.clipboard && navigator.clipboard.writeText){
      await navigator.clipboard.writeText(uri);
    }else{
      const box=document.createElement('textarea');
      box.value=uri;
      box.style.position='fixed';
      box.style.opacity='0';
      document.body.appendChild(box);
      box.focus();
      box.select();
      const ok=document.execCommand('copy');
      box.remove();
      if(!ok)throw Error('copy failed');
    }

    $('configError').textContent='已复制 '+node.toUpperCase()+' 分享链接';
  }catch(e){
    $('configError').textContent='复制失败，请使用下载配置';
  }
}

async function loadConfig(){
  try{
    const r=await request('config');
    const d=await r.json();
    shareUris=d.uris||{};

    $('qrWs').src=endpoint+'?action=qr&node=ws&v='+Date.now();
    $('qrReality').src=endpoint+'?action=qr&node=reality&v='+Date.now();

    $('downloadWs').href=endpoint+'?action=download&node=ws';
    $('downloadReality').href=endpoint+'?action=download&node=reality';

    $('copyWs').onclick=()=>copyUri('ws');
    $('copyReality').onclick=()=>copyUri('reality');
  }catch(e){
    $('configError').textContent=e.message;
  }
}
'''

src = src[:js_start] + new_js + src[js_end:]

# 修改 API 返回两个节点
old_config = 'self.send_body(200, json.dumps({"uri": get_uri()}), "application/json")'
new_config = 'self.send_body(200, json.dumps({"uris": get_uris()}), "application/json")'

if old_config not in src:
    raise SystemExit("无法定位 config API。")

src = src.replace(old_config, new_config, 1)

# 二维码接口支持 node 参数
old_qr = '''img = qrcode.make(get_uri())
                buf = BytesIO()'''

new_qr = '''node = parse_qs(parsed.query).get("node", ["ws"])[0]
                img = qrcode.make(get_uri(node))
                buf = BytesIO()'''

if old_qr not in src:
    raise SystemExit("无法定位二维码 API。")

src = src.replace(old_qr, new_qr, 1)

# 下载接口支持 node 参数
old_download = '''self.send_body(200, get_uri() + "\\n", "text/plain; charset=utf-8",
                                {"Content-Disposition": 'attachment; filename="node-a.txt"'})'''

new_download = '''node = parse_qs(parsed.query).get("node", ["ws"])[0]
                filename = "node-reality.txt" if node == "reality" else "node-ws.txt"
                self.send_body(200, get_uri(node) + "\\n", "text/plain; charset=utf-8",
                                {"Content-Disposition": f'attachment; filename="{filename}"'})'''

if old_download not in src:
    raise SystemExit("无法定位下载 API。")

src = src.replace(old_download, new_download, 1)

# 修改面板监听端口
old_server = 'server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)'
new_server = 'server = ThreadingHTTPServer(("127.0.0.1", PANEL_CFG["panel_port"]), Handler)'

if old_server not in src:
    raise SystemExit("无法定位面板监听端口。")

src = src.replace(old_server, new_server, 1)

p.write_text(src, encoding="utf-8")
PATCH

python3 -m py_compile "$PANEL_FILE" ||
  die "面板 Python 语法检查失败。"

# ------------------- 面板认证 -------------------

green "[6/8] 创建面板认证信息"

python3 - "$AUTH_FILE" "$SECRET_FILE" "$PANEL_PASSWORD" <<'PY'
import hashlib, json, os, secrets, sys
from pathlib import Path

auth_file, secret_file, password = sys.argv[1:]

salt = secrets.token_bytes(16)
iterations = 310000
digest = hashlib.pbkdf2_hmac(
    "sha256", password.encode(), salt, iterations
).hex()

Path(auth_file).write_text(json.dumps({
    "salt": salt.hex(),
    "iterations": iterations,
    "hash": digest
}) + "\n")

Path(secret_file).write_bytes(secrets.token_bytes(32))

os.chmod(auth_file, 0o600)
os.chmod(secret_file, 0o600)
PY

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
ss -lnt | grep -E ":${WS_PORT}|:${REALITY_PORT}|:${PANEL_PORT}" || true

echo
echo "===== 面板接口 ====="
curl -fsS "http://127.0.0.1:${PANEL_PORT}/usage" >/dev/null ||
  yellow "面板 /usage 检查未通过，请查看日志。"

# ------------------- 输出部署信息 -------------------

echo
green "=================================================="
green "                 部署完成"
green "=================================================="

echo "面板本地地址： http://127.0.0.1:$PANEL_PORT"
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
echo
yellow "面板需要你自行配置："
echo "  面板域名 -> HTTP -> 127.0.0.1:$PANEL_PORT"
echo
yellow "REALITY 需要 VPS 服务商允许 TCP $REALITY_PORT 入站。"
yellow "如果运行在容器内，还需要宿主机映射该 TCP 端口。"
yellow "本脚本没有安装、登录或配置 Cloudflared。"
echo
echo "查看日志："
echo "  journalctl -u xray -u node-panel -n 50 --no-pager"
SCRIPT

chmod 700 /root/node-stack-installer.txt
bash /root/node-stack-installer.txt
