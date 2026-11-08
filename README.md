# 服务异常恢复与安装失败处理

本项目使用 systemd 管理 Xray 和 Node Monitor Python 面板。Cloudflared 由用户自行安装和配置。

## 当前入口与历史版本

根目录保留当前安装入口 `install.sh`、更新脚本 `update.sh` 和面板源码 `panel_dual.py`（文件名使用下划线）。本次整理不改变这些文件的内容。

两份旧安装脚本按完整 Git Blob SHA 归档，内容保持不变，仅供历史查阅，不作为当前安装入口：

| 原路径 | 归档路径（文件名包含完整 Git Blob SHA） |
| --- | --- |
| `install-dual.sh` | [`old_version/install-dual-81bbe73b7803b77281a35e9d5679ae0676edbc0d.sh`](old_version/install-dual-81bbe73b7803b77281a35e9d5679ae0676edbc0d.sh) |
| `install-dual.bak` | [`old_version/install-dual-36c1cc4e34413a46008ca4a1918147cd37bde4aa.bak`](old_version/install-dual-36c1cc4e34413a46008ca4a1918147cd37bde4aa.bak) |

上述哈希是 Git Blob SHA，不是文件 SHA256；现有历史备份没有被覆盖。测试仍优先使用 `install.sh`，其旧文件回退逻辑未修改。`install.sh` 内少量历史提示仍包含 `install-dual.sh`，本次不改程序逻辑；实际运行请使用下面的 `install.sh` 命令。

安装脚本 install.sh 支持三种运行模式：

```bash
bash install.sh            正常安装（交互式）
bash install.sh --check    只读检查当前部署状态，不修改系统
bash install.sh --recover  在新 Shell 中恢复中断或失败的安装事务
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

## 6. 安装状态检查：`bash install.sh --check`

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
bash install.sh --check
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

文件指纹覆盖文件类型、内容、权限、属主/属组与纳秒级修改时间（mtime）：

```text
FILE:<sha256>|perm=<p>|owner=<u>:<g>|mtime=<m>    普通文件
LINK:<目标>|perm=<p>|owner=<u>:<g>|mtime=<m>      符号链接（只校验链接自身，不跟随链接）
DIR:<内容摘要>|perm=<p>|owner=<u>:<g>|mtime=<m>   目录
SPECIAL:<类型>|perm=<p>|owner=<u>:<g>|mtime=<m>   FIFO/socket/设备等特殊类型（恢复时明确拒绝自动替换）
MISSING                                          路径不存在
```

为兼容本次改动之前创建的历史事务（其指纹不含 mtime），比较指纹时若任一方缺少 mtime 字段，则忽略 mtime、仅比较其余属性。

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

install.sh 具有安装事务备份和失败回滚机制，行为取决于退出方式：

- 情况 A：脚本以错误码正常退出（例如某一步骤失败）→ 由 EXIT trap 自动回滚，恢复安装前状态，并写入 ROLLED_BACK。
- 情况 B：收到 Ctrl+C（SIGINT）或 SIGTERM → 尽力自动回滚（这两个信号可被捕获）。
- 情况 C：kill -9（SIGKILL）→ Shell 无法捕获，自动回滚不会发生，state.txt 停留在 IN_PROGRESS。
- 情况 D：断电 / 系统崩溃 → Shell 无法捕获，自动回滚不会发生，state.txt 停留在 IN_PROGRESS。

无论哪种情况，都必须先人工检查，不要假设脚本已经完成回滚，也不要直接删除文件。

检查步骤：

```bash
bash install.sh --check

