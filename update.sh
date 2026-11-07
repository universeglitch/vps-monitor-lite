#!/usr/bin/env bash
set -Eeuo pipefail

# Node Monitor single-node -> dual-node panel updater.
# Only updates /opt/node-panel/panel.py and /etc/node-panel/panel-config.json.
# Does NOT modify Xray, UUIDs, REALITY private keys, auth.json, secret.key or cloudflared.

PANEL_DIR="/opt/node-panel"
PANEL_FILE="$PANEL_DIR/panel.py"
CONFIG_DIR="/etc/node-panel"
PANEL_CONFIG="$CONFIG_DIR/panel-config.json"
XRAY_CONFIG="/usr/local/etc/xray/config.json"
SERVICE="node-panel"
PANEL_URL="https://raw.githubusercontent.com/universeglitch/vps-monitor-lite/main/panel_dual.py"
TMP_PANEL="/tmp/panel_dual.update.$$"
TMP_CONFIG="/tmp/panel-config.update.$$.json"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="/var/backups/node-panel-update-$STAMP"

say() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
die() { printf '\n错误：%s\n' "$*" >&2; exit 1; }

rollback() {
  local rc=$?
  if [[ ${UPGRADE_COMMITTED:-0} != 1 ]]; then
    echo "升级未完成，正在尝试恢复旧面板……" >&2
    if [[ -f "$BACKUP_DIR/panel.py" ]]; then
      cp -a "$BACKUP_DIR/panel.py" "$PANEL_FILE" || true
    fi
    if [[ -f "$BACKUP_DIR/panel-config.json" ]]; then
      cp -a "$BACKUP_DIR/panel-config.json" "$PANEL_CONFIG" || true
    elif [[ -f "$BACKUP_DIR/panel-config-was-absent" ]]; then
      rm -f "$PANEL_CONFIG" || true
    fi
    systemctl restart "$SERVICE" >/dev/null 2>&1 || true
    echo "回滚检查完成。备份目录：$BACKUP_DIR" >&2
  fi
  rm -f "$TMP_PANEL" "$TMP_CONFIG"
  exit "$rc"
}
trap rollback EXIT

[[ $EUID -eq 0 ]] || die "请使用 root 执行：bash update.sh"

for cmd in python3 curl systemctl xray; do
  command -v "$cmd" >/dev/null 2>&1 || die "缺少命令：$cmd"
done

[[ -d "$PANEL_DIR" && -f "$PANEL_FILE" ]] || die "未找到旧面板：$PANEL_FILE"
[[ -f "$CONFIG_DIR/auth.json" ]] || die "缺少认证文件：$CONFIG_DIR/auth.json；为避免重置登录信息，停止升级"
[[ -f "$CONFIG_DIR/secret.key" ]] || die "缺少密钥文件：$CONFIG_DIR/secret.key；为避免破坏会话认证，停止升级"
[[ -f "$XRAY_CONFIG" ]] || die "未找到 Xray 配置：$XRAY_CONFIG；此脚本要求目标机器已有 Xray 双入站配置"
systemctl cat "$SERVICE" >/dev/null 2>&1 || die "未找到 systemd 服务：$SERVICE"
grep -Fq "$PANEL_FILE" <(systemctl cat "$SERVICE") || die "$SERVICE 的启动命令不是 $PANEL_FILE，不能安全自动替换"

python3 - <<'PY' || die "认证文件格式检查失败"
import json
p='/etc/node-panel/auth.json'
with open(p) as f: d=json.load(f)
assert all(k in d for k in ('salt','iterations','hash'))
with open('/etc/node-panel/secret.key','rb') as f: assert len(f.read()) >= 16
PY

say "检查旧面板和 Xray 配置"
python3 - <<'PY' || die "Xray 配置 JSON 无效"
import json
with open('/usr/local/etc/xray/config.json') as f: json.load(f)
PY

