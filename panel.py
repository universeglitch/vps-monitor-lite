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

AUTH = Path("/etc/node-panel/auth.json")
KEY = Path("/etc/node-panel/secret.key")
XRAY_CONFIG = Path("/usr/local/etc/xray/config.json")
PANEL_CONFIG = Path("/etc/node-panel/panel-config.json")
ALLOWED = re.compile(r"(?i)^/(?:usage|u|admin|ad|a)/?$")

def load_panel_config():
    if not PANEL_CONFIG.exists():
        raise RuntimeError("缺少 /etc/node-panel/panel-config.json")

    cfg = json.loads(PANEL_CONFIG.read_text())

    ws = cfg.get("ws") or {}
    reality = cfg.get("reality") or {}

    reality_enabled = cfg.get("reality_enabled", bool(reality))
    cfg["reality_enabled"] = bool(reality_enabled)

    required = {
        "ws.listen_port": ws.get("listen_port"),
        "ws.host": ws.get("host"),
        "ws.port": ws.get("port")
    }

    if reality_enabled:
        required.update({
            "reality.listen_port": reality.get("listen_port"),
            "reality.address": reality.get("address"),
            "reality.port": reality.get("port"),
            "reality.server_name": reality.get("server_name"),
            "reality.public_key": reality.get("public_key"),
            "reality.short_id": reality.get("short_id")
        })

    for name, value in required.items():
        if value is None or str(value).strip() == "":
            raise RuntimeError("panel-config.json 缺少配置项：" + name)

    ports = [
        ("ws.listen_port", ws["listen_port"]),
        ("ws.port", ws["port"])
    ]

    if reality_enabled:
        ports.extend([
            ("reality.listen_port", reality["listen_port"]),
            ("reality.port", reality["port"])
        ])

    for name, value in ports:
        if not str(value).isdigit() or not 1 <= int(value) <= 65535:
            raise RuntimeError("无效端口：" + name)

    if not str(ws.get("path", "/")).startswith("/"):
        raise RuntimeError("ws.path 必须以 / 开头")

    return cfg

# Loaded only by explicit compatibility helpers, never at import/startup.
panel_config = {}

