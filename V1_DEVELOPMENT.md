# V1.0 plus implementation status

Baseline: main/plus `6d3d01b135f1bc5b3c04c149cd18e8dcdecc0002`.
Hardware ceiling: **200,000,000 bytes**, 1 vCPU, no Swap, 1 GB disk.
This is staged development, **not a production release or completed V1 acceptance**.

## Implemented

- auth.py: pinned-directory file store, no-follow reads, exact 0700/0600 ownership checks, stable flock, fsync/atomic authority replacement, fail-closed uncertain-commit state; explicit Argon2id v19 8192 KiB/t3/p1/16-byte salt/32-byte hash.
- Independent 24h server Sessions, epoch checks on every request, current-device logout, all-device invalidation after password/recovery/backup restoration.
- High-entropy once-returned recovery key; verifier only; bounded source/global rate state; global single Argon2 workspace.
- Per-API immutable AES-GCM ciphertext files, wrapped per-record DEK/KEK/LMK, random nonces/AAD; authority index deletion revokes new verifications immediately. Old ciphertext is conservatively retained, not prematurely garbage collected.
- Two verified local encrypted backup slots; explicit `BackupPending` distinguishes committed authority from failed event backup publication.
- Independent-password encrypted export and authenticated restore into an existing initialized vault; imported KEK rewrapped under local LMK, authority switched only after validation; Sessions not exported.
- Resource gate denies unmeasured profiles and unknown memory boundaries; bounded logical login/normal lanes; no automatic proxy stop or parameter downgrade.
- Active fixed unit allowlist, manual low-level control, heartbeat disabled by default, 60-second explicit monitor, two failures and bounded recovery attempts; no configuration writes.
- Loopback-only bounded HTTP entry, secure HttpOnly/SameSite Cookie, exact Host/Origin and CSRF checks, no request-secret logging; API list/create/read/delete UI and password change.

## Explicitly incomplete / safely refused

- Durable Xray transaction backend implemented and mock-tested: owned tags, fingerprint checks, candidate self-test callback, one-use epoch/config-bound restart confirmation, PREPARED/CONFIG_APPLIED/SERVICE_VERIFIED/COMMITTED/ROLLBACK_REQUIRED and bounded rollback/recovery. Production wiring, task cgroups and protocol forms remain incomplete; web starts remain refused. No guessed XHTTP/WS+REALITY/TLS parameters.
- Installer/updater task recipes and per-task cgroups not implemented. Requests refused, not silently delegated to legacy installation scripts.
- Continuous memory sampling/ancestor namespace validation, calibrated resource profiles and cgroup deployment policy need completion and target-machine testing. A resource profile is not supplied with invented measured values.
- Fresh-machine terminal external restore, local backup restore, LMK/KEK/DEK rotation and reference-aware cleanup implemented. Immutable old master objects remain accessible while latest/previous backups depend on them. Rotation/local-restore/disaster-restore tests pass in temporary directories; actual power loss remains unverified.
- PHC upgrades require an explicit future supported-parameter migration policy; current exact frozen PHC enforced, no silent downgrade.
- Session/API cardinality caps (32/64) are protective implementation limits, not tested capacity guarantees.
- Backups currently use a hard-bounded small envelope (payload 256 KiB, encrypted envelope 1 MiB), not a general streaming archive. Larger imports refused. No claim of streaming large data.
- Durable local backup latest/previous metadata transaction recovery and directory-data-set import need further failure-injection coverage; current metadata is advisory, ciphertext validated directly.
- New/legacy panel integration remains absent. panel.py is never imported, modified or exposed through the new server. Legacy port/auth remains independently managed; no claim of unified protection or dual-way synchronization.
- Web password recovery, bounded external export/import, local backup/restore, key rotation/cleanup and masked password change are connected. Secure-cookie/Host/Origin/CSRF tests run over local test HTTP with explicit headers, not a production TLS endpoint.
- Service sliders and all-on switch are deliberately disabled until readiness/config/restart confirmation paths are implemented. They do not pretend to succeed.
- UI uses specified blue/purple palette, cards and 2x2 layout; original V1 screenshot pixel-level comparison has not been performed. Password and recovery fields use masked modal inputs; original V1 screenshot visual acceptance still pending.
- stdlib HTTP frontend is not for direct Internet exposure; exact HTTPS trusted termination required. Post-init bootstrap is CLI-only. No TLS/PQ handshake implementation or verification.

## Frozen files and deployment

panel.py, install.sh, update.sh and node-panel.service are untouched. Legacy update.sh still references panel_dual.py although main renamed it to panel.py: observed pre-existing mismatch, not modified in this scope. Existing service starts the frozen legacy panel; no replacement service is installed automatically. No startup pip install/update.
Core dependency versions are pinned in requirements-v1.txt; transitive hashes, security maintenance and target Debian compatibility still require validation. Existing legacy panel dependencies remain unchanged.

## Tests and acceptance

CI executes Python syntax and isolated unittest discovery. No real systemd, Xray or Cloudflared actions in unit tests. A successful hosted runner is not evidence of 200 MB peak compliance.

