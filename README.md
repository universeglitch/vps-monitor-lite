# 服务异常恢复与安装失败处理

本项目使用 systemd 管理 Xray 和 Node Monitor Python 面板。Cloudflared 由用户自行安装和配置。

安装脚本 install-dual.sh 支持三种运行模式：

```bash
bash install-dual.sh            正常安装（交互式）
bash install-dual.sh --check    只读检查当前部署状态，不修改系统
bash install-dual.sh --recover  在新 Shell 中恢复中断或失败的安装事务
```

下面各节的命令与脚本实际实现保持一致。

## 1. 服务异常时如何手动恢复

如果发现某个服务停止或异常，可以单独重启对应服务，不需要重新运行安装脚本。

重启 Xray：

```bash
systemctl restart xray.service
```

重启 Python 面板：

```bash
systemctl restart node-panel.service
```

重启 Cloudflared（服务名称以实际安装为准）：

```bash
systemctl restart cloudflared.service
```

如果提示找不到 Cloudflared 服务，先检查：

```bash
systemctl list-units --all | grep -i cloudflared
```

## 2. 查看服务运行状态

分别检查：

```bash
systemctl status xray.service --no-pager -l
systemctl status node-panel.service --no-pager -l
systemctl status cloudflared.service --no-pager -l
```

也可以快速查看三个服务是否正在运行：

```bash
for s in xray node-panel cloudflared; do
  printf "%-16s " "$s"
  systemctl is-active "$s" 2>/dev/null || true
done
```

正常运行时应显示 xray active / node-panel active。Cloudflared 由用户自行管理。

## 3. 查看服务错误日志

查看最近 50 条日志：

```bash
journalctl -u xray -u node-panel -u cloudflared -n 50 --no-pager
```

实时跟踪某个服务：

```bash
journalctl -u xray -f
journalctl -u node-panel -f
journalctl -u cloudflared -f
```

如果服务启动失败，应先检查日志、确认原因后再处理，不要直接删除配置文件或重新安装。

## 4. 服务异常自动恢复（自愈）

Python 面板的 systemd 服务由安装脚本写入并已配置：

```ini
Restart=always
RestartSec=3
```

即面板进程异常退出后，systemd 会在 3 秒后自动重启。

Xray 的自动恢复通过 systemd drop-in 设置，不改写官方安装器生成的 xray.service：

```ini
# /etc/systemd/system/xray.service.d/90-restart-on-failure.conf
[Service]
Restart=on-failure
RestartSec=5s
```

查看各服务实际的 Restart 配置：

```bash
systemctl show xray -p Restart -p RestartUSec
systemctl show node-panel -p Restart -p RestartUSec
systemctl show cloudflared -p Restart -p RestartUSec
```

注意：安装脚本不会安装、修改或覆盖 Cloudflared 的任何配置，也不会为了设置 Restart 而覆盖用户已有的 systemd 自定义配置。不要对正在主动维护的服务随意执行 stop 或 disable。

## 5. 设置开机自动启动

检查服务是否已经启用：

```bash
systemctl is-enabled xray.service
systemctl is-enabled node-panel.service
systemctl is-enabled cloudflared.service
```

如需启用：

```bash
systemctl enable xray.service
systemctl enable node-panel.service
systemctl enable cloudflared.service
```

Cloudflared 如果不是通过 systemd 安装的，需要按照实际运行方式配置开机启动。

## 6. 安装状态检查：`bash install-dual.sh --check`

`--check` 为只读模式，只做检查，不会生成密钥、创建目录、修改权限、重启服务或删除任何文件。

它会检查：

- 系统版本与 CPU 架构
- 是否检测到 systemd
- Xray 程序、Xray 配置（JSON 是否有效）、xray.service 单元及其运行/启用状态
- Python 面板程序、面板配置、node-panel.service 单元及其运行/启用状态
- auth.json 与 secret.key 是否存在及权限是否为 600
- Cloudflared 单元（仅查看，不属于本脚本管理范围）
- /var/backups/node-panel-install-* 下是否存在未完成或回滚失败的事务
- 是否残留临时文件（如 /run/node-panel-password.*、*.new 等）

运行：

```bash
bash install-dual.sh --check
```

输出结论为以下之一：

- 未安装，可进行全新安装
- 已安装（完整），禁止重复安装
- 已安装，但服务当前异常
- 安装中断，存在未完成事务
- 自动回滚失败，需要人工处理
- 检测到不完整或不一致的部署状态

## 7. 安装事务备份目录内容

每次安装都会创建独立的事务目录：

```text
/var/backups/node-panel-install-<时间戳>.<随机值>/
```

目录权限为 700，内部包含：

