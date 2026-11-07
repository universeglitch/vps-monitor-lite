#!/usr/bin/python3
import os, re, json, time, hmac, hashlib, secrets, socket
import subprocess
import ipaddress
import shutil
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, urlencode, quote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from io import BytesIO
import qrcode

AUTH = Path("/etc/node-panel/auth.json")
KEY = Path("/etc/node-panel/secret.key")
XRAY_CONFIG = Path("/usr/local/etc/xray/config.json")
PANEL_CONFIG = Path("/etc/node-panel/panel-config.json")
ALLOWED = re.compile(r"(?i)^/(?:usage|u|admin|ad|a)/?$")

def load_panel_config():
    if not PANEL_CONFIG.exists():
        raise RuntimeError("缺少 /etc/node-panel/panel-config.json")
    cfg = json.loads(PANEL_CONFIG.read_text())
    ws, reality = cfg.get("ws") or {}, cfg.get("reality") or {}
    required = {
        "ws.listen_port": ws.get("listen_port"), "ws.host": ws.get("host"),
        "ws.port": ws.get("port"), "reality.listen_port": reality.get("listen_port"),
        "reality.address": reality.get("address"), "reality.port": reality.get("port"),
        "reality.server_name": reality.get("server_name"),
        "reality.public_key": reality.get("public_key"), "reality.short_id": reality.get("short_id")
    }
    for name, value in required.items():
        if value is None or str(value).strip() == "":
            raise RuntimeError("panel-config.json 缺少配置项：" + name)
    for name, value in (("ws.listen_port", ws["listen_port"]), ("ws.port", ws["port"]),
                        ("reality.listen_port", reality["listen_port"]), ("reality.port", reality["port"])):
        if not str(value).isdigit() or not 1 <= int(value) <= 65535:
            raise RuntimeError("无效端口：" + name)
    if not str(ws.get("path", "/")).startswith("/"):
        raise RuntimeError("ws.path 必须以 / 开头")
    return cfg

panel_config = load_panel_config()
auth = json.loads(AUTH.read_text())
secret_key = KEY.read_bytes()

failures = 0
blocked_until = 0
login_lock = __import__("threading").Lock()