Pending real-machine acceptance: cgroup memory.current/peak/events, MemAvailable, PSS/RSS, CPU, disk/inodes, login+proxy pressure, backup/import memory, second Xray test process, exact 200 MB no-Swap OOM behavior, install/update peaks, actual service recovery, interruption/power loss, Cloudflared coexistence, TLS hybrid negotiation. All remain **unverified**.

## Current unresolved integration — do not deploy as finished V1

- Six service sliders/all-on and legacy detail navigation are not fully connected. They remain visibly disabled instead of claiming success. Protocol parameters are not fabricated.
- Discovery and digest-checked confirmation-bound installer backend exist; no unmeasured universal package-manager recipe is enabled. General apt termination is unsafe. No automatic software installation occurred.
- Login resource gate lanes are bounded, but HTTP still has a shared two-worker pool; a long synchronous request can delay login. Dedicated request scheduling and continuous memory supervision are still required.
- LMK remains locally readable to the service owner. Full root compromise is outside encryption protection. No post-quantum handshake claim.
- Normal backup envelope bounded at 1 MiB; no streaming large-archive support. Data payload is capped at 256 KiB. These are enforced protective limits, not measured 200 MB guarantees.
- `Store` rejects symlink components during opening, but administrator-controlled ancestors and single process owner remain deployment assumptions. Interruption safety needs further fault injection and audit.
- No new service unit has been installed; the frozen legacy panel service remains unchanged. Do not expose the new listener directly to the Internet.


## 2026-11-09 双面板整合更新（取代前文 panel 冻结/未整合条目）

本轮明确解除 panel.py 冻结。main.py 与 panel.py 是两个独立页面实现，共用 main.Server 一个监听器、auth.Auth 的同一 24h Session 与 function.PanelBackend/XrayTransaction。页面状态切换，不使用 /main、/panel 页面路由或 iframe，不改变地址栏。内部 JSON API 路径不是页面路由。旧 panel 的诊断、浏览器目标检测、分享及下载区域保留；旧独立 PBKDF2/HMAC 认证及第二监听器移除。旧服务文件未改，部署应使用统一 main.py 入口，不能再启动旧 panel.py 服务器。

默认面板写入私有 panel-preferences.json。约300ms淡出/淡入，切换不启停服务；共享后端不建立第二套服务状态。节点名称保存在独立元数据，不改协议配置。没有完整真实节点时隐藏节点卡；二维码没有默认 src，用户点击后才鉴权生成 SVG，隐藏时移除 src；不是把预加载明文图仅 CSS 模糊。qrcode 8.0 延迟导入，原面板已有二维码能力，不新增常驻进程或 Pillow 需求。

模块管理展示未安装、未配置和配置错误等状态；只有配置识别完整的模块进入正常列表。官方 XTLS/Xray-core、cloudflare/cloudflared release 元数据内置，要求 sha256 digest，限制下载、重定向、大小、超时，流式摘要，zip只取受限普通xray文件，不执行远程脚本。panel下载固定到本轮已审查提交和内置SHA256。安装绝不覆盖已有文件、配置或unit。**安装程序二进制不代表完整服务安装成功**；unit/配置缺失仍显示未完成。若官方未提供digest、没有测量预算、任务cgroup/权限不可用则拒绝。没有在真实主机测试任何安装。

WS/XHTTP/RAW-TCP REALITY 图形向导接入候选预览、自测及一次性候选确认；不生成WS+REALITY。公网连接地址/端口必须输入，不猜测；REALITY公私钥在本地验证匹配。候选内容保留在有界服务端，展示差异不输出私钥。只允许本项目tag，原用户入站拒绝接管。端口修改列出实际配置，检测范围和bind占用，经同一事务确认；用户未选配置保留。确认取消不写停用定义。Cloudflare指导仅展示两条规则、复制按钮，不修改Tunnel/DNS/账户。

### 仍存在的明确限制（不是完整V1已完成）

- 安装器仅安全安装经校验的程序文件，不创建缺失的systemd unit、不猜测Tunnel配置；服务完整安装流程仍不完整。普通管理端口只展示，不能在线变更（需要监听/入口事务）；管理面板不能安全停止承载自己的进程，详细面板入口作为视图切换。
- Cloudflared启动验证器在版本/配置验证能力未认证时仍拒绝开启；停止需要用户确认。一键涉及用户自建入站时拒绝，涉及部分服务失败时明确报告部分提交，不伪装原子跨服务成功。
- 入站识别和节点完整性不是端到端连通性验证。XHTTP具体版本能力仍由部署机Xray自测决定。尚无真实协议互通测试。
- 生产写入需要 --single-writer 明确部署所有权；--task-memory-max 和 --task-slice 来自管理员实测及预建cgroup；没有预算不能测试或应用。
- 新入口采用loopback、可信HTTPS Origin，未进行TLS/PQ协商实测。内存调度与祖先cgroup可见性仍需审计，不能保证200MB绝不OOM。
- 本轮浏览器验证使用模拟服务和临时认证数据，没有真实服务操作。V1原始两张截图未取得像素级比对，不能声明UI完全一致。
- 旧 install.sh/update.sh/node-panel.service 原样保留；update.sh仍有旧panel_dual.py路径问题，未擅自改动。原node-panel.service直接启动panel.py在统一模块版不再适用；必须由新部署指引显式改部署入口，不能直接覆盖生产panel文件。
