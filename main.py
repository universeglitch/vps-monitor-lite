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
from auth import Auth, AuthError, BackupPending, Busy, Corrupt, loads
from function import Coordinator, Memory, ResourceGate

PAGE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>VPS Monitor</title>
<style>*{box-sizing:border-box}body{margin:0;color:#eef0ff;font:16px system-ui;background:linear-gradient(145deg,#0c1938,#26143d);min-height:100vh}main{max-width:620px;margin:32px auto;padding:20px}header{padding:24px;border-radius:22px;background:linear-gradient(120deg,#2163aa,#6542b0)}section{background:#141a34cc;border:1px solid #ffffff12;border-radius:20px;padding:20px;margin-top:18px;backdrop-filter:blur(12px)}.row{display:flex;justify-content:space-between;align-items:center;padding:14px 0;gap:12px}.switch{width:54px;height:30px;border-radius:20px;background:#414466;position:relative;transition:background .3s ease-in-out}.switch:after{content:'';position:absolute;width:22px;height:22px;left:4px;top:4px;border-radius:50%;background:white;transition:transform .3s ease-in-out}.switch.on{background:#35ac73}.switch.on:after{transform:translateX(24px)}button{color:inherit;background:#413578;border:0;border-radius:12px;padding:12px;cursor:pointer}button:active{transform:scale(.98)}button:disabled{opacity:.5;cursor:not-allowed}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.all{display:flex;align-items:center;flex-direction:column;gap:10px;color:#6cb8ff}.all .switch{background:#814fc1}input,select{display:block;width:100%;padding:12px;margin:10px 0;border:1px solid #454969;border-radius:10px;background:#11172e;color:white}dialog{width:min(90vw,430px);background:#18213e;color:white;border:1px solid #58508a;border-radius:18px}dialog::backdrop{background:#0009}#notice{white-space:pre-wrap;font-size:14px;color:#f5cd8e}small{color:#aeb9d4}.secret{overflow-wrap:anywhere}</style>
<main><header><h1>VPS Monitor</h1><small>统一节点管理 · plus 开发版</small></header>
<section id="login"><h2>管理密码登录</h2><input id="password" type="password" autocomplete="current-password"><button id="loginButton">登录</button><small>首次初始化请使用受限本机终端，不接受公网抢先初始化。</small></section>
<div id="app" hidden><section><div class="all"><span>一键开关</span><button class="switch" id="all" disabled title="未配置协议和重启确认流程，拒绝执行"></button></div><div id="services"></div></section><section class="grid"><button id="api">API 管理</button><button id="manage">管理功能</button><button disabled>设为默认面板</button><button id="detail" disabled>切换到详细面板</button></section><button id="logout">退出当前设备</button></div><p id="notice" role="status"></p>
<dialog id="modal"><h2 id="title"></h2><div id="body"></div><button id="close">确认关闭</button></dialog></main><script>
'use strict';let csrf='';const $=s=>document.getElementById(s);function notice(t){$('notice').textContent=t}async function request(path,data){let r=await fetch(path,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json','X-CSRF-Token':csrf}:{},body:data?JSON.stringify(data):undefined,cache:'no-store'});let j=await r.json();if(!r.ok)throw Error(j.error||'操作失败');return j}function button(t,fn){let b=document.createElement('button');b.textContent=t;b.onclick=async()=>{try{await fn()}catch(e){notice(e.message)}};return b}function show(title){$('title').textContent=title;$('body').replaceChildren();$('modal').showModal();return $('body')}async function refresh(){let s=await request('/session');csrf=s.csrf;$('login').hidden=true;$('app').hidden=false;let st=await request('/status');$('services').replaceChildren();for(let [label,id] of [['WS + TLS','ws'],['XHTTP','xhttp'],['WS + Reality','reality'],['Cloudflared','cloudflared'],['详细面板','panel'],['测活','heartbeat']]){let row=document.createElement('div');row.className='row';let text=document.createElement('span');text.textContent=label;let b=button('',async()=>{if(id==='heartbeat'){await request('/heartbeat',{enabled:!st.heartbeat});await refresh()}else{notice('本项尚未完成安全配置/确认流程，当前拒绝操作，不会自动安装或重启')}});let on=id==='heartbeat'?st.heartbeat:id==='cloudflared'&&st.services.cloudflared.active;b.className='switch'+(on?' on':'');b.setAttribute('aria-label',label);b.setAttribute('aria-pressed',String(!!on));if(id!=='heartbeat')b.disabled=true;row.append(text,b);$('services').append(row)}}
$('loginButton').onclick=async()=>{try{await request('/login',{password:$('password').value});$('password').value='';await refresh();notice('')}catch(e){$('password').value='';notice(e.message)}};$('logout').onclick=async()=>{try{await request('/logout',{});location.reload()}catch(e){notice(e.message)}};$('close').onclick=()=>{$('body').replaceChildren();$('modal').close()};
$('api').onclick=()=>{let body=show('API 管理');body.append(button('生成 API Key',async()=>{let name=prompt('API 名称');if(name===null)return;let j=await request('/api/create',{name});let box=show('API Key');let p=document.createElement('p');p.className='secret';p.textContent=j.key;box.append(p,button('复制',async()=>navigator.clipboard.writeText(j.key)))}),button('读取 API Key',async()=>{let j=await request('/api/list');let box=show('选择 API');for(let r of j.items){let row=document.createElement('div');row.className='row';row.append(button(r.name,async()=>{let k=await request('/api/read',{ident:r.id});let b=show(r.name);let p=document.createElement('p');p.className='secret';p.textContent=k.key;b.append(p,button('复制',async()=>navigator.clipboard.writeText(k.key)))}),button('删除',async()=>{if(!confirm('确认删除 API：'+r.name+'？'))return;await request('/api/delete',{ident:r.id,confirmation_name:r.name});row.remove()}));box.append(row)}}))};
$('manage').onclick=()=>{let box=show('管理功能');box.append(button('修改管理密码',async()=>{let old=prompt('当前管理密码');if(old===null)return;let value=prompt('新管理密码');if(value===null)return;let confirmation=prompt('再次输入新管理密码');if(confirmation===null)return;await request('/password',{old,new:value,confirmation});location.reload()}));notice('备份导出、恢复及密钥轮换的网页流程尚未接入；不会执行占位操作')};refresh().catch(()=>{});
</script></html>'''


class Server(HTTPServer):
    def __init__(self, address, handler, coordinator, origin):
        self.coordinator, self.origin = coordinator, origin
        self.csrf_key = secrets.token_bytes(32)
        self.tasks = queue.Queue(maxsize=2)
        super().__init__(address, handler)
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
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie is not None: self.send_header('Set-Cookie',cookie)
        self.end_headers(); self.wfile.write(raw)

    def do_GET(self):
        try:
            if self.headers.get('Host') != urlsplit(self.server.origin).netloc:
                raise AuthError('unexpected Host')
            if self.path == '/': return self.reply(200,PAGE,html=True)
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
            if len(lengths)!=1 or not lengths[0].isdigit() or not 0<int(lengths[0])<=8192:
                raise AuthError('invalid request size')
            if self.headers.get_content_type() != 'application/json':
                raise AuthError('JSON required')
            data=loads(self.rfile.read(int(lengths[0])))
            if not isinstance(data,dict): raise AuthError('object required')
            c=self.server.coordinator
            if self.path == '/login':
                # Conservative single trusted-loopback source bucket. Do not trust
                # unvalidated forwarding headers for rate limit identity.
                token=c.auth.login(data.get('password'),source=self.client_address[0])
                return self.reply(200,{'ok':True},'__Host-vps_session='+token+'; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=86400')
            token=self.token(); c.auth.validate(token)
            if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),self.csrf(token)):
                raise AuthError('CSRF rejected')
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
            if self.path == '/heartbeat':
                c.active.set_heartbeat(data.get('enabled')); return self.reply(200,{'ok':True})
            raise AuthError('operation unavailable')
        except BackupPending:
            self.reply(503,{'error':'change committed; event backup pending; do not retry blindly','committed':True})
        except Busy as exc:
            self.reply(503,{'error':str(exc)})
        except (AuthError,ServiceError,TypeError,ValueError):
            self.reply(403,{'error':'request rejected'})
        except Exception:
            self.reply(503,{'error':'operation outcome requires verification'})


def cli():
    p=argparse.ArgumentParser(description='V1 plus management; legacy panel remains frozen')
    p.add_argument('--data',required=True)
    p.add_argument('--resource-profile',required=True,help='private JSON of measured incremental profiles and reserve bytes')
    p.add_argument('--initialize',action='store_true',help='trusted terminal bootstrap; do not expose through HTTP')
    p.add_argument('--bind',default='127.0.0.1')
    p.add_argument('--port',type=int,required=True)
    p.add_argument('--origin',required=True,help='exact public HTTPS origin')
    args=p.parse_args()
    if not ipaddress.ip_address(args.bind).is_loopback: p.error('loopback binding required')
    origin=urlsplit(args.origin)
    if origin.scheme!='https' or not origin.netloc or origin.path or origin.query or origin.fragment or origin.username:
        p.error('exact HTTPS origin required')
    with open(args.resource_profile,'rb') as f: profile=loads(f.read(262145))
    gate=ResourceGate(Memory(),profile['profiles'],profile['reserve'])
    auth=Auth(args.data,gate=gate); active=Active()
    try:
        if args.initialize:
            if not os.isatty(0): p.error('interactive trusted terminal required')
            recovery=auth.initialize(getpass.getpass('管理密码：'),getpass.getpass('确认密码：'),local_authority=True)
            print('恢复密钥仅展示一次：',recovery)
            return
        active.start_monitor()
        server=Server((args.bind,args.port),Handler,Coordinator(auth,active,gate),args.origin)
        try: server.serve_forever()
        finally: server.server_close()
    finally:
        active.close(); auth.close()


if __name__=='__main__': cli()