# 网络诊断缓存：只存于进程内存，不写入磁盘
diag_lock = __import__("threading").Lock()
visitor_lookup_cache = {}
server_diag_cache = {"at": 0, "data": None}

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#11152c">
<title>Node Monitor</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;color:#eef0ff;font-family:system-ui,-apple-system,sans-serif;
background:radial-gradient(ellipse at 15% 10%,#44317d 0,transparent 42%),
radial-gradient(ellipse at 90% 85%,#123e78 0,transparent 42%),#0b1020}
main{width:min(900px,92%);margin:42px auto}
.panel{background:#ffffff0d;border:1px solid #ffffff20;border-radius:22px;
padding:24px;backdrop-filter:blur(16px);box-shadow:0 18px 55px #0005}
h1{font-size:25px;margin:0 0 8px}
.sub{color:#aeb8d8;font-size:13px;margin-bottom:22px}
input{width:100%;padding:14px;border-radius:12px;border:1px solid #ffffff30;
background:#0b1020;color:white;font-size:16px;margin:12px 0}
button,.btn{border:0;border-radius:12px;padding:12px 16px;color:white;
background:linear-gradient(110deg,#7855df,#277ce8);font-weight:600;
cursor:pointer;text-decoration:none;display:inline-block}
button.secondary,.btn.secondary{background:#ffffff16;border:1px solid #ffffff24}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
.tile{background:#ffffff0b;border:1px solid #ffffff18;border-radius:15px;padding:16px}
.tile small{display:block;color:#aeb8d8;margin-bottom:10px}
.value{font-size:17px;font-weight:650}
.ok{color:#63e6a2}.bad{color:#ff8585}
.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:15px}
.qr{width:210px;height:210px;object-fit:contain;background:white;padding:9px;border-radius:12px}
.hidden{display:none}
.value{overflow-wrap:anywhere}
.diag-note{color:#aeb8d8;font-size:12px;line-height:1.7;margin-top:10px}
.error{color:#ff9b9b;min-height:20px;font-size:13px}
hr{border:0;border-top:1px solid #ffffff18;margin:22px 0}
@media(max-width:500px){main{margin:18px auto}.panel{padding:18px}}
</style>
</head>
<body><main>
<section class="panel" id="login">
<h1>Node Monitor</h1><div class="sub">安全登录 · 节点状态管理</div>
<form id="loginForm">
<input name="password" type="password" placeholder="面板登录密码" required autocomplete="current-password">
<button type="submit">登录</button>
<div class="error" id="loginError"></div>
</form></section>

<section class="panel hidden" id="dashboard">
<h1>Node Monitor</h1><div class="sub">服务状态与节点配置</div>
<div class="grid">
<div class="tile"><small>Xray 服务</small><div class="value" id="xray">读取中</div></div>
<div class="tile"><small>Cloudflare Tunnel 服务</small><div class="value" id="tunnel">读取中</div></div>
<div class="tile"><small>入口 A · 本地监听</small><div class="value" id="nodeA">读取中</div></div>
<div class="tile"><small>入口 B · 本地监听</small><div class="value" id="nodeB">读取中</div></div>
<div class="tile"><small>内存使用</small><div class="value" id="memory">读取中</div></div>
</div>
<hr>

  <h2 style="font-size:19px">访客网络信息</h2>
  <div class="sub">当前打开面板的设备网络信息</div>
  <div class="grid">
  <div class="tile"><small>访客公网 IP</small><div class="value" id="visitorIp">读取中</div></div>
  <div class="tile"><small>国家 / 地区代码</small><div class="value" id="visitorCountry">读取中</div></div>
  <div class="tile"><small>所在地区</small><div class="value" id="visitorRegion">读取中</div></div>
  <div class="tile"><small>所在城市</small><div class="value" id="visitorCity">读取中</div></div>
  <div class="tile"><small>运营商 ISP</small><div class="value" id="visitorISP">读取中</div></div>
  <div class="tile"><small>ASN</small><div class="value" id="visitorASN">读取中</div></div>
  </div>
  <div class="diag-note" id="visitorNote">位置优先读取 Cloudflare 请求头，ISP 和 ASN 由第三方查询。</div>

  <hr>
  <h2 style="font-size:19px">手机浏览器网络检测</h2>
  <div class="sub">从当前手机直接向目标网站发起网络请求</div>
  <div class="grid" id="browserChecks"></div>
  <div class="diag-note">可达表示请求已完成，但浏览器无法读取跨站 HTTP 状态码；失败也可能是网站策略或浏览器限制，不代表网站一定无法打开。</div>

  <hr>
  <h2 style="font-size:19px">VPS 出站网络检测</h2>
  <div class="sub">由 VPS 自身执行 DNS、TCP 和 HTTPS 检测</div>
  <div class="grid">
  <div class="tile"><small>DNS 解析</small><div class="value" id="serverDns">读取中</div></div>
  <div class="tile"><small>TCP 443</small><div class="value" id="serverTcp">读取中</div></div>
  <div class="tile"><small>HTTPS 出站</small><div class="value" id="serverHttps">读取中</div></div>
  <div class="tile"><small>VPS 出口 IP</small><div class="value" id="serverEgress">读取中</div></div>
  <div class="tile"><small>VPS 出口运营商</small><div class="value" id="serverISP">读取中</div></div>
  <div class="tile"><small>VPS 出口 ASN</small><div class="value" id="serverASN">读取中</div></div>
  </div>
  <div class="actions">
  <button class="secondary" id="refreshNetwork">刷新网络诊断</button>
  <a class="btn secondary" href="https://speed.cloudflare.com/" target="_blank" rel="noopener noreferrer">Cloudflare 测速</a>
  <a class="btn secondary" href="https://www.speedtest.net/" target="_blank" rel="noopener noreferrer">Ookla 测速</a>
  </div>
  <div class="diag-note" id="networkStatus">登录后自动执行一次网络诊断。</div>
  <hr>
  <h2 style="font-size:19px">NekoBox 节点配置</h2>
<div class="sub">WS + TLS（Cloudflare Tunnel）与 REALITY（TCP 直连）独立展示</div>
<div class="grid">
<div class="tile">
<h3 style="margin:0 0 12px">节点 A · VLESS + WS + TLS</h3>
<div style="text-align:center"><img class="qr" id="qrWs" alt="WS 节点二维码"></div>
<div class="actions"><button id="copyWs">复制 WS 分享链接</button>
<a class="btn secondary" id="downloadWs">下载 WS 配置</a></div>
<div class="error" id="configErrorWs"></div>
</div>
<div class="tile">
<h3 style="margin:0 0 12px">节点 B · VLESS + REALITY</h3>
<div style="text-align:center"><img class="qr" id="qrReality" alt="REALITY 节点二维码"></div>
<div class="actions"><button id="copyReality">复制 REALITY 分享链接</button>
<a class="btn secondary" id="downloadReality">下载 REALITY 配置</a></div>
<div class="error" id="configErrorReality"></div>
</div>
</div>
<hr>
<div class="actions"><button class="secondary" id="refresh">刷新状态</button>
<button class="secondary" id="logout">退出登录</button></div>
<div class="sub" style="margin:18px 0 0">节点状态仅反映服务器进程和本地监听情况，不代表客户端端到端连通性。</div>
</section>
</main>
<script>
const $=id=>document.getElementById(id);
const endpoint=location.pathname;
async function request(action){
 const r=await fetch(endpoint+'?action='+encodeURIComponent(action),{cache:'no-store'});
 if(r.status===401){showLogin();throw Error('登录状态已过期');}
 if(!r.ok)throw Error('请求失败');
 return r;
}
function showLogin(){$('dashboard').classList.add('hidden');$('login').classList.remove('hidden');}
function showDash(){$('login').classList.add('hidden');$('dashboard').classList.remove('hidden');}
async function refresh(){
 try{
  const r=await request('status'),s=await r.json();
  const set=(id,v)=>{$(id).textContent=v?'运行中':'异常';$(id).className='value '+(v?'ok':'bad');};
  set('xray',s.xray);set('tunnel',s.tunnel);set('nodeA',s.node_a);set('nodeB',s.node_b);
  $('memory').textContent=s.memory_used+' / '+s.memory_total+' ('+s.memory_percent+'%)';
 }catch(e){}
}
const shareUris={ws:null,reality:null};
async function copyNode(kind){
 const errorId=kind==='ws'?'configErrorWs':'configErrorReality';
 const uri=shareUris[kind];
 if(!uri){$(errorId).textContent='配置尚未加载';return;}
 if(navigator.clipboard && navigator.clipboard.writeText){
  try{await navigator.clipboard.writeText(uri);$(errorId).textContent='已复制分享链接';}
  catch(e){fallbackCopy(uri,errorId);}
 }else{fallbackCopy(uri,errorId);}
}
function fallbackCopy(uri,errorId){
 const box=document.createElement('textarea');
 box.value=uri;box.style.position='fixed';box.style.opacity='0';
 document.body.appendChild(box);box.focus();box.select();
 try{$(errorId).textContent=document.execCommand('copy')?'已复制分享链接':'复制失败，请使用下载配置';}
 catch(e){$(errorId).textContent='复制失败，请使用下载配置';}
 box.remove();
}
async function loadConfig(){
 try{
  const r=await request('config');
  const d=await r.json();
  for(const kind of ['ws','reality']){
   const node=d.nodes && d.nodes[kind];
   if(!node || !node.uri)throw Error('缺少 '+kind+' 节点配置');
   shareUris[kind]=node.uri;
   const suffix=kind==='ws'?'Ws':'Reality';
   $('qr'+suffix).src=endpoint+'?action=qr&node='+kind+'&v='+Date.now();
   $('download'+suffix).href=endpoint+'?action=download&node='+kind;
   $('copy'+suffix).onclick=()=>copyNode(kind);
  }
 }catch(e){
  $('configErrorWs').textContent=e.message;
  $('configErrorReality').textContent=e.message;
 }
}
const browserTargets=[
 {name:'百度',url:'https://www.baidu.com/'},
 {name:'淘宝',url:'https://www.taobao.com/'},
 {name:'Google',url:'https://www.google.com/'},
 {name:'YouTube',url:'https://www.youtube.com/'},
 {name:'GitHub',url:'https://github.com/'},
 {name:'OpenAI',url:'https://chatgpt.com/'},
 {name:'Claude',url:'https://claude.ai/'},
 {name:'Gemini',url:'https://gemini.google.com/'}
];

async function loadVisitor(){
 try{
  const r=await request('visitor');
  const d=await r.json();
  const put=(id,v)=>$(id).textContent=v||'未提供';
  put('visitorIp',d.ip);
  put('visitorCountry',d.country);
  put('visitorRegion',d.region);
  put('visitorCity',d.city);
  put('visitorISP',d.isp);
  put('visitorASN',d.asn);
  put('visitorNote',d.note);
 }catch(e){
  $('visitorNote').textContent='访客信息读取失败：'+e.message;
 }
}

async function loadServerDiagnostics(){
 try{
  const r=await request('serverdiag');
  const d=await r.json();
  const fields=[
   ['serverDns',d.dns],
   ['serverTcp',d.tcp],
   ['serverHttps',d.https]
  ];
  fields.forEach(([id,value])=>{
   const el=$(id);
   el.textContent=value||'不可用';
   el.className='value '+(/^正常|^已连接/.test(value||'')?'ok':'bad');
  });
  const put=(id,v)=>$(id).textContent=v||'未获取';
  put('serverEgress',d.egress_ip);
  put('serverISP',d.egress_isp);
  put('serverASN',d.egress_asn);
 }catch(e){
  $('serverHttps').textContent='检测失败';
  $('serverHttps').className='value bad';
 }
}

async function runBrowserChecks(){
 const root=$('browserChecks');
 root.textContent='';
 const cards=browserTargets.map(t=>{
  const tile=document.createElement('div');
  tile.className='tile';
  const label=document.createElement('small');
  label.textContent=t.name;
  const value=document.createElement('div');
  value.className='value';
  value.textContent='等待检测';
  tile.appendChild(label);
  tile.appendChild(value);
  root.appendChild(tile);
  return value;
 });

 for(let i=0;i<browserTargets.length;i+=3){
  await Promise.all(browserTargets.slice(i,i+3).map(async(t,j)=>{
   const value=cards[i+j];
   const controller=new AbortController();
   const started=performance.now();
   const timer=setTimeout(()=>controller.abort(),4500);
   try{
    const separator=t.url.includes('?')?'&':'?';
    await fetch(t.url+separator+'_node_monitor='+Date.now(),{
     method:'GET',
     mode:'no-cors',
     cache:'no-store',
     signal:controller.signal
    });
    value.textContent='可达 · '+Math.round(performance.now()-started)+' ms';
    value.className='value ok';
   }catch(e){
    value.textContent=e.name==='AbortError'?'超时':'失败 / 浏览器限制';
    value.className='value bad';
   }finally{
    clearTimeout(timer);
   }
  }));
 }
}

async function refreshNetwork(){
 $('networkStatus').textContent='正在执行网络诊断……';
 await Promise.all([
  loadVisitor(),
  loadServerDiagnostics(),
  runBrowserChecks()
 ]);
 $('networkStatus').textContent='网络诊断完成 · '+new Date().toLocaleTimeString();
}

$('loginForm').addEventListener('submit',async e=>{
 e.preventDefault();$('loginError').textContent='';
 const body=new URLSearchParams(new FormData(e.target));
 try{
  const r=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'action=login&'+body.toString()});
  if(r.ok){showDash();refresh();loadConfig();refreshNetwork();}
  else $('loginError').textContent=(await r.text())||'登录失败';
 }catch(e){$('loginError').textContent='连接失败';}
});
$('refresh').onclick=refresh;
$('refreshNetwork').onclick=refreshNetwork;
$('logout').onclick=async()=>{
 await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'action=logout'});
 showLogin();
};
(async()=>{
 try{const r=await request('status');if(r.ok){showDash();refresh();loadConfig();refreshNetwork();}}
 catch(e){showLogin();}
})();
</script></body></html>"""

def session_token():
    expiry = str(int(time.time()) + 43200)
    nonce = secrets.token_hex(16)
    raw = expiry + "." + nonce
    sig = hmac.new(secret_key, raw.encode(), hashlib.sha256).hexdigest()
    return raw + "." + sig

def valid_session(handler):
    cookie = SimpleCookie()
    try:
        cookie.load(handler.headers.get("Cookie", ""))
        token = cookie["node_session"].value
        expiry, nonce, sig = token.split(".")
        raw = expiry + "." + nonce
        expected = hmac.new(secret_key, raw.encode(), hashlib.sha256).hexdigest()
        return int(expiry) > int(time.time()) and hmac.compare_digest(sig, expected)
    except Exception:
        return False

def service_active(name):
    try:
        result = subprocess.run(
            ["/bin/systemctl", "is-active", "--quiet", name],
            timeout=2
        )
        return result.returncode == 0
    except Exception:
        return False

def port_active(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False

def get_status():
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        mem[k] = int(v.strip().split()[0])
    total = mem.get("MemTotal", 0)
    available = mem.get("MemAvailable", 0)
    used = max(0, total - available)
    return {
        "xray": service_active("xray"),
        "tunnel": service_active("cloudflared"),
        "node_a": port_active(int(panel_config["ws"]["listen_port"])),
        "node_b": port_active(int(panel_config["reality"]["listen_port"])),
        "memory_used": str(round(used / 1024)) + " MB",
        "memory_total": str(round(total / 1024)) + " MB",
        "memory_percent": round(used * 100 / total) if total else 0
    }


def find_vless_inbound(port):
    data = json.loads(XRAY_CONFIG.read_text())
    for inbound in data.get("inbounds", []):
        if inbound.get("port") == int(port) and inbound.get("protocol") == "vless":
            return inbound
    raise RuntimeError("未找到端口为 " + str(port) + " 的 VLESS 入站")


def get_uris():
    ws_cfg, reality_cfg = panel_config["ws"], panel_config["reality"]
    ws_inbound = find_vless_inbound(ws_cfg["listen_port"])
    ws_clients = ws_inbound.get("settings", {}).get("clients", [])
    ws_stream = ws_inbound.get("streamSettings", {})
    ws_settings = ws_stream.get("wsSettings", {})
    ws_uid = ws_clients[0].get("id") if ws_clients else None
    ws_path = ws_settings.get("path") or ws_cfg.get("path")
    if not ws_uid or not ws_path or ws_stream.get("network") != "ws":
        raise RuntimeError("WS 入站配置不完整或传输类型不是 ws")
    ws_host, ws_port = str(ws_cfg["host"]).strip(), int(ws_cfg["port"])
    ws_query = urlencode({
        "encryption": "none", "security": "tls", "sni": ws_host,
        "fp": "chrome", "type": "ws", "host": ws_host, "path": ws_path
    }, quote_via=quote)
    ws_uri = f"vless://{ws_uid}@{ws_host}:{ws_port}?{ws_query}#Node-A-WS"

    reality_inbound = find_vless_inbound(reality_cfg["listen_port"])
    reality_clients = reality_inbound.get("settings", {}).get("clients", [])
    reality_stream = reality_inbound.get("streamSettings", {})
    reality_uid = reality_clients[0].get("id") if reality_clients else None
    if not reality_uid or reality_stream.get("security") != "reality":
        raise RuntimeError("REALITY 入站配置不完整或安全类型不是 reality")
    reality_host, reality_port = str(reality_cfg["address"]).strip(), int(reality_cfg["port"])
    reality_query = urlencode({
        "encryption": "none", "security": "reality",
        "sni": str(reality_cfg["server_name"]),
        "fp": str(reality_cfg.get("fingerprint", "chrome")),
        "pbk": str(reality_cfg["public_key"]), "sid": str(reality_cfg["short_id"]),
        "type": str(reality_cfg.get("network", "tcp")),
        "flow": str(reality_cfg.get("flow", "xtls-rprx-vision"))
    }, quote_via=quote)
    reality_uri = f"vless://{reality_uid}@{reality_host}:{reality_port}?{reality_query}#Node-B-REALITY"
    return {"ws": ws_uri, "reality": reality_uri}


def lookup_visitor_network(ip):
    now = time.time()
    with diag_lock:
        cached = visitor_lookup_cache.get(ip)
        if cached and now - cached[0] < cached[1]:
            return dict(cached[2])

    result = {
        "country": "",
        "region": "",
        "city": "",
        "isp": "",
        "asn": ""
    }

    try:
        req = urllib.request.Request(
            "https://ipwho.is/" + ip,
            headers={"User-Agent": "NodeMonitor/1.0", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            payload = json.loads(response.read(32768).decode("utf-8", "replace"))

        if payload.get("success"):
            conn = payload.get("connection") or {}
            if not isinstance(conn, dict):
                conn = {}

            asn = conn.get("asn") or conn.get("asn_number") or ""
            org = conn.get("org") or conn.get("asn_org") or ""

            result["country"] = str(payload.get("country") or "")[:80]
            result["region"] = str(payload.get("region") or "")[:100]
            result["city"] = str(payload.get("city") or "")[:100]
            result["isp"] = str(conn.get("isp") or "")[:120]

            asn_text = ("AS" + str(asn)) if asn else ""
            if org:
                asn_text += (" · " if asn_text else "") + str(org)
            result["asn"] = asn_text[:160]
    except Exception:
        pass

    ttl = 600 if (result["isp"] or result["asn"]) else 60
    with diag_lock:
        if len(visitor_lookup_cache) >= 64 and ip not in visitor_lookup_cache:
            oldest = min(visitor_lookup_cache, key=lambda key: visitor_lookup_cache[key][0])
            visitor_lookup_cache.pop(oldest, None)
        visitor_lookup_cache[ip] = (now, ttl, dict(result))

    return result


def get_visitor_info(handler):
    note = (
        "访客 IP 来自 Cloudflare CF-Connecting-IP；"
        "ISP/ASN 及缺失的地区信息由 ipwho.is 查询。"
        "访客公网 IP 会发送给该查询服务；查询结果仅临时缓存在 VPS 内存中。"
    )

    raw_ip = (handler.headers.get("CF-Connecting-IP") or "").strip()

    try:
        address = ipaddress.ip_address(raw_ip)
    except ValueError:
        return {
            "ip": "未获取（请确认请求经 Cloudflare 代理）",
            "country": "未提供",
            "region": "未提供",
            "city": "未提供",
            "isp": "未提供",
            "asn": "未提供",
            "note": note
        }

    if not address.is_global:
        return {
            "ip": "非公网地址",
            "country": "未提供",
            "region": "未提供",
            "city": "未提供",
            "isp": "未提供",
            "asn": "未提供",
            "note": note
        }

    ip = str(address)
    lookup = lookup_visitor_network(ip)

    country = (handler.headers.get("CF-IPCountry") or "").strip()
    region = (handler.headers.get("CF-Region") or "").strip()
    city = (handler.headers.get("CF-IPCity") or "").strip()
    region_code = (handler.headers.get("CF-Region-Code") or "").strip()

    if region_code and region and region_code not in region:
        region = region + " (" + region_code + ")"
    elif not region:
        region = lookup.get("region", "")

    return {
        "ip": ip,
        "country": (country or lookup.get("country") or "未提供")[:100],
        "region": (region or "未提供")[:120],
        "city": (city or lookup.get("city") or "未提供")[:120],
        "isp": lookup.get("isp") or "未获取",
        "asn": lookup.get("asn") or "未获取",
        "note": note
    }


def get_server_diagnostics():
    now = time.monotonic()
    with diag_lock:
        cached = server_diag_cache.get("data")
        if cached and now - server_diag_cache.get("at", 0) < 60:
            return dict(cached)

    result = {
        "dns": "未检测",
        "tcp": "未检测",
        "https": "未检测",
        "egress_ip": "",
        "egress_isp": "",
        "egress_asn": ""
    }

    # DNS 查询：使用 getent 并设置硬超时
    resolved_ip = ""
    try:
        getent = shutil.which("getent")
        if not getent:
            raise FileNotFoundError("getent unavailable")

        proc = subprocess.run(
            [getent, "ahosts", "www.cloudflare.com"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False
        )

        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                parts = line.split()
                if not parts:
                    continue
                try:
                    ipaddress.ip_address(parts[0])
                    resolved_ip = parts[0]
                    break
                except ValueError:
                    continue

        result["dns"] = (
            "正常 · " + resolved_ip if resolved_ip
            else "失败 · 未取得解析结果"
        )
    except subprocess.TimeoutExpired:
        result["dns"] = "失败 · DNS 超时"
    except Exception:
        result["dns"] = "失败 · DNS 检测工具不可用"

    # TCP 连接测试：直接连接 DNS 解析出的 IP，避免再次依赖域名解析
    if resolved_ip:
        try:
            with socket.create_connection((resolved_ip, 443), timeout=2.5):
                result["tcp"] = "正常 · TCP 443"
        except Exception as exc:
            result["tcp"] = "失败 · " + type(exc).__name__
    else:
        result["tcp"] = "跳过 · DNS 未成功"

    # HTTPS 测试：Cloudflare trace
    try:
        req = urllib.request.Request(
            "https://www.cloudflare.com/cdn-cgi/trace",
            headers={"User-Agent": "NodeMonitor/1.0"}
        )
        with urllib.request.urlopen(req, timeout=4) as response:
            response.read(2048)
            result["https"] = "正常 · HTTP " + str(response.status)
    except Exception as exc:
        result["https"] = "失败 · " + type(exc).__name__

    # VPS 出口 IP、ISP 和 ASN 查询
    try:
        req = urllib.request.Request(
            "https://ipwho.is/",
            headers={"User-Agent": "NodeMonitor/1.0", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            payload = json.loads(response.read(32768).decode("utf-8", "replace"))

        if payload.get("success"):
            conn = payload.get("connection") or {}
            if not isinstance(conn, dict):
                conn = {}

            result["egress_ip"] = str(payload.get("ip") or "")[:100]
            result["egress_isp"] = str(conn.get("isp") or "")[:120]

            asn = conn.get("asn") or conn.get("asn_number") or ""
            org = conn.get("org") or conn.get("asn_org") or ""
            asn_text = ("AS" + str(asn)) if asn else ""
            if org:
                asn_text += (" · " if asn_text else "") + str(org)
            result["egress_asn"] = asn_text[:160]
    except Exception:
        pass

    with diag_lock:
        server_diag_cache["at"] = time.monotonic()
        server_diag_cache["data"] = dict(result)

    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_body(self, code, body, content_type="text/plain; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode())

    def authorized(self):
        if valid_session(self):
            return True
        self.send_body(401, "Unauthorized")
        return False

    def do_GET(self):
        global failures, blocked_until
        parsed = urlsplit(self.path)
        if not ALLOWED.fullmatch(parsed.path):
            self.send_response(302)
            self.send_header("Location", secrets.choice([
                "https://eo.52013130.xyz", "https://if.camoo.cyou"
            ]))
            self.end_headers()
            return

        action = parse_qs(parsed.query).get("action", [""])[0]
        if action == "logout":
            self.send_body(405, "Method not allowed")
            return
        if not action:
            self.send_body(200, PAGE, "text/html; charset=utf-8")
            return
        if not self.authorized():
            return
        try:
            if action == "status":
                self.send_body(200, json.dumps(get_status()), "application/json")
            elif action == "visitor":
                self.send_body(
                    200,
                    json.dumps(get_visitor_info(self), ensure_ascii=False),
                    "application/json"
                )
            elif action == "serverdiag":
                self.send_body(
                    200,
                    json.dumps(get_server_diagnostics(), ensure_ascii=False),
                    "application/json"
                )
            elif action == "config":
                uris = get_uris()
                self.send_body(200, json.dumps({"nodes": {
                    "ws": {"uri": uris["ws"]},
                    "reality": {"uri": uris["reality"]}
                }}, ensure_ascii=False), "application/json")
            elif action in ("qr", "download"):
                node = parse_qs(parsed.query).get("node", ["ws"])[0].lower()
                if node not in ("ws", "reality"):
                    self.send_body(400, "Invalid node")
                    return
                uri = get_uris()[node]
                if action == "qr":
                    img = qrcode.make(uri)
                    buf = BytesIO()
                    img.save(buf, format="PNG")
                    self.send_body(200, buf.getvalue(), "image/png")
                else:
                    filename = "node-ws.txt" if node == "ws" else "node-reality.txt"
                    self.send_body(200, uri + "\\n", "text/plain; charset=utf-8",
                                   {"Content-Disposition": f'attachment; filename="{filename}"'})
            else:
                self.send_body(404, "Not found")
        except Exception:
            self.send_body(500, "配置读取失败，请检查服务器配置")

    def do_POST(self):
        global failures, blocked_until
        parsed = urlsplit(self.path)
        if not ALLOWED.fullmatch(parsed.path):
            self.send_body(404, "Not found")
            return
        length = min(int(self.headers.get("Content-Length", "0")), 4096)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        action = form.get("action", [""])[0]

        if action == "login":
            with login_lock:
                now = time.time()
                if now < blocked_until:
                    self.send_body(429, "尝试次数过多，请稍后再试")
                    return
                password = form.get("password", [""])[0]
                candidate = hashlib.pbkdf2_hmac(
                    "sha256", password.encode(), bytes.fromhex(auth["salt"]),
                    int(auth["iterations"])
                ).hex()
                if hmac.compare_digest(candidate, auth["hash"]):
                    failures = 0
                    token = session_token()
                    self.send_body(200, "OK", headers={
                        "Set-Cookie": f"node_session={token}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=43200"
                    })
                else:
                    failures += 1
                    if failures >= 5:
                        failures = 0
                        blocked_until = now + 300
                    self.send_body(401, "密码错误")
            return

        if action == "logout":
            self.send_body(200, "OK", headers={
                "Set-Cookie": "node_session=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0"
            })
            return
        self.send_body(404, "Not found")

    def do_HEAD(self):
        self.send_body(405, "Method not allowed")

if __name__ == "__main__":
    panel_port = int(panel_config.get("panel_port", 8765))
    if not 1 <= panel_port <= 65535:
        raise RuntimeError("panel_port 必须在 1–65535 之间")
    server = ThreadingHTTPServer(("127.0.0.1", panel_port), Handler)
    server.serve_forever()