ls -l /usr/local/etc/xray/config.json
ls -l /usr/local/bin/xray
ls -l /opt/node-panel/panel.py
ls -l /etc/node-panel/
systemctl status xray node-panel --no-pager
```

如果 `--check` 提示“安装中断，存在未完成事务”或“自动回滚失败，需要人工处理”，请使用下一节的 `--recover` 进行恢复，恢复完成前不要重新安装。

## 9. 中断事务恢复：`bash install.sh --recover`

`--recover` 可以在一个全新的 Shell 中独立运行，不依赖上一次安装进程的任何内存状态，它完全根据磁盘上的 state.txt / manifest.tsv / dirs.txt / meta.txt 来恢复。

运行：

```bash
bash install.sh --check     # 先检查，确认存在未完成事务
bash install.sh --recover   # 再恢复
```

`--recover` 会：

- 扫描 /var/backups/node-panel-install-*，列出状态为 IN_PROGRESS / ROLLBACK_IN_PROGRESS / ROLLBACK_FAILED 的事务，并显示创建时间、失败阶段、备份目录
- 校验备份完整性：逐项核对每个备份的类型、内容、权限、属主/属组与清单记录的安装前指纹是否一致（不只是判断“文件是否存在”）；任何一项损坏都会拒绝恢复，并明确报告是哪一个备份损坏
- 恢复前先校验清单与恢复日志：核对 manifest.tsv 与 hashes.tsv 的字段数量、路径去重、路径合法性与指纹格式，并逐条校验 restore-journal.tsv（必须恰好六列、版本合法、路径不重复、动作只能是 RESTORE/REMOVE、状态合法、RESTORE/REMOVE 与清单中安装前是否存在一致、pre 指纹与清单一致、post 指纹与 hashes.tsv 一致）；只要发现日志被损坏、截断或字段不完整，一律拒绝恢复，绝不猜测文件状态
- 列出安装前服务状态，供你确认
- 需要你选择事务编号并显式输入 yes 确认后才执行
- 按该事务的清单恢复文件；只恢复清单中记录的文件，只删除清单中记录为“安装前不存在”的文件，并保留权限、属主、时间戳、符号链接属性
- 每个文件先写入并持久化恢复意图（INTENT），再以“同文件系统临时文件 + 原子替换”的方式还原，绝不把备份直接复制到正式路径（避免中断留下半截文件）；符号链接按链接本身还原，不跟随链接覆盖
- 恢复服务的启用/运行状态（依据 state.txt 记录），只涉及本事务管理的 Xray 与 node-panel，不触碰 Cloudflared
- 在覆盖某个服务的配置文件之前，先停止对应服务：若 systemctl stop 失败、停止后服务仍处于 active 状态、或安装前未启用而 disable 失败，都会立即中止文件恢复，绝不在服务运行时覆盖其配置
- 校验恢复结果，成功后写入 ROLLED_BACK，失败则写入 ROLLBACK_FAILED 并保留全部备份；最终状态（ROLLED_BACK / ROLLBACK_FAILED）写入或回读校验失败时，不会报告“回滚成功”，而是以非零退出并明确提示状态持久化失败

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

## 验收记录与后续待验收项目

### 记录范围与证据来源

本节记录固定版本验证结果、外部 Box 隔离测试报告、用户提供的 Podman 容器实际检查记录，以及尚未完成的真实环境验收。这里只记录事实和后续计划，不代表所有部署环境均已验收通过。

本节验收对象为下述固定 commit 中的 `install.sh`；归档操作不修改该脚本或测试代码，也不扩大原验收范围。

### 1. 固定版本、静态检查与新机器检查

目标 commit：

```text
475f019c04249170cf421e968d6219e2e1292933
```

`install.sh` SHA256：

```text
6e9681bed20d692f778b7ec0841fb7ebac96501a213dcf0ea7a0d13892531a45
```

以下三个文件均已通过逐文件 `bash -n` 语法检查：

- `install.sh`
- `tests/run-recovery-tests.sh`
- `tests/recovery-boundary-tests.sh`

检查命令：

```bash
for f in install.sh tests/run-recovery-tests.sh tests/recovery-boundary-tests.sh; do
  echo "检查：$f"
  bash -n "$f" || exit 1