```text
state.txt      事务状态文件（权限 600），字段包括：
               transaction_id, created_at, updated_at, status,
               current_stage, xray_was_active, xray_was_enabled,
               node_panel_was_active, node_panel_was_enabled,
               manifest_complete, hashes_complete
manifest.tsv   事务清单：每个被管理文件的“真实路径、备份相对路径、安装前是否存在、安装前指纹”
hashes.tsv     每个被管理文件的“安装后指纹”（用于判断目标是否被外部修改）
restore-journal.tsv  逐文件恢复日志（权限 600）：恢复动作与状态，使 --recover 可断点续跑
dirs.txt       本次安装新建的目录列表
meta.txt       记录 Xray 用户/用户组安装前是否存在
files/         被备份的文件副本
```

其中 manifest_complete 与 hashes_complete 是两个独立标记，含义不同，不能混用：

```text
manifest_complete=1   备份清单（含安装前指纹）已完整写入，可据以恢复
hashes_complete=1     所有受管文件的“安装后指纹”都已记录
```

安装中途被 SIGKILL / 断电时，通常 manifest_complete=1 而 hashes_complete=0：此时对“已改动但无安装后指纹”的文件，恢复会保守拒绝，绝不猜测其归属。

逐文件恢复日志 restore-journal.tsv 的每条记录包含：日志版本、源路径、恢复动作（RESTORE/REMOVE）、当前状态、安装前指纹、安装后指纹。状态取值：

```text
PENDING    尚未开始
INTENT     已持久化恢复意图，尚未确认完成
RESTORED   已成功恢复到安装前状态
REMOVED    原本不存在的文件已安全移除
```

该文件采用“临时文件 + 原子替换 + 回读校验 + 目录持久化”写入，因此任意时刻被 SIGKILL / 断电，日志要么是旧版本、要么是新版本，绝不会半写。

status 取值：

```text
IN_PROGRESS            安装进行中（正常退出会改为 ROLLED_BACK 或 COMMITTED；
                       kill -9 或断电会一直停留在此状态）
ROLLBACK_IN_PROGRESS   正在回滚
ROLLBACK_FAILED        回滚失败，需要人工处理（备份保留）
ROLLED_BACK            已成功回滚
COMMITTED              安装成功提交（之后不再自动回滚）
NOT_STARTED            保留状态
```

查看最新事务目录：

```bash
ls -ltd /var/backups/node-panel-install-* 2>/dev/null | head
```

## 8. 安装脚本中途退出后的处理

install-dual.sh 具有安装事务备份和失败回滚机制，行为取决于退出方式：

- 情况 A：脚本以错误码正常退出（例如某一步骤失败）→ 由 EXIT trap 自动回滚，恢复安装前状态，并写入 ROLLED_BACK。
- 情况 B：收到 Ctrl+C（SIGINT）或 SIGTERM → 尽力自动回滚（这两个信号可被捕获）。
- 情况 C：kill -9（SIGKILL）→ Shell 无法捕获，自动回滚不会发生，state.txt 停留在 IN_PROGRESS。
- 情况 D：断电 / 系统崩溃 → Shell 无法捕获，自动回滚不会发生，state.txt 停留在 IN_PROGRESS。

无论哪种情况，都必须先人工检查，不要假设脚本已经完成回滚，也不要直接删除文件。

检查步骤：

```bash
bash install-dual.sh --check

ls -l /usr/local/etc/xray/config.json
ls -l /usr/local/bin/xray
ls -l /opt/node-panel/panel.py
ls -l /etc/node-panel/
systemctl status xray node-panel --no-pager
```

如果 `--check` 提示“安装中断，存在未完成事务”或“自动回滚失败，需要人工处理”，请使用下一节的 `--recover` 进行恢复，恢复完成前不要重新安装。

## 9. 中断事务恢复：`bash install-dual.sh --recover`

`--recover` 可以在一个全新的 Shell 中独立运行，不依赖上一次安装进程的任何内存状态，它完全根据磁盘上的 state.txt / manifest.tsv / dirs.txt / meta.txt 来恢复。

运行：

```bash
bash install-dual.sh --check     # 先检查，确认存在未完成事务
bash install-dual.sh --recover   # 再恢复
```

`--recover` 会：

