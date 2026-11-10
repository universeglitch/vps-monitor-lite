# Alpine Linux：安装 cloudflared 并配置 Tunnel 开机启动

本文记录在 Alpine Linux（Podman 容器）中安装 Cloudflare `cloudflared`、保存 Tunnel Token、配置 OpenRC 系统服务并连接 Tunnel 的完整流程。

> 注意：以下命令假设使用 AMD64/x86_64 架构、root 用户，并且容器使用 OpenRC。请根据实际环境确认。

## 1. 安装 cloudflared

更新软件包并安装必要工具：

```sh
apk update
apk add --no-cache curl ca-certificates
```

下载 Cloudflare 官方 AMD64 二进制文件：

```sh
mkdir -p /usr/local/bin

curl -fL \
https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
-o /usr/local/bin/cloudflared

chmod 755 /usr/local/bin/cloudflared
```

检查版本：

```sh
/usr/local/bin/cloudflared --version
```

也可以检查命令是否能从 PATH 中找到：

```sh
command -v cloudflared
```

## 2. 保存 Cloudflare Tunnel Token

在 Cloudflare Dashboard 中打开已有 Tunnel 的连接器安装页面，选择 Linux，复制 Tunnel Token。不要把 Token 分享给他人。

创建配置目录：

```sh
mkdir -p /etc/cloudflared
chmod 700 /etc/cloudflared
```

使用下面这一行交互式输入 Token。输入时字符不会显示：

```sh
umask 077; read -rsp "请输入 Tunnel Token: " TOKEN; echo; printf '%s' "$TOKEN" > /etc/cloudflared/token; unset TOKEN; chmod 600 /etc/cloudflared/token
```

检查 Token 文件是否存在及其字节数：

```sh
ls -l /etc/cloudflared/token
wc -c < /etc/cloudflared/token
```

本次配置中 Token 文件为 **184 字节**。以后重新部署时，实际长度可能不同；不要将 Token 内容发到聊天或截图中。

## 3. 前台测试 Tunnel 连接

使用 Token 文件启动 cloudflared：

```sh
/usr/local/bin/cloudflared tunnel --no-autoupdate run --token-file /etc/cloudflared/token
```

如果日志出现类似以下内容，说明连接器已成功向 Cloudflare 注册：

```text
Registered tunnel connection
```

测试完成后，按 `Ctrl + C` 停止前台进程，再配置后台服务。

## 4. 创建 OpenRC 服务

创建 `/etc/init.d/cloudflared`：

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

赋予执行权限：

```sh
chmod 755 /etc/init.d/cloudflared
```

该脚本使用 OpenRC 的 `supervise-daemon` 管理进程，不要再同时添加 `command_background=yes`。

## 5. 启动并检查服务

启动服务：

```sh
rc-service cloudflared start
```

检查状态：

```sh
rc-service cloudflared status
```

如果服务启动失败，先查看命令输出和系统日志，不要直接重复创建服务脚本或覆盖 Token 文件。

## 6. 加入 OpenRC 默认启动级别

将服务加入 `default`：

```sh
rc-update add cloudflared default
```

检查启动项：

```sh
rc-update show default
```

确认列表中包含 `cloudflared`。

> `rc-update add` 只负责将服务加入 OpenRC 启动级别；它本身不保证 Podman 容器会自动启动。还需要在宿主机侧配置容器自动启动，并确保容器启动流程会运行 OpenRC。

## 7. 常用服务管理命令

| 操作 | 命令 |
|---|---|
| 启动 | `rc-service cloudflared start` |
| 停止 | `rc-service cloudflared stop` |
| 重启 | `rc-service cloudflared restart` |
| 查看状态 | `rc-service cloudflared status` |
| 查看启动项 | `rc-update show default` |
| 设置开机启动 | `rc-update add cloudflared default` |
| 取消开机启动 | `rc-update del cloudflared default` |

查看 cloudflared 进程：

```sh
pgrep -af cloudflared
```

查看日志：

```sh
tail -n 30 /var/log/cloudflared.log
tail -n 30 /var/log/cloudflared-error.log
```

注意：当前服务脚本没有显式配置日志重定向，因此上述日志文件可能不存在；文件不存在不一定代表服务故障。可以结合 `rc-service cloudflared status`、进程状态以及 Cloudflare Dashboard 中 Tunnel 的状态判断。

## 8. 目录结构

```text
/usr/local/bin/
└── cloudflared                 # cloudflared 可执行文件

/etc/cloudflared/
└── token                       # Tunnel Token，权限 600

/etc/init.d/
└── cloudflared                 # OpenRC 服务脚本
```

## 9. 部署完成检查清单

- [ ] `cloudflared --version` 能正常显示版本
- [ ] `/etc/cloudflared/token` 存在且不是空文件
- [ ] 前台运行时出现 `Registered tunnel connection`
- [ ] `rc-service cloudflared start` 启动成功
- [ ] `rc-service cloudflared status` 显示服务正在运行
- [ ] `rc-update show default` 中包含 `cloudflared`
- [ ] Cloudflare Dashboard 中对应 Tunnel 显示连接正常（例如 `Healthy`）

**说明：** Tunnel 显示 `Healthy` 代表连接器已连接 Cloudflare，不代表 Tunnel 的 Public Hostname、Ingress 规则或源站服务一定配置正确；这些仍需单独检查。