def authorize(coordinator, token):
    """Detailed panel uses the same auth.py Session/epoch authority as main."""
    return coordinator.auth.validate(token)


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
.qr-cover{position:relative;width:228px;min-height:228px;margin:auto;background:repeating-conic-gradient(#393451 0% 25%,#24283d 0% 50%) 50%/18px 18px;border-radius:14px;display:grid;place-items:center}.qr-cover button{position:absolute;z-index:1}.qr-cover .qr{visibility:hidden;filter:blur(16px)}.qr-cover.revealed .qr{visibility:visible;filter:none}.qr-cover.revealed button{opacity:.8;bottom:0}.qr{width:210px;height:210px;object-fit:contain;background:white;padding:9px;border-radius:12px}
.hidden{display:none}
.value{overflow-wrap:anywhere}
.diag-note{color:#aeb8d8;font-size:12px;line-height:1.7;margin-top:10px}
.error{color:#ff9b9b;min-height:20px;font-size:13px}
hr{border:0;border-top:1px solid #ffffff18;margin:22px 0}
@media(max-width:500px){main{margin:18px auto}.panel{padding:18px}}
</style>
</head>
<body><main>
<section class="panel" id="dashboard">
<h1>Node Monitor</h1><div class="sub">服务状态与节点配置</div><section class="tile" id="unifiedServices"><h2>主面板服务管理</h2><div id="detailServices"></div><div class="actions"><button id="detailAll">一键开关</button><button id="detailModules">模块管理</button><button id="defaultPanel">设为默认面板</button><button id="switchMain">切换面板</button></div><div id="detailOperation" class="diag-note"></div></section>
<section class="tile"><h2>服务端口管理</h2><div id="portList"></div><small>仅列出真实配置；修改必须经过候选验证及绑定事务的重启确认。</small></section>

<div class="grid">
<div class="tile"><small>Xray 服务</small><div class="value" id="xray">读取中</div></div>
<div class="tile"><small>Cloudflare Tunnel 服务</small><div class="value" id="tunnel">读取中</div></div>
<div class="tile"><small>入口 A · 本地监听</small><div class="value" id="nodeA">读取中</div></div>
<div class="tile" id="nodeBTile"><small>入口 B · 本地监听</small><div class="value" id="nodeB">读取中</div></div><div class="tile"><small>内存使用</small><div class="value" id="memory">读取中</div></div>
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
  <div id="nodesSection" class="hidden"><h2 style="font-size:19px">NekoBox 节点配置</h2><div class="sub">仅展示真实、完整的节点配置；二维码需点击眼睛后才从服务端获取。</div><div class="grid" id="nodeCards"></div></div>
<hr>
<div class="actions"><button class="secondary" id="refresh">刷新状态</button>
<button class="secondary" id="logout">退出登录</button></div>
<div class="sub" style="margin:18px 0 0">节点状态仅反映服务器进程和本地监听情况，不代表客户端端到端连通性。</div>
</section>
</main>
<script>
const $=id=>root.getElementById(id);
const endpoint=location.pathname;
async function request(action){return bridge.detail(action)}
function showLogin(){bridge.expired()}
function showDash(){}
async function refresh(){
 try{const s=await (await request('status')).json();const set=(id,v)=>{if(!$(id))return;$(id).textContent=v?'运行中':'未运行';$(id).className='value '+(v?'ok':'bad')};
 set('xray',s.xray);set('tunnel',s.tunnel);set('nodeA',s.node_a);$('nodeBTile').style.display=s.reality_enabled?'':'none';if(s.reality_enabled)set('nodeB',s.node_b);$('memory').textContent=s.memory_used+' / '+s.memory_total+' ('+s.memory_percent+'%)';
 await serviceBlock(); await ports();}catch(e){$('detailOperation').textContent=e.message}
}
function actionButton(label, fn){let b=document.createElement('button');b.textContent=label;b.onclick=async()=>{try{await fn()}catch(e){$('detailOperation').textContent=e.message}};return b}
async function serviceBlock(){let state=await bridge.call('/modules',{});$('detailServices').replaceChildren();for(let m of state.modules.filter(m=>m.visible)){let row=document.createElement('div');row.className='actions';let label=document.createElement('span');label.textContent=m.name+' · '+m.state;row.append(label,actionButton(m.active?'关闭':'开启',async()=>{await bridge.toggle(m);await refresh()}));$('detailServices').append(row)}let monitor=document.createElement('div');monitor.className='actions';monitor.append(actionButton('测活：'+(state.heartbeat?'开':'关'),async()=>{await bridge.call('/heartbeat',{enabled:!state.heartbeat});await refresh()}));$('detailServices').append(monitor)}
async function ports(){let d=await bridge.call('/ports',{});$('portList').replaceChildren();for(let p of d.ports){let row=document.createElement('div');row.className='actions';let text=document.createElement('span');text.textContent=p.name+' · '+p.port;row.append(text,actionButton('修改端口',()=>bridge.port(p)));$('portList').append(row)}}
async function loadConfig(){let d=await bridge.call('/nodes',{});$('nodeCards').replaceChildren();$('nodesSection').classList.toggle('hidden',d.nodes.length===0);for(let node of d.nodes){let card=document.createElement('section');card.className='tile';let title=document.createElement('h3');title.textContent=node.name;let info=document.createElement('p');info.textContent=node.type+' · '+node.address+':'+node.port;card.append(title,info,actionButton('修改名称',async()=>{let name=prompt('节点显示名称',node.name);if(name===null)return;await bridge.call('/node/name',{id:node.id,name});await loadConfig()}));let cover=document.createElement('div');cover.className='qr-cover';let image=document.createElement('img');image.className='qr';image.alt='隐藏的节点二维码';let eye=actionButton('◉ 显示二维码',async()=>{if(image.hasAttribute('src')){image.removeAttribute('src');cover.classList.remove('revealed');eye.textContent='◉ 显示二维码';return}let qr=await bridge.call('/node/qr',{id:node.id});image.src='data:image/svg+xml;base64,'+qr.image;cover.classList.add('revealed');eye.textContent='◉ 隐藏二维码'});cover.append(image,eye);card.append(cover);let actions=document.createElement('div');actions.className='actions';actions.append(actionButton('复制分享链接',async()=>{let d=await bridge.call('/node/share',{id:node.id});await navigator.clipboard.writeText(d.uri)}),actionButton('下载节点配置',async()=>{let d=await bridge.call('/node/share',{id:node.id});let url=URL.createObjectURL(new Blob([d.uri+'\n'],{type:'text/plain'}));let a=document.createElement('a');a.href=url;a.download='node-'+node.id+'.txt';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}));card.append(actions);$('nodeCards').append(card)}}
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

$('refresh').onclick=()=>{refresh();loadConfig()};
$('refreshNetwork').onclick=refreshNetwork;
$('logout').onclick=bridge.logout;
$('switchMain').onclick=()=>bridge.switch('main');
$('defaultPanel').onclick=()=>bridge.call('/preferences',{default_panel:'panel'}).then(()=>$('detailOperation').textContent='详细面板已设为默认');
$('detailModules').onclick=bridge.modules;
$('detailAll').onclick=bridge.all;
refresh();loadConfig().catch(e=>{$('detailOperation').textContent=e.message});
// Network diagnostics run once per browser session, not on every switch.
if(!bridge.diagnosed){bridge.diagnosed=true;refreshNetwork()}
</script></body></html>"""

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
        "node_b": (
            port_active(int(panel_config["reality"]["listen_port"]))
            if panel_config["reality_enabled"] else False
        ),
        "reality_enabled": panel_config["reality_enabled"],
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
    ws_cfg = panel_config["ws"]
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

    result = {"ws": ws_uri}

    if panel_config["reality_enabled"]:
        reality_cfg = panel_config["reality"]
        reality_inbound = find_vless_inbound(reality_cfg["listen_port"])
        reality_clients = reality_inbound.get("settings", {}).get("clients", [])
        reality_stream = reality_inbound.get("streamSettings", {})
        reality_uid = reality_clients[0].get("id") if reality_clients else None

        if not reality_uid or reality_stream.get("security") != "reality":
            raise RuntimeError("REALITY 入站配置不完整或安全类型不是 reality")

        reality_host = str(reality_cfg["address"]).strip()
        reality_port = int(reality_cfg["port"])
        reality_query = urlencode({
            "encryption": "none", "security": "reality",
            "sni": str(reality_cfg["server_name"]),
            "fp": str(reality_cfg.get("fingerprint", "chrome")),
            "pbk": str(reality_cfg["public_key"]),
            "sid": str(reality_cfg["short_id"]),
            "type": str(reality_cfg.get("network", "tcp")),
            "flow": str(reality_cfg.get("flow", "xtls-rprx-vision"))
        }, quote_via=quote)

        result["reality"] = (
            f"vless://{reality_uid}@{reality_host}:{reality_port}"
            f"?{reality_query}#Node-B-REALITY"
        )

    return result


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



def render_components():
    """Independent detailed-page markup/script, hosted in one DOM ShadowRoot.

    Preserves diagnostics, browser checks, share/download and status areas. No
    iframe, main-page import, independent listener or legacy authentication.
    """
    import re
    page = PAGE
    style = re.search(r'<style>(.*?)</style>', page, re.S).group(1)
    markup = re.search(r'<body>(.*?)<script>', page, re.S).group(1)
    script = re.search(r'<script>(.*?)</script>', page, re.S).group(1)
    return style, markup, script

if __name__ == '__main__':
    raise SystemExit('Detailed panel is a unified-service module. Start main.py; no second listener is opened.')