- 扫描 /var/backups/node-panel-install-*，列出状态为 IN_PROGRESS / ROLLBACK_IN_PROGRESS / ROLLBACK_FAILED 的事务，并显示创建时间、失败阶段、备份目录
- 校验备份完整性：逐项核对每个备份的类型、内容、权限、属主/属组与清单记录的安装前指纹是否一致（不只是判断“文件是否存在”）；任何一项损坏都会拒绝恢复，并明确报告是哪一个备份损坏
- 列出安装前服务状态，供你确认
- 需要你选择事务编号并显式输入 yes 确认后才执行
- 按该事务的清单恢复文件；只恢复清单中记录的文件，只删除清单中记录为“安装前不存在”的文件，并保留权限、属主、时间戳、符号链接属性
- 每个文件先写入并持久化恢复意图（INTENT），再以“同文件系统临时文件 + 原子替换”的方式还原，绝不把备份直接复制到正式路径（避免中断留下半截文件）；符号链接按链接本身还原，不跟随链接覆盖
- 恢复服务的启用/运行状态（依据 state.txt 记录），只涉及本事务管理的 Xray 与 node-panel，不触碰 Cloudflared
- 校验恢复结果，成功后写入 ROLLED_BACK，失败则写入 ROLLBACK_FAILED 并保留全部备份

恢复支持断点续跑：`--recover` 执行到一半再次被 SIGKILL / 断电时，下一次执行会依据持久化的 restore-journal.tsv 判断每个文件的进度，而不是只看 hashes.tsv：

- 已标记 RESTORED 的文件，只要当前确实等于安装前指纹，就跳过（不会因为“不再是安装后状态”而被误判为外部修改）
- 已标记 REMOVED 的文件，只要目标确实不存在，就跳过
- 处于 INTENT 的文件：若当前已等于安装前指纹，说明上次原子替换其实已完成，仅补记 RESTORED；若当前仍等于安装后指纹，则可安全重试
- 与安装前、安装后指纹都不一致，或缺安装后指纹又与安装前不一致，一律保守拒绝，绝不覆盖

关键持久化（写入状态文件、恢复日志、指纹记录、fsync）一旦失败会立即中止后续危险操作，不会静默忽略，也不会误报恢复成功。

恢复原则：

- 不会影响 Cloudflared
- 不会删除任何备份目录
- 不是通过 rm -rf 实现
- 如果备份不完整、备份内容与安装前指纹不一致、状态文件损坏或清单与目标不匹配，会停止并提示人工处理，不会猜测或强制恢复

## 10. 为什么第二次运行安装脚本会提示已有文件？

这是安装脚本的覆盖保护机制。脚本会检查以下项目：

```text
/usr/local/bin/xray
/usr/local/etc/xray/config.json
/opt/node-panel/panel.py
/etc/node-panel/（auth.json、secret.key、panel-config.json）
/etc/systemd/system/xray.service 与 xray.service.d
/etc/systemd/system/node-panel.service
Xray 用户和用户组
正在运行的 Xray 进程
```

只要检测到其中任意安装痕迹，就会停止安装，以免覆盖已有节点、密钥、账号或配置。脚本会把结果分类为：

- A. 完整安装 —— 已存在完整的部署，禁止重复安装
- B. 部分安装 —— 存在部分安装文件，可能上次中断，请先用 --check / --recover
- C. 残留文件 —— 只有孤立的程序/服务/用户等痕迹且缺少配置，脚本不会自动删除
- D. 未知部署 —— 无法明确归类，为保守起见停止安装，请人工确认

注意：Cloudflared 属于用户外部服务，其缺失或存在都不会被判定为 Xray 安装失败。“文件存在”不等于“可以删除”，也不等于“可以覆盖”。

## 11. 安全恢复和重新安装原则

- 情况 A：自动回滚成功（state.txt 为 ROLLED_BACK）→ 确认服务和文件已恢复到安装前状态后，可以重新运行安装脚本。
- 情况 B：自动回滚失败（state.txt 为 ROLLBACK_FAILED）→ 先保留备份目录，用 --recover 查看，必要时人工核对，确认恢复后再重新运行。
- 情况 C：强制终止或断电（state.txt 停留在 IN_PROGRESS）→ 先执行 --check，再执行 --recover，不要直接删除文件。

严禁为了绕过覆盖保护而直接执行：

```bash
rm -rf /usr/local/etc/xray
rm -rf /etc/node-panel
rm -rf /opt/node-panel
```

这些目录可能包含正在使用的节点配置、面板认证信息和密钥。

核心原则：先检查（--check）、再恢复（--recover）、最后重新安装。不要用删除文件的方式绕过安装保护。

## 仓库内容与凭据说明

本仓库仅包含程序与模板（panel.py、node-panel.service 等），**不携带任何凭据**：

- 不含 auth.json（登录凭据）
- 不含 secret.key（会话密钥）
- 不含 Xray 配置
- 不含 Cloudflare Tunnel 凭据

因此已有登录凭据与会话无法从本仓库恢复；这些文件由部署时生成，请自行妥善保管。