# Ask for the public WS hostname; this must be the hostname clients use through Cloudflare.
read -r -p "请输入这台机器的 WS 公网域名（例如 ws.example.com）：" WS_HOST
[[ -n "$WS_HOST" ]] || die "WS 域名不能为空"
if [[ ! "$WS_HOST" =~ ^[A-Za-z0-9.-]+$ || "$WS_HOST" == .* || "$WS_HOST" == *..* ]]; then
  die "WS 域名格式不正确"
fi

read -r -p "WS 公网端口 [443]：" WS_PUBLIC_PORT
WS_PUBLIC_PORT=${WS_PUBLIC_PORT:-443}
[[ "$WS_PUBLIC_PORT" =~ ^[0-9]+$ ]] && (( WS_PUBLIC_PORT >= 1 && WS_PUBLIC_PORT <= 65535 )) || die "WS 公网端口无效"

AUTO_IP=""
if command -v curl >/dev/null 2>&1; then
  AUTO_IP="$(curl -4fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
fi
if [[ -n "$AUTO_IP" ]]; then
  read -r -p "REALITY 客户端连接地址 [$AUTO_IP]（回车使用自动检测值）：" REALITY_ADDRESS
  REALITY_ADDRESS=${REALITY_ADDRESS:-$AUTO_IP}
else
  read -r -p "无法自动获取公网 IP，请输入 REALITY 客户端连接地址：" REALITY_ADDRESS
fi
[[ -n "$REALITY_ADDRESS" ]] || die "REALITY 连接地址不能为空"

read -r -p "面板监听端口 [8765]：" PANEL_PORT
PANEL_PORT=${PANEL_PORT:-8765}
[[ "$PANEL_PORT" =~ ^[0-9]+$ ]] && (( PANEL_PORT >= 1 && PANEL_PORT <= 65535 )) || die "面板端口无效"

say "下载新版面板"
curl -fL --retry 3 --connect-timeout 10 --max-time 90 "$PANEL_URL" -o "$TMP_PANEL"
[[ -s "$TMP_PANEL" ]] || die "下载的新版面板为空"
python3 -m py_compile "$TMP_PANEL" || die "新版面板 Python 语法检查失败"

if ! python3 -c 'import qrcode' >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    say "安装二维码运行依赖 python3-qrcode"
    apt-get update
    apt-get install -y --no-install-recommends python3-qrcode python3-pil
  else
    die "缺少 Python qrcode 模块，且系统没有 apt-get；请先安装对应依赖"
  fi
fi

say "从现有 Xray 配置提取节点参数并生成面板配置"
WS_HOST="$WS_HOST" WS_PUBLIC_PORT="$WS_PUBLIC_PORT" REALITY_ADDRESS="$REALITY_ADDRESS" PANEL_PORT="$PANEL_PORT" python3 - "$XRAY_CONFIG" "$TMP_CONFIG" <<'PY'
import json, os, re, subprocess, sys
src, dst = sys.argv[1:]
with open(src) as f: data=json.load(f)
inbounds=data.get('inbounds', [])
ws=[]; reality=[]
for item in inbounds:
    if item.get('protocol') != 'vless':
        continue
    stream=item.get('streamSettings') or {}
    if stream.get('network') == 'ws':
        ws.append(item)
    if stream.get('security') == 'reality':
        reality.append(item)
if len(ws) != 1:
    raise SystemExit(f'要求恰好一个 VLESS+WS 入站，实际找到 {len(ws)} 个；停止以避免选错节点')
if len(reality) != 1:
    raise SystemExit(f'要求恰好一个 VLESS+REALITY 入站，实际找到 {len(reality)} 个；停止以避免选错节点')
w, r = ws[0], reality[0]
wstream=w.get('streamSettings') or {}
wpath=(wstream.get('wsSettings') or {}).get('path')
if not wpath or not str(wpath).startswith('/'):
    raise SystemExit('WS 入站缺少有效的 wsSettings.path')
rstream=r.get('streamSettings') or {}
rsettings=rstream.get('realitySettings') or {}
private=rsettings.get('privateKey')
if not private:
    raise SystemExit('REALITY 配置中没有 privateKey，无法安全计算公钥')