done
```

完整输出为：

```text
检查：install.sh
检查：tests/run-recovery-tests.sh
检查：tests/recovery-boundary-tests.sh
```

语法检查退出码为 `0`。这是静态语法检查，不等于真实 VPS 安装或恢复验收。

根据用户提供的新机器实际检查记录，已完成以下检查；环境细节见下方 Podman 记录：

- `--help` 正常。
- `--check` 正常，能识别 Debian 13 x86_64 和 systemd。
- 能识别 Xray、面板及 Cloudflared 均未安装。
- 能扫描事务目录并报告无未完成事务。
- 最终判断为“未安装，可进行全新安装”。
- 检查时没有发现事务备份目录。
- 已核对代码路径：`--check` 在正常安装锁获取之前执行检查并直接退出。

以上只记录检查结果和代码路径核对结论，**不表示整个系统绝对零写入已经得到证明**。

### 2. 外部 Box 隔离测试报告

报告固定在上述目标 HEAD 执行，统计如下：

| 环境 | PASS 断言 | FAIL 断言 | SKIP |
| --- | ---: | ---: | ---: |
| 普通用户 | 660 | 0 | 2 |
| root mock | 666 | 0 | 0 |

普通用户的两个 SKIP 为需要 root 权限的测试（T0、T4）；root mock 运行没有 SKIP。**660 和 666 是 PASS 断言数量，不是测试用例数量。**

T33–T43 边界测试在普通用户和 root 两种身份下均执行，报告覆盖：

- 服务缺失时的状态恢复和安装前服务状态保护。
- 文件恢复失败时阻断服务启动。
- daemon-reload 失败时阻断服务启动。
- enabled 状态恢复失败时阻断服务启动。
- `ROLLBACK_IN_PROGRESS` 事务状态写入失败保护。
- `meta.txt`、`state.txt` 元数据和服务状态字段损坏保护。
- `manifest.tsv`、`hashes.tsv` 的格式、末行截断及换行边界。
- Xray 启动成功但 Panel 启动失败时，整体返回失败而不误报回滚成功。
- 拒绝 `SERVICE` journal action。
- 同一路径多条合法 `post` 记录仍以最后一条为准。

**来源与限制：这些数据以外部 Box 环境提供的隔离测试报告归档，使用临时目录和 mock，不代表真实 systemd、真实账户管理或物理断电测试。本次 README 更新中，本助手没有独立取得 Box 原始日志，不能将上述外部报告表述为本次更新亲自复现的结果。**

外部报告给出的日志位置为 Box 内的 `/workspace/validation/final-evidence.J9pPIS/`，包括 `user-full.plain.log`、`root-full.plain.log`、实际命令及版本核对记录。这些是外部环境路径，不是仓库附件或保证长期可访问的证据存档。

### 3. Podman 容器实际检查记录

日期：**2026-11-08**。以下为用户提供的容器实际检查记录；本次文档更新未另行复现该容器检查。

| 检查项 | 实际记录 |
| --- | --- |
| `systemd-detect-virt` | `podman` |
| PID 1 | `systemd` |
| `systemctl is-system-running` | `running` |
| 运行服务 | SSH、systemd-journald |
| 根文件系统 | `overlay` |
| cgroup `memory.max` | `134217728` |
| cgroup `memory.swap.max` | `134217728` |
| 内存限制 | 128 MiB |
| Debian | 13.7 |
| 内核 | `6.12.41+deb13-amd64` |

该容器已完成：

- `--help`。
- `--check`。
- 脚本 SHA256 核对。

**没有执行正式安装。** 虽然容器内部运行 systemd，但 Podman 容器不等同完整 VM 或独立 VPS；`overlay` 文件系统和 128 MiB 内存限制也使其不能代表真实安装、磁盘持久化及异常断电验收。

### 4. 后续待验收清单

以下项目全部为**未验收**。不得使用 mock 通过结果替代真实环境验收，也不能根据上面的检查输出推断这些项目已经通过。

#### A. 完整安装——未验收

在完整 Debian 13 VM 或独立 VPS 上验证：

- [ ] 全新安装。
- [ ] Xray 实际安装及配置测试。
- [ ] Xray systemd 服务的 active/enabled 状态。
- [ ] node-panel 实际启动。
- [ ] `/usage` 返回 HTTP 200。
- [ ] WS 和 REALITY（启用时）端口监听。
- [ ] `auth.json` 内容结构、非空、属主和 `600` 权限；`secret.key` 的预期密钥格式/长度、非空、属主和 `600` 权限。
- [ ] 事务最终状态为 `COMMITTED`。
- [ ] 安装完成后重新运行 `--check`，记录实际结果。

#### B. 真实 systemd 回滚——未验收

在具有可恢复快照的环境中验证：

- [ ] 真实 EXIT trap 回滚。
- [ ] 文件和目录恢复。
- [ ] Xray 用户和组恢复。
- [ ] 服务 active/enabled 状态恢复。
- [ ] stop、disable、daemon-reload、start 等操作失败时，危险恢复步骤是否被阻断。
- [ ] Xray 启动成功但 Panel 启动失败时的真实回滚。

#### C. SIGINT / SIGTERM / SIGKILL 恢复——未验收

在可回滚快照环境中分别测试：

- [ ] SIGINT。
- [ ] SIGTERM。
- [ ] 不同安装阶段 SIGKILL。
- [ ] 新 Shell 执行 `--recover`。
- [ ] 恢复过程中再次 SIGKILL。
- [ ] 恢复中断后能否安全断点续跑。
- [ ] 核对实际结果对应的最终状态 `ROLLED_BACK` 或 `ROLLBACK_FAILED`，并核对文件、账户和服务结果。

不能只根据进程退出码判断恢复成功。

#### D. 真实磁盘持久化和异常重启——未验收

在支持快照和控制台恢复的测试 VM 中验证：

- [ ] 事务状态持久化。
- [ ] journal 持久化。
- [ ] 文件指纹持久化。
- [ ] fsync 实际行为。
- [ ] 受控重启或异常断电后，`--check` 能否发现未完成事务。
- [ ] `--recover` 能否安全恢复并避免覆盖外部修改。

**mock 的 fsync 替身不构成真实磁盘持久化证据。**

#### E. Cloudflared 共存保护——未验收

当前测试容器没有 Cloudflared，因此此项尚未验收。后续在测试 VM 中依次执行：

1. 预先安装并运行 Cloudflared。
2. 记录配置、进程、服务状态和文件指纹。
3. 执行正常安装。
4. 执行失败回滚。
5. 执行恢复。
6. 检查 Cloudflared 配置、进程、服务状态和相关文件是否完全未被修改、停止、重启或删除。

- [ ] 完成上述全部步骤，保存前后对照和日志。

#### F. 发行版与资源边界——未验收

- [ ] 在完整 Debian 13 环境重新验收。
- [ ] 评估低内存机器的安装行为。
- [ ] 记录内存峰值、OOM 和失败回滚情况。
- [ ] 补充 ShellCheck 静态检查；当前尚未执行。

### 5. PANEL_URL 的版本固定问题

当前 `install.sh` 的 `PANEL_URL` 指向 GitHub 的 `main` 分支，而不是固定提交。本次通过 GitHub 文件元数据和目标提交核对，目标提交与当时 `main`（`362266e46b06417108195c0569447e5431289861`）中的 `panel_dual.py` Blob SHA 相同：

```text
45be400e2e0cc1716b7fa99506c3868bd854498a
```

该一致性是当时文件内容的核对结果，不保证之后的 `main` 仍然相同。安装时仍依赖可变的 `main` 引用；如果要求完全可复现，正式验收前应将面板下载地址固定到经过核对的 commit。

修改 `PANEL_URL` 属于后续代码任务；本次 README 更新没有修改 `install.sh`。

### 6. 后续真实验收记录规范

每次真实验收至少记录：

- 日期。
- 机器类型（完整 VM / 独立 VPS / 容器）。
- 发行版和版本。
- 内存限制。
- 目标 commit SHA。
- 脚本 SHA256。
- 执行命令。
- 实际输出和完整日志。
- 事务最终状态。
- 快照编号。

未执行项目保持“未验收”；失败项目必须记录原因和日志，不允许用 mock 通过结果替代真实环境验收。

## V1.0 plus 开发版本（尚未完成生产验收）

新模块为 `main.py`、`function.py`、`auth.py`、`active.py`，冻结的 `panel.py` 不导入、不修改。实现范围、仍未接通的服务开关/安装配方/任务 cgroup 和 UI 视觉验收限制见 [开发状态](V1_DEVELOPMENT.md)。不能将安全拒绝路径描述为完整功能。

### 开发检查与运行

在隔离开发环境中显式安装 `requirements-v1.txt`，运行 Python unittest；服务启动不会自动 pip 安装或升级。`main.py --help` 列出绑定地址、HTTPS Origin、端口和资源配置参数。生产启动必须绑定回环，由可信 HTTPS 入口转发，并提供管理员实测的资源预算文件；缺少预算的重操作明确拒绝，不内置伪造测量值。

首次初始化使用 `--initialize`，只允许交互式本机终端；恢复密钥仅本次输出。灾难恢复使用 `--restore-fresh <外部加密包>`，仅空 vault 可恢复。数据目录不可指向旧面板认证目录，避免格式覆盖。初始化/恢复同样需要显式数据路径、端口、Origin 与资源预算参数。

待实机验收：200,000,000 bytes 无 Swap 峰值、cgroup 祖先约束、OOM、代理高负载、真实 Xray 配置测试和重启回滚、Cloudflared 共存、磁盘写满/断电、UI 两张 V1 基准比对、安装更新峰值及权限部署。没有真实 VPS 测试结果。
