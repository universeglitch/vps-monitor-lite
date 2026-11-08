"""Single-process V1 management entrypoint. Legacy panel.py is never imported.

Run only behind a trusted HTTPS endpoint forwarding to loopback. Bootstrap is CLI
only. No software installation, dependency upgrade or proxy stop on startup.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import queue
import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

from active import Active, ServiceError
from auth import Auth, AuthError, BackupPending, Busy, Corrupt, loads, b64, unb64
from function import Coordinator, Memory, ResourceGate, PanelBackend, Discovery, XrayTransaction, XraySelfTest, BoundedCommand, ModuleInstaller

PAGE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>VPS Monitor</title>
<style>*{box-sizing:border-box}body{margin:0;color:#eef0ff;font:16px system-ui;background:linear-gradient(145deg,#0c1938,#26143d);min-height:100vh}body{opacity:1;transition:opacity .3s ease-in-out}body.switching{opacity:0}main{max-width:620px;margin:32px auto;padding:20px}header{padding:24px;border-radius:22px;background:linear-gradient(120deg,#2163aa,#6542b0)}section{background:#141a34cc;border:1px solid #ffffff12;border-radius:20px;padding:20px;margin-top:18px;backdrop-filter:blur(12px)}.row{display:flex;justify-content:space-between;align-items:center;padding:14px 0;gap:12px}.switch{width:54px;height:30px;border-radius:20px;background:#414466;position:relative;transition:background .3s ease-in-out}.switch:after{content:'';position:absolute;width:22px;height:22px;left:4px;top:4px;border-radius:50%;background:white;transition:transform .3s ease-in-out}.switch.on{background:#35ac73}.switch.on:after{transform:translateX(24px)}button{color:inherit;background:#413578;border:0;border-radius:12px;padding:12px;cursor:pointer}button:active{transform:scale(.98)}button:disabled{opacity:.5;cursor:not-allowed}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.all{display:flex;align-items:center;flex-direction:column;gap:10px;color:#6cb8ff}.all .switch{background:#814fc1}input,select{display:block;width:100%;padding:12px;margin:10px 0;border:1px solid #454969;border-radius:10px;background:#11172e;color:white}dialog{width:min(90vw,430px);background:#18213e;color:white;border:1px solid #58508a;border-radius:18px}dialog::backdrop{background:#0009}#notice{white-space:pre-wrap;font-size:14px;color:#f5cd8e}small{color:#aeb9d4}.secret{overflow-wrap:anywhere}</style>
<main><header><h1>VPS Monitor</h1><small>统一节点管理 · plus 开发版</small></header>
<section id="login"><h2>管理密码登录</h2><input id="password" type="password" autocomplete="current-password"><button id="loginButton">登录</button><small>首次初始化请使用受限本机终端，不接受公网抢先初始化。</small></section>
<div id="app" hidden><section><div class="all"><span>一键开关</span><button class="switch" id="all" disabled title="未配置协议和重启确认流程，拒绝执行"></button></div><div id="services"></div></section><section class="grid"><button id="api">API 管理</button><button id="manage">管理功能</button><button id="modules">模块管理</button><button id="defaultMain">设为默认面板</button><button id="detail">切换到详细面板</button></section><button id="logout">退出当前设备</button></div><div id="detailHost" hidden></div><p id="notice" role="status"></p>
<dialog id="modal"><h2 id="title"></h2><div id="body"></div><button id="close">确认关闭</button></dialog></main><script>
'use strict';let csrf='';const $=s=>document.getElementById(s);function notice(t){$('notice').textContent=t}async function request(path,data){let r=await fetch(path,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json','X-CSRF-Token':csrf}:{},body:data?JSON.stringify(data):undefined,cache:'no-store'});let j=await r.json();if(!r.ok)throw Error(j.error||'操作失败');return j}function button(t,fn){let b=document.createElement('button');b.textContent=t;b.onclick=async()=>{try{await fn()}catch(e){notice(e.message)}};return b}function show(title){$('title').textContent=title;$('body').replaceChildren();$('modal').showModal();return $('body')}async function refresh(){let s=await request('/session');csrf=s.csrf;$('login').hidden=true;$('app').hidden=false;let st=await request('/modules',{});$('services').replaceChildren();for(let [label,id] of st.modules.filter(m=>m.visible).map(m=>[m.name,m.id]).concat([['测活','heartbeat']])){let row=document.createElement('div');row.className='row';let text=document.createElement('span');text.textContent=label;let b=button('',async()=>{if(id==='heartbeat'){await request('/heartbeat',{enabled:!st.heartbeat});await refresh()}else{await toggleService(st.modules.find(m=>m.id===id));await refresh()}});let on=id==='heartbeat'?st.heartbeat:!!st.modules.find(m=>m.id===id)?.active;b.className='switch'+(on?' on':'');b.setAttribute('aria-label',label);b.setAttribute('aria-pressed',String(!!on));row.append(text,b);$('services').append(row)}}
$('loginButton').onclick=async()=>{try{await request('/login',{password:$('password').value});$('password').value='';await refresh();let prefs=await request('/preferences',{});if(prefs.default_panel==='panel')await switchPanel('panel');notice('')}catch(e){$('password').value='';notice(e.message)}};$('logout').onclick=async()=>{try{await request('/logout',{});location.reload()}catch(e){notice(e.message)}};$('close').onclick=()=>{$('body').replaceChildren();$('modal').close()};
$('api').onclick=()=>{let body=show('API 管理');body.append(button('生成 API Key',async()=>{return fields('生成 API Key',[['name','API 名称','text']],async data=>{let j=await request('/api/create',data);let box=show('API Key');let p=document.createElement('p');p.className='secret';p.textContent=j.key;box.append(p,button('复制',async()=>navigator.clipboard.writeText(j.key)))});} ),button('读取 API Key',async()=>{let j=await request('/api/list');let box=show('选择 API');for(let r of j.items){let row=document.createElement('div');row.className='row';row.append(button(r.name,async()=>{let k=await request('/api/read',{ident:r.id});let b=show(r.name);let p=document.createElement('p');p.className='secret';p.textContent=k.key;b.append(p,button('复制',async()=>navigator.clipboard.writeText(k.key)))}),button('删除',async()=>{if(!confirm('确认删除 API：'+r.name+'？'))return;await request('/api/delete',{ident:r.id,confirmation_name:r.name});row.remove()}));box.append(row)}}))};
async function fields(title, items, perform){let box=show(title);let inputs={};for(let item of items){let label=document.createElement('label');label.textContent=item[1];let input=document.createElement('input');input.type=item[2]||'password';input.autocomplete='off';inputs[item[0]]=input;label.append(input);box.append(label)}box.append(button('确认执行',async()=>{let data={};for(let [k,v] of Object.entries(inputs)){data[k]=v.value;v.value=''}await perform(data)}))}
$('manage').onclick=()=>{let box=show('管理功能');box.append(
button('修改管理密码',()=>fields('修改管理密码',[['old','当前密码'],['new','新密码'],['confirmation','确认新密码']],async d=>{await request('/password',d);location.reload()})),
button('本地加密备份',async()=>{await request('/backup/local',{});notice('本地备份完成')}),
button('导出外部加密备份',()=>fields('独立备份密码',[['password','独立备份密码']],async d=>{let r=await request('/backup/export',d);let bytes=Uint8Array.from(atob(r.package),c=>c.charCodeAt(0));let url=URL.createObjectURL(new Blob([bytes],{type:'application/octet-stream'}));let a=document.createElement('a');a.href=url;a.download='vps-auth-backup.enc';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);$('modal').close();notice('导出完成，请异地保存并保管独立密码')})),
button('恢复外部备份',async()=>{let b=show('恢复外部备份（使所有旧 Session 失效）');let f=document.createElement('input');f.type='file';let pass=document.createElement('input');pass.type='password';pass.placeholder='独立备份密码';b.append(f,pass,button('确认恢复',async()=>{if(!f.files[0]||f.files[0].size>1048576)throw Error('请选择不超过 1 MiB 的加密备份');if(!confirm('确认验证并恢复认证数据？所有 Session 将失效。'))return;let raw=new Uint8Array(await f.files[0].arrayBuffer());let text='';for(let i=0;i<raw.length;i+=4096)text+=String.fromCharCode(...raw.subarray(i,i+4096));let p=pass.value;pass.value='';await request('/backup/restore',{package:btoa(text),password:p});location.reload()}))}),
button('恢复本地备份',async()=>{let b=show('选择本地备份');for(let slot of ['latest','previous'])b.append(button(slot,async()=>{if(!confirm('确认恢复 '+slot+'？所有 Session 将失效。'))return;await request('/backup/local-restore',{slot});location.reload()}))}),
button('轮换主密钥',async()=>{if(!confirm('确认轮换 LMK/KEK/DEK，并触发加密备份？'))return;let r=await request('/keys/rotate',{});notice('密钥已轮换，版本 '+r.version)}),
button('清理无引用对象',async()=>{if(!confirm('仅清理不被当前状态和本地备份引用的对象？'))return;let r=await request('/keys/cleanup',{});notice('已清理对象 '+r.removed)}))};
$('login').append(button('忘记密码',()=>fields('恢复密码',[['recovery','恢复密钥'],['new','新管理密码'],['confirmation','确认密码']],async d=>{let r=await request('/recover',d);let b=show('新恢复密钥（仅本次展示）');let p=document.createElement('p');p.className='secret';p.textContent=r.recovery;b.append(p,button('复制',()=>navigator.clipboard.writeText(r.recovery)));notice('密码已恢复；旧恢复密钥和全部旧 Session 已失效')})));
let detailReady=false, diagnosed=false;
async function switchPanel(name){if(!['main','panel'].includes(name))return;document.body.classList.add('switching');await new Promise(r=>setTimeout(r,300));if(name==='panel'&&!detailReady){let d=await request('/view',{view:'panel'});let host=$('detailHost');let shadow=host.attachShadow({mode:'open'});let style=document.createElement('style');style.textContent=d.style;let content=document.createElement('div');content.innerHTML=d.markup;shadow.append(style,content);let bridge={call:request,detail:async action=>({json:async()=>request('/detail',{action})}),expired:()=>location.reload(),switch:switchPanel,modules:moduleWindow,all:allServices,toggle:toggleService,port:portWindow,logout:()=>request('/logout',{}).then(()=>location.reload()),diagnosed};mountDetailed(shadow,bridge);detailReady=true}$('app').hidden=name==='panel';$('detailHost').hidden=name!=='panel';document.querySelector('header').hidden=name==='panel';document.body.classList.remove('switching')}
$('detail').onclick=()=>switchPanel('panel');$('defaultMain').onclick=async()=>{try{await request('/preferences',{default_panel:'main'});notice('主面板已设为默认')}catch(e){notice(e.message)}};
async function toggleService(m){if(m.id==='panel'){return switchPanel('panel')}let p=await request('/service/prepare',{id:m.id,enabled:!m.active});if(!confirm(p.warning))return;await request('/service/apply',{confirmation:p.confirmation});}
async function allServices(){let d=await request('/modules',{});let eligible=d.modules.filter(m=>m.visible&&m.id!=='panel');let enable=eligible.some(m=>!m.active);let p=await request('/service/prepare',{id:'all',enabled:enable});if(!confirm(p.warning))return;await request('/service/apply',{confirmation:p.confirmation});await refresh()}
$('all').disabled=false;$('all').onclick=()=>allServices().catch(e=>notice(e.message));
async function moduleWindow(){let d=await request('/modules',{});let b=show('模块管理');for(let m of d.modules){let row=document.createElement('section');let t=document.createElement('p');t.textContent=m.name+' · '+m.state;row.append(t);if(!m.installed)row.append(button('安装',async()=>{let p=await request('/module/install',{service:m.id});if(!confirm('确认安装 '+m.name+'？缺少：'+(p.missing||[]).join('、')))return;notice('正在验证和安装…');let result=await request('/module/install',{service:m.id,confirmation:p.confirmation});notice(JSON.stringify(result));$('modal').close();await moduleWindow()}));if(['ws','xhttp','reality'].includes(m.id))row.append(button('配置向导',()=>wizard(m.id)));if(m.id==='cloudflared')row.append(button('Cloudflare 配置指导',cloudGuide));b.append(row)}}
$('modules').onclick=()=>moduleWindow().catch(e=>notice(e.message));
async function cloudGuide(){let d=await request('/cloud-guide',{});let b=show(d.title);for(let text of [d.ws_rule,d.panel_rule]){let p=document.createElement('p');p.textContent=text;b.append(p,button('复制',()=>navigator.clipboard.writeText(text)))}let note=document.createElement('p');note.textContent=d.note;b.append(note)}
async function wizard(kind){let schema=await request('/xray/schema',{kind});let b=show(schema.title);let note=document.createElement('p');note.textContent=schema.description;b.append(note);let inputs={};for(let f of schema.fields){let label=document.createElement('label');label.textContent=f.label+(f.required?' *':'（可选）')+' — '+f.help;let el=document.createElement('input');el.type=f.secret?'password':'text';el.autocomplete='off';el.value=f.value||'';inputs[f.name]=el;label.append(el);b.append(label)}b.append(button('验证候选配置与查看差异',async()=>{let values={};for(let [k,v] of Object.entries(inputs))values[k]=v.value;let p=await request('/xray/preview',{kind,values});let box=show('确认 Xray 配置变更');let diff=document.createElement('pre');diff.textContent=p.diff;box.append(diff);let warn=document.createElement('p');warn.textContent=p.warning;box.append(warn,button('明确同意应用并重启',async()=>{await request('/xray/apply',{confirmation:p.confirmation});$('modal').close();notice('配置已应用并验证');await refresh()}))}))}
async function portWindow(port){if(!port.editable){notice(port.reason||'该端口不属于本项目，禁止修改');return}return fields('修改端口 '+port.name+'（当前 '+port.port+'）',[['port','新端口','number']],async data=>{let p=await request('/port/preview',{id:port.id,port:data.port});let b=show('端口变更确认');let diff=document.createElement('pre');diff.textContent=p.diff;b.append(diff,button('确认应用并重启 Xray',async()=>{await request('/xray/apply',{confirmation:p.confirmation});$('modal').close();notice('端口变更完成')}))})}
refresh().then(async()=>{let p=await request('/preferences',{});if(p.default_panel==='panel')await switchPanel('panel')}).catch(()=>{});

</script></html>'''


class Server(HTTPServer):
    def __init__(self, address, handler, coordinator, origin):
        self.coordinator, self.origin = coordinator, origin
        self.panel_backend = PanelBackend(coordinator,port=address[1] or None)
        self.csrf_key = secrets.token_bytes(32)
        self.tasks = queue.Queue(maxsize=2)
        super().__init__(address, handler)
        self.panel_backend.port=self.server_port
        for i in range(2):
            threading.Thread(target=self.worker, name='http-'+str(i), daemon=True).start()

    def process_request(self, request, address):
        request.settimeout(5)
        try:
            self.tasks.put_nowait((request,address))
        except queue.Full:
            self.shutdown_request(request)

    def worker(self):
        while True:
            request,address=self.tasks.get()
            try:
                self.finish_request(request,address)
            except Exception:
                self.handle_error(request,address)
            finally:
                self.shutdown_request(request); self.tasks.task_done()

    def handle_error(self, request, address):
        # Never log request bodies, cookies or tracebacks containing secrets.
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = 'VPSMonitor'
    sys_version = ''
    protocol_version = 'HTTP/1.0'

    def log_message(self, *args):
        pass

    def token(self):
        cookie=SimpleCookie()
        try: cookie.load(self.headers.get('Cookie',''))
        except Exception: raise AuthError('invalid cookie') from None
        return cookie['__Host-vps_session'].value if '__Host-vps_session' in cookie else ''

    def csrf(self, token):
        return hmac.new(self.server.csrf_key,token.encode(),hashlib.sha256).hexdigest()

    def reply(self, code, data, cookie=None, html=False):
        raw=data.encode() if html else json.dumps(data,ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type','text/html; charset=utf-8' if html else 'application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; connect-src 'self' https://www.baidu.com https://www.taobao.com https://www.google.com https://www.youtube.com https://github.com https://chatgpt.com https://claude.ai https://gemini.google.com; img-src 'self' data:; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie is not None: self.send_header('Set-Cookie',cookie)
        self.end_headers(); self.wfile.write(raw)

    def do_GET(self):
        try:
            if self.headers.get('Host') != urlsplit(self.server.origin).netloc:
                raise AuthError('unexpected Host')
            if self.path == '/':
                try:
                    import panel
                    script=panel.render_components()[2]
                except ImportError:
                    script="bridge.expired();throw Error('详细面板尚未安装')"
                entry=PAGE.replace('<script>', '<script>function mountDetailed(root,bridge){'+script+'}\n',1)
                return self.reply(200,entry,html=True)
            token=self.token(); self.server.coordinator.auth.validate(token)
            if self.path == '/session': return self.reply(200,{'csrf':self.csrf(token)})
            if self.path == '/status': return self.reply(200,self.server.coordinator.status(token))
            raise AuthError('route unavailable')
        except AuthError as exc:
            self.reply(401,{'error':str(exc)})
        except Exception:
            self.reply(503,{'error':'service unavailable; no operation completed'})

    def do_POST(self):
        try:
            if (self.headers.get('Origin') != self.server.origin or
                    self.headers.get('Host') != urlsplit(self.server.origin).netloc):
                raise AuthError('origin rejected')
            if self.headers.get('Transfer-Encoding') is not None:
                raise AuthError('chunked requests not accepted')
            lengths=self.headers.get_all('Content-Length',[])
            if len(lengths)!=1 or not lengths[0].isdigit() or not 0<int(lengths[0])<=1_400_000:
                raise AuthError('invalid request size')
            if self.headers.get_content_type() != 'application/json':
                raise AuthError('JSON required')
            raw=self.rfile.read(int(lengths[0]))
            if len(raw)!=int(lengths[0]): raise AuthError('incomplete request')
            data=loads(raw,1_400_000)
            if not isinstance(data,dict): raise AuthError('object required')
            c=self.server.coordinator
            if self.path == '/login':
                # Conservative single trusted-loopback source bucket. Do not trust
                # unvalidated forwarding headers for rate limit identity.
                token=c.auth.login(data.get('password'),source=self.client_address[0])
                return self.reply(200,{'ok':True},'__Host-vps_session='+token+'; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=86400')
            if self.path == '/recover':
                replacement=c.auth.recover(data.get('recovery'),data.get('new'),data.get('confirmation'),source=self.client_address[0])
                return self.reply(200,{'recovery':replacement})
            token=self.token(); c.auth.validate(token)
            if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),self.csrf(token)):
                raise AuthError('CSRF rejected')
            backend=self.server.panel_backend
            if self.path == '/view':
                if data.get('view')!='panel': raise AuthError('invalid view')
                import panel
                panel.authorize(c,token)
                style,markup,script=panel.render_components()
                return self.reply(200,{'style':style,'markup':markup})
            if self.path == '/preferences':return self.reply(200,backend.preferences(token,data.get('default_panel')))
            if self.path == '/modules':return self.reply(200,backend.modules(token))
            if self.path == '/nodes':return self.reply(200,{'nodes':backend.nodes(token)})
            if self.path == '/node/name':return self.reply(200,backend.rename(token,data.get('id'),data.get('name')))
            if self.path == '/node/share':return self.reply(200,{'uri':backend.share(token,data.get('id'))})
            if self.path == '/node/qr':return self.reply(200,{'image':backend.qr(token,data.get('id'))})
            if self.path == '/ports':return self.reply(200,{'ports':backend.ports(token)})
            if self.path == '/cloud-guide':return self.reply(200,backend.guide(token))
            if self.path == '/detail':return self.reply(200,backend.detail(token,data.get('action'),self.headers))
            if self.path == '/module/install':
                installer=getattr(self.server,'module_installer',None)
                if installer is None:raise AuthError('isolated installer runner unavailable')
                result=installer.execute(token,data['confirmation']) if data.get('confirmation') else installer.prepare(token,data.get('service'))
                return self.reply(200,result)
            if self.path == '/service/prepare':return self.reply(200,backend.prepare_service(token,data.get('id'),data.get('enabled')))
            if self.path == '/service/apply':return self.reply(200,backend.apply_service(token,data.get('confirmation')))
            if self.path == '/xray/schema':return self.reply(200,backend.schema(token,data.get('kind')))
            if self.path == '/xray/preview':return self.reply(200,backend.preview(token,data.get('kind'),data.get('values')))
            if self.path == '/port/preview':return self.reply(200,backend.port_preview(token,data.get('id'),data.get('port')))
            if self.path == '/xray/apply':return self.reply(200,backend.apply_candidate(token,data.get('confirmation')))
            if self.path == '/logout':
                c.auth.logout(token)
                return self.reply(200,{'ok':True},'__Host-vps_session=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0')
            if self.path == '/api/list': return self.reply(200,{'items':c.api(token,'list')})
            if self.path == '/api/create': return self.reply(200,c.api(token,'create',name=data.get('name')))
            if self.path == '/api/read': return self.reply(200,{'key':c.api(token,'read',ident=data.get('ident'))})
            if self.path == '/api/delete':
                c.api(token,'delete',ident=data.get('ident'),confirmation_name=data.get('confirmation_name'))
                return self.reply(200,{'ok':True})
            if self.path == '/password':
                c.auth.change_password(token,data.get('old'),data.get('new'),data.get('confirmation'),source=self.client_address[0])
                return self.reply(200,{'ok':True})
            if self.path == '/backup/export':
                return self.reply(200,{'package':b64(c.auth.export_backup(token,data.get('password')))})
            if self.path == '/backup/restore':
                c.auth.restore_backup(token,unb64(data.get('package')),data.get('password'))
                return self.reply(200,{'ok':True,'sessions_revoked':True})
            if self.path == '/backup/local':
                return self.reply(200,{'revision':c.auth.local_backup(token)})
            if self.path == '/backup/local-restore':
                c.auth.restore_local(token,data.get('slot'));return self.reply(200,{'ok':True,'sessions_revoked':True})
            if self.path == '/keys/rotate':
                return self.reply(200,{'version':c.auth.rotate_keys(token)})
            if self.path == '/keys/cleanup':
                return self.reply(200,{'removed':c.auth.collect_objects(token)})
            if self.path == '/heartbeat':
                c.auth.validate(token); c.active.set_heartbeat(data.get('enabled')); return self.reply(200,{'ok':True})
            raise AuthError('operation unavailable')
        except BackupPending:
            self.reply(503,{'error':'change committed; event backup pending; do not retry blindly','committed':True})
        except Busy as exc:
            self.reply(503,{'error':str(exc)})
        except (AuthError,ServiceError) as exc:
            self.reply(403,{'error':str(exc)})
        except (TypeError,ValueError):
            self.reply(403,{'error':'invalid request'})
        except Exception:
            self.reply(503,{'error':'operation outcome requires verification'})


def cli():
    p=argparse.ArgumentParser(description='V1 plus management; legacy panel remains frozen')
    p.add_argument('--data',required=True)
    p.add_argument('--resource-profile',required=True,help='private JSON of measured incremental profiles and reserve bytes')
    p.add_argument('--restore-fresh',help='encrypted external backup; trusted terminal, empty vault only')
    p.add_argument('--initialize',action='store_true',help='trusted terminal bootstrap; do not expose through HTTP')
    p.add_argument('--xray-config',default='/usr/local/etc/xray/config.json')
    p.add_argument('--cloud-config',help='existing operator-managed Cloudflared configuration; never rewritten')
    p.add_argument('--single-writer',action='store_true',help='operator attests this process is the only Xray config writer')
    p.add_argument('--task-memory-max',type=int,help='measured isolated test task cgroup cap in bytes; no invented default')
    p.add_argument('--task-slice',help='administrator-provisioned isolated management slice')
    p.add_argument('--bind',default='127.0.0.1')
    p.add_argument('--port',type=int,required=True)
    p.add_argument('--origin',required=True,help='exact public HTTPS origin')
    args=p.parse_args()
    if not ipaddress.ip_address(args.bind).is_loopback: p.error('loopback binding required')
    origin=urlsplit(args.origin)
    if origin.scheme!='https' or not origin.netloc or origin.path or origin.query or origin.fragment or origin.username:
        p.error('exact HTTPS origin required')
    with open(args.resource_profile,'rb') as f: profile=loads(f.read(262145))
    if (not isinstance(profile,dict) or not isinstance(profile.get('profiles'),dict) or
        type(profile.get('reserve')) is not int or not 0 < profile['reserve'] < 200_000_000 or
        any(type(v) is not int or not 0 < v < 200_000_000 for v in profile['profiles'].values())):
        p.error('positive measured byte profiles and reserve required')
    gate=ResourceGate(Memory(),profile['profiles'],profile['reserve'])
    auth=Auth(args.data,gate=gate); active=Active()
    try:
        if args.restore_fresh:
            if args.initialize or not os.isatty(0): p.error('fresh restore requires trusted terminal and exclusive mode')
            with open(args.restore_fresh,'rb') as backup:
                package=backup.read(1048577)
            auth.import_fresh(package,getpass.getpass('独立备份密码：'),local_authority=True)
            print('认证备份已恢复，所有旧 Session 无效')
            return
        if args.initialize:
            if not os.isatty(0): p.error('interactive trusted terminal required')
            recovery=auth.initialize(getpass.getpass('管理密码：'),getpass.getpass('确认密码：'),local_authority=True)
            print('恢复密钥仅展示一次：',recovery)
            return
        active.start_monitor()
        coordinator=Coordinator(auth,active,gate)
        coordinator.discovery=Discovery(active,args.xray_config,args.cloud_config)
        import shutil
        launcher=None
        if args.task_memory_max is not None:
            import re
            if not 0 < args.task_memory_max < 200_000_000 or not args.task_slice or not re.fullmatch(r'[A-Za-z0-9_.-]+\.slice',args.task_slice):
                p.error('measured positive task cap and administrator-provisioned slice required')
            binary=shutil.which('systemd-run')
            if binary is None:p.error('systemd-run unavailable')
            def launcher(argv):
                return [binary,'--quiet','--wait','--pipe','--collect','--slice='+args.task_slice,
                        '--property=MemoryMax='+str(args.task_memory_max),'--property=TasksMax=16',
                        '--property=RuntimeMaxSec=45','--property=KillMode=control-group',
                        '--property=Nice=10','--',*argv]
        runner=BoundedCommand(gate,launcher)
        executable=shutil.which('xray')
        if args.single_writer and executable:
            tester=XraySelfTest(runner,executable)
            x=XrayTransaction(auth.store,args.xray_config,gate,tester,active,auth.validate)
            x.single_writer=True;coordinator.xray=x
        def readiness(name):
            info=coordinator.discovery.inspect(name)
            if info['readiness']!='requires_validation':raise ServiceError('service not installed/configured')
            if name=='xray':
                if coordinator.xray is None:raise ServiceError('single-writer test backend unavailable')
                with gate('xray-test'):coordinator.xray.test(coordinator.xray._read()[0])
            else:
                # Unknown Cloudflared CLI cannot be guessed or config rewritten.
                raise ServiceError('Cloudflared executable/config validation not certified for this version')
        active.readiness=readiness
        server=Server((args.bind,args.port),Handler,coordinator,args.origin)
        server.module_installer=ModuleInstaller(server.panel_backend,runner)
        try: server.serve_forever()
        finally: server.server_close()
    finally:
        active.close(); auth.close()


if __name__=='__main__': cli()
