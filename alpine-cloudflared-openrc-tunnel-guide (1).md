# Alpine Linux：安装 cloudflared 并配置 OpenRC 系统服务连接 Cloudflare Tunnel

本文适用于 Alpine Linux（x86_64 / AMD64）、root 权限、使用 OpenRC 管理服务，并连接 Cloudflare Dashboard 中已经创建的**远程管理（Remotely-managed）Tunnel**。

> **安全提醒：** Tunnel Token 是机密信息。不要把 Token 发给他人，也不要粘贴到聊天、截图或公开日志中。

## 1. 更新软件包并安装依赖

```sh
apk update
apk add --no-cache curl ca-certificates
update-ca-certificates
```

## 2. 下载并安装 cloudflared（AMD64）

```sh
curl -fL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64" -o /usr/local/bin/cloudflared
chmod 755 /usr/local/bin/cloudflared
```

检查安装结果：

```sh
/usr/local/bin/cloudflared --version
command -v rc-service
command -v rc-update
```

如果系统不是 AMD64/x86_64，请勿使用上面的 AMD64 下载文件，应改用与 CPU 架构匹配的版本。

## 3. 创建 Token 存放目录

```sh
mkdir -p /etc/cloudflared
chmod 700 /etc/cloudflared
```

## 4. 安全保存 Tunnel Token

在 Cloudflare Dashboard 中打开对应 Tunnel，复制它提供的 Token。然后在容器终端执行下面这一整行：

```sh
umask 077; read -rsp "请输入 Tunnel Token: " TOKEN; echo; printf '%s' "$TOKEN" > /etc/cloudflared/token; unset TOKEN; chmod 600 /etc/cloudflared/token
```

在提示处粘贴 Token 并按回车。输入内容不会显示在屏幕上。

检查 Token 文件是否存在以及字节数（不会显示 Token 内容）：

```sh
ls -l /etc/cloudflared/token
wc -c < /etc/cloudflared/token
```

不要使用 `cat /etc/cloudflared/token` 输出 Token。若需要重新保存，重复本节命令即可。

## 5. 先以前台方式测试 Tunnel

执行：

```sh
/usr/local/bin/cloudflared tunnel --no-autoupdate run --token-file /etc/cloudflared/token
```

等待日志出现连接注册成功的信息，例如 `Registered tunnel connection`。同时可在 Cloudflare Zero Trust / Networks / Connectors（界面名称可能随版本变化）查看 Connector 状态。

确认连接成功后，按 `Ctrl+C` 停止前台进程，再继续配置服务。

如果出现 Token 无效或 JSON 解析错误，先检查 Token 是否完整复制、是否误带引号或换行，并重新保存；不要把 Token 发给他人排查。

## 6. 创建 OpenRC 服务脚本

创建服务脚本：

```sh
cat > /etc/init.d/cloudflared <<'EOF'
#!/sbin/openrc-run

supervisor=supervise-daemon

command="/usr/local/bin/cloudflared"
command_args="tunnel --no-autoupdate run --token-file /etc/cloudflared/token"

pidfile="/run/cloudflared.pid"

respawn_delay=5
respawn_max=0

depend() {
    need net
}
EOF
```

设置可执行权限：

```sh
chmod 755 /etc/init.d/cloudflared
```

这里使用 OpenRC 的 `supervise-daemon` 管理 cloudflared 进程。不要再同时添加 `command_background=yes`，避免两种后台管理方式冲突。

## 7. 清理旧服务状态（必要时）

如果之前已经创建或启动过同名服务，或者服务状态残留导致新配置无法正常接管，可以先执行：

```sh
rc-service cloudflared stop
rc-service cloudflared zap
```

`zap` 用于清除 OpenRC 记录的该服务状态。它不会删除 `/etc/cloudflared/token` 或 `/etc/init.d/cloudflared`。

如果这是第一次配置、没有旧服务状态，可以跳过本节。

## 8. 启动并检查服务

```sh
rc-service cloudflared start
rc-service cloudflared status
```

也可以检查进程：

```sh
pgrep -af cloudflared
```

如果启动失败，先查看终端返回的错误信息，并检查以下项目：

```sh
ls -l /usr/local/bin/cloudflared
ls -l /etc/cloudflared/token
ls -l /etc/init.d/cloudflared
```

确认 Token 文件权限为 `600`，服务脚本和 cloudflared 二进制文件可执行。

## 9. 设置为 OpenRC 默认运行级别自动启动

```sh
rc-update add cloudflared default
rc-update show default
```

检查输出中是否包含 `cloudflared | default`。

取消自动启动：

```sh
rc-update del cloudflared default
```

## 10. 日常管理命令

```sh
# 启动
rc-service cloudflared start

# 停止
rc-service cloudflared stop

# 重启
rc-service cloudflared restart

# 查看服务状态
rc-service cloudflared status

# 清除 OpenRC 服务状态（仅在必要时）
rc-service cloudflared zap

# 查看进程
pgrep -af cloudflared

# 查看默认运行级别中的服务
rc-update show default
```

## 11. 完成检查清单

- [ ] `/usr/local/bin/cloudflared` 存在且可执行。
- [ ] `/etc/cloudflared/token` 存在，且权限为 `600`。
- [ ] 前台测试时出现 Tunnel 连接注册成功的信息。
- [ ] `/etc/init.d/cloudflared` 存在且权限为 `755`。
- [ ] `rc-service cloudflared start` 可以成功启动服务。
- [ ] `rc-service cloudflared status` 显示服务正在运行。
- [ ] `rc-update show default` 中包含 `cloudflared | default`。
- [ ] Cloudflare Dashboard 中对应 Connector 显示健康/已连接状态。

## 12. 关于 Podman 容器自动启动的提醒

`rc-update add cloudflared default` 只负责配置**容器内部**的 OpenRC 服务自动启动。它不会自动启动 Podman 容器本身。

如果容器停止或宿主机重启，还需要在 Podman 宿主机侧配置容器自动启动，并确保容器启动时确实执行 OpenRC 的 init 流程。具体做法取决于服务商提供的容器启动方式。

## 13. 日志说明

本文服务脚本没有配置将输出重定向到 `/var/log/cloudflared.log` 或 `/var/log/cloudflared-error.log`，因此这些文件不一定存在。文件不存在并不代表 Tunnel 服务异常。

优先结合 `rc-service cloudflared status`、`pgrep -af cloudflared` 和 Cloudflare Dashboard 中 Connector 的状态判断服务是否正常。