servers=rsettings.get('serverNames') or []
shorts=rsettings.get('shortIds') or []
clients=(r.get('settings') or {}).get('clients') or []
if not servers or not shorts or not clients:
    raise SystemExit('REALITY 缺少 serverNames、shortIds 或 clients')
# Only the public key is retained; the private key is never printed or written to panel-config.json.
proc=subprocess.run(['xray','x25519','-i',private],capture_output=True,text=True,timeout=15)
if proc.returncode:
    raise SystemExit('xray x25519 公钥计算失败：'+(proc.stderr or proc.stdout).strip()[:300])
match=re.search(r'(?:Password\s*\(PublicKey\)|PublicKey|Public key)\s*:\s*([A-Za-z0-9_-]+)', proc.stdout+'\n'+proc.stderr, re.I)
if not match:
    raise SystemExit('无法从 xray x25519 输出中识别公钥字段')
network=str(rstream.get('network') or 'raw').lower()
if network == 'raw': network='tcp'
if network not in ('tcp',):
    raise SystemExit(f'当前 REALITY network={network}，此版面板仅按 TCP 生成节点，停止升级')
flow=str(clients[0].get('flow') or '')
config={
  'panel_port':int(os.environ['PANEL_PORT']),
  'ws':{
    'listen_port':int(w['port']),
    'host':os.environ['WS_HOST'],
    'port':int(os.environ['WS_PUBLIC_PORT']),
    'path':str(wpath)
  },
  'reality':{
    'listen_port':int(r['port']),
    'address':os.environ['REALITY_ADDRESS'],
    'port':int(r['port']),
    'server_name':str(servers[0]),
    'public_key':match.group(1),
    'short_id':str(shorts[0]),
    'fingerprint':'chrome',
    'network':'tcp'
  }
}
if flow:
    config['reality']['flow']=flow
with open(dst,'w') as f: json.dump(config,f,ensure_ascii=False,indent=2)
print('WS 入站端口:', w['port'])
print('WS 路径:', wpath)
print('REALITY 入站端口:', r['port'])
print('REALITY SNI:', servers[0])
print('REALITY Flow:', flow or '(未设置)')
print('REALITY 公钥已在本机计算（不显示私钥）')
PY

python3 -m json.tool "$TMP_CONFIG" >/dev/null || die "生成的 panel-config.json 无效"

say "备份现有面板与面板配置"
mkdir -p "$BACKUP_DIR"
cp -a "$PANEL_FILE" "$BACKUP_DIR/panel.py"
if [[ -f "$PANEL_CONFIG" ]]; then
  cp -a "$PANEL_CONFIG" "$BACKUP_DIR/panel-config.json"
else
  touch "$BACKUP_DIR/panel-config-was-absent"
fi
chmod 700 "$BACKUP_DIR"

say "安装新版面板与配置"
install -o root -g root -m 644 "$TMP_PANEL" "$PANEL_FILE"
install -o root -g root -m 600 "$TMP_CONFIG" "$PANEL_CONFIG"

say "重启面板并验证"
systemctl restart "$SERVICE"
sleep 2
if ! systemctl is-active --quiet "$SERVICE"; then
  systemctl status "$SERVICE" --no-pager -l || true
  journalctl -u "$SERVICE" -n 40 --no-pager || true
  die "新版面板未能正常启动，触发自动回滚"
fi
HTTP_CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${PANEL_PORT}/usage" || true)"
if [[ "$HTTP_CODE" != "200" ]]; then
  echo "本机 /usage HTTP 状态码：${HTTP_CODE:-无响应}" >&2
  die "面板 HTTP 检查未通过，触发自动回滚"
fi

UPGRADE_COMMITTED=1
trap - EXIT
rm -f "$TMP_PANEL" "$TMP_CONFIG"

say "升级成功"
echo "面板服务：$(systemctl is-active "$SERVICE")"
echo "本机 HTTP：$HTTP_CODE"
echo "备份目录：$BACKUP_DIR"
echo "认证文件未改动：$CONFIG_DIR/auth.json、$CONFIG_DIR/secret.key"
echo "Xray 与 cloudflared 未重启、未修改"
echo "访问路径：原面板域名/usage"
