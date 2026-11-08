服务异常恢复与安装失败处理

本项目使用 systemd 管理 Xray 和 Node Monitor Python 面板。Cloudflared 由用户自行安装和配置。

1. 服务异常时如何手动恢复

如果发现某个服务停止或异常，可以单独重启对应服务。

重启 Xray：

systemctl restart xray.service 

重启 Python 面板：

systemctl restart node-panel.service 

重启 Cloudflared：

systemctl restart cloudflared.service 

注意：Cloudflared 服务名称以实际安装为准。如果提示找不到服务，先检查：

systemctl list-units --all | grep -i cloudflared 

以上操作只重启对应服务，不需要重新运行安装脚本。

2. 查看服务运行状态

分别检查：

systemctl status xray.service --no-pager -l systemctl status node-panel.service --no-pager -l systemctl status cloudflared.service --no-pager -l 

也可以快速查看三个服务是否正在运行：

for s in xray node-panel cloudflared; do printf "%-16s " "$s" systemctl is-active "$s" 2>/dev/null || true done 

正常运行时应显示：

xray active node-panel active cloudflared active 

3. 查看服务错误日志

查看最近 50 条日志：

journalctl -u xray -u node-panel -u cloudflared -n 50 --no-pager 

实时跟踪某个服务：

journalctl -u xray -f journalctl -u node-panel -f journalctl -u cloudflared -f 

如果服务启动失败，应先检查日志，确认原因后再处理，不要直接删除配置文件或重新安装。

4. 设置服务异常自动恢复

Python 面板的 systemd 服务已经配置：

Restart=always RestartSec=3 

因此 Python 进程异常退出后，systemd 会尝试在 3 秒后重新启动。

Xray 和 Cloudflared 是否自动重启，应以各自实际 systemd 单元的 Restart 配置为准。

查看配置：

systemctl show xray -p Restart -p RestartUSec systemctl show node-panel -p Restart -p RestartUSec systemctl show cloudflared -p Restart -p RestartUSec 

如果需要为 Xray 开启失败自动恢复，可以创建 systemd 覆盖配置：

systemctl edit xray.service 

写入：

[Service] Restart=on-failure RestartSec=5s 

保存后执行：

systemctl daemon-reload systemctl restart xray.service 

Cloudflared 也可以采用相同方式，但应先确认它确实由对应的 systemd 单元管理。

注意：不要对正在主动维护的服务随意执行 stop 或 disable。

5. 设置开机自动启动

检查服务是否已经启用：

systemctl is-enabled xray.service systemctl is-enabled node-panel.service systemctl is-enabled cloudflared.service 

如需启用：

systemctl enable xray.service systemctl enable node-panel.service systemctl enable cloudflared.service 

Cloudflared 如果不是通过 systemd 安装的，需要按照实际运行方式配置开机启动。

6. 安装脚本中途退出后的处理

本项目的 install-dual.sh 具有安装事务备份和失败回滚机制。

安装过程中会备份相关配置和服务文件。如果脚本发生可捕获的异常退出，通常会自动执行回滚，恢复安装前的状态。

备份目录通常位于：

/var/backups/node-panel-install-* 

如果出现安装失败，请先检查备份目录：

ls -ltd /var/backups/node-panel-install-* 2>/dev/null | head 

检查是否存在未完成的安装残留：

ls -l /usr/local/etc/xray/config.json ls -l /usr/local/bin/xray ls -l /opt/node-panel/panel.py ls -l /etc/node-panel/ systemctl status xray node-panel --no-pager 

如果脚本提示自动回滚失败，或者提示保留备份目录：

不要直接重新运行安装脚本。

不要删除 Xray、Python 面板或 Cloudflared 文件。

保留 /var/backups/node-panel-install-* 目录。

检查 Xray 和面板当前运行状态。

根据备份目录中的文件和 state.txt 确认安装前状态。

手动恢复失败的文件或服务后，再考虑重新安装。

特别注意：如果安装进程被 kill -9 强制终止，或者机器突然断电，Shell 的 EXIT trap 无法执行，自动回滚可能不会发生。

这种情况下必须先人工检查残留，不能假设脚本已经完成回滚。

7. 为什么第二次运行安装脚本会提示已有文件？

这是安装脚本的覆盖保护机制。

脚本会检查以下项目：

/usr/local/bin/xray

/usr/local/etc/xray/config.json

/opt/node-panel/panel.py

/etc/node-panel/

/etc/systemd/system/xray.service

/etc/systemd/system/node-panel.service

Xray 用户和用户组

已存在的 Xray 进程

只要检测到其中任意安装痕迹，就会停止安装，以免覆盖已有节点、密钥、账号或配置。

这不代表系统一定安装成功，也不代表这些文件都是无用残留。

8. 安全恢复和重新安装原则

如果确认上一次安装已经失败：

情况 A：自动回滚成功

检查服务和文件已经恢复到安装前状态后，可以重新运行安装脚本。

情况 B：自动回滚失败

先保留备份目录，人工核对文件和服务状态。确认恢复完成后再重新运行。

情况 C：强制终止或断电

先检查所有安装残留和服务状态，不能直接删除文件。

严禁为了绕过覆盖保护而直接执行：

rm -rf /usr/local/etc/xray rm -rf /etc/node-panel rm -rf /opt/node-panel 

这些目录可能包含正在使用的节点配置、面板认证信息和密钥。

核心原则：先检查、再恢复、最后重新安装。不要用删除文件的方式绕过安装保护。










Node Monitor Panel Backup
=========================

Contents:
1. panel.py
   Current panel source code.

2. node-panel.service
   Clean systemd service template without credentials.

IMPORTANT SECURITY NOTES:
- This archive does not contain auth.json.
- This archive does not contain secret.key.
- This archive does not contain Xray configuration.
- This archive does not contain Cloudflare Tunnel credentials.
- Existing login credentials and sessions cannot be restored from this archive.

INSTALLATION:
1. Create the directory:
   mkdir -p /opt/node-panel

2. Copy panel.py:
   cp panel.py /opt/node-panel/panel.py

3. Install the service template:
   cp node-panel.service /etc/systemd/system/node-panel.service

4. Ensure the authentication files are separately configured:
   /etc/node-panel/auth.json
   /etc/node-panel/secret.key

5. Reload and start:
   systemctl daemon-reload
   systemctl enable node-panel
   systemctl restart node-panel

6. Check service status:
   systemctl status node-panel

NOTE:
The service template assumes the panel runs as root and uses
/usr/bin/python3. Verify the paths and authentication files
before restoring on another machine.

#installation
use install.sh
