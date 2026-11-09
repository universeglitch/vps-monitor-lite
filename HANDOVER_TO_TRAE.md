# vps-monitor-lite V1.0 开发交接：Sol → Trae

> 本文是开发交接，不是 V1.0 完成或生产安全验收证明。交接基于远端 `plus` 快照 `7632a8b21580c1da5c4f055bbc497572ec9ee31f`。本次仅新增本文，不改程序、不安装、不重跑整套测试、不合并 PR。后续开发暂停，由 Trae 接手。

## 1. 接手第一步与 Git 现场

### 1.1 实际核验结果

2026-11-09 UTC 进行 fetch、ls-remote、status、log/worktree 及关键源码核验：

| 项目 | 实际结果 |
| --- | --- |
| 远端 plus（本文提交前） | `7632a8b21580c1da5c4f055bbc497572ec9ee31f` |
| 远端 main | `6d3d01b135f1bc5b3c04c149cd18e8dcdecc0002` |
| 当前整合工作区 | `/workspace/v1-development/integration` |
| 整合工作区本地分支 | `v1-integration-local`，向远端 plus 推送 |
| 整合工作区本地 HEAD | 与本文提交前远端 plus 相同 |
| 整合工作区改文档前状态 | 干净，无未提交应用代码 |
| 旧工作区 | `/workspace/v1-development/repo`，本地分支 plus |
| 旧工作区 HEAD | `3a7cd37e2a6f5d2ada21515e384168b0becc3232`，未推送 |
| 旧工作区未跟踪项 | `__pycache__/`、`tests/__pycache__/`，未删除 |
| 文档发布 worktree | `/workspace/v1-development/doc-publish`，detached HEAD `d85bfa80118759c6f849087d8bf57e042b2aeed3`，干净 |

`git log --branches --not --remotes` 仅列出旧提交 `3a7cd37…`。该提交原来因 CLI OAuth 无 workflow scope 推送失败，包含旧 CI 更新、失败一次性工作流删除和当时文档。相应文档已另行通过 d85bfa8 发布；相对 d85bfa8，3a7cd37 仅多出旧 workflow 差异。**不要直接推送、reset 或 rebase 该旧现场；先比较，保留历史。**

本文提交后远端 HEAD 将是文档提交本身，可用 `git log -1 -- HANDOVER_TO_TRAE.md` 获取，无需在正文中自引用尚未产生的 SHA。源码快照仍由上面的固定 SHA 标识。

建议先只读执行：

```bash
git fetch origin plus main
git status --short
git log -12 --oneline origin/plus
git diff --stat 6d3d01b135f1bc5b3c04c149cd18e8dcdecc0002..origin/plus
```

注意：原克隆只跟踪 plus，`git fetch origin main` 不一定创建 `origin/main`。比较时使用查询到的完整 main SHA，或显式获取对应 refspec，不误把 bad revision 当成文件不存在。

### 1.2 重要提交

| 完整 commit SHA | 具体内容 |
| --- | --- |
| `42024994aa39dcc82eab96b5a78d4765e8d1aee7` | auth、依赖、认证测试与 CI 初版 |
| `6797c6e0f5173b2bbcaf3c997ce83b58ac1a7db2` | 协调、资源准入、active 和模拟测试 |
| `05416cbf1364d96e7e8f95b813074732fbc4d3b7` | 主页面与认证 API 初版 |
| `2a8e16a0299ad748d3d0bb051681c4f2961d5263` | 不依赖 assert 的安全验证；密钥轮换、本地恢复、对象清理 |
| `4957105603d9e2f9a0a547da8e3b201f79952b64` | Xray 持久化事务、有界回滚 |
| `a0f6a4bb44a678648a86223b852a2a8ff206551e` | 网页备份、恢复和密钥管理接入 |
| `4b40e950cea40d2b40527da998c5c0c565d9918d` | 新机器外部恢复、服务发现和受确认任务后端 |
| `d85bfa80118759c6f849087d8bf57e042b2aeed3` | 前期 Box 测试证据与准确开发状态 |
| `c8799bf0d32a0f0754b5237058cf164eac2bb05d` | 单监听器、统一认证和详细组件整合 |
| `b090bc233945d7c436c1e692b72265b139488478` | 向导、端口/节点/服务接线与测试 |
| `69db0353e03061761c8c5531e5945ba82ba806f4` | 官方来源、摘要校验和有界模块下载 |
| `d98d9c821c4c3053aa2f7ee26428fb3e83717704` | 公网节点元数据、REALITY 公私钥匹配、确认后持久化 |
| `50d0a3019d61b68bfc1f71a4e8533806ac506150` | 双面板浏览器证据与限制 |
| `7632a8b21580c1da5c4f055bbc497572ec9ee31f` | CI 对齐 panel 解冻，加入 -O 和旧部署文件哈希保护 |

历史 `f4e5616…`、`832417a…` 为未完成阶段补丁/验证工作流，不是已提交回应用源码的修复；实际应用修正后来在2a8e16a等完成。

## 2. 修改文件及当前真实架构

相对 main 基线，V1 开发修改/新增的文件：

| 文件 | 修改目的与实际职责 |
| --- | --- |
| main.py | 新统一 HTTP 入口、主 UI、Session/CSRF/Origin、API 调度、双面板 DOM 切换 |
| panel.py | 保留详细面板独立实现，移除旧独立认证/监听，适配共享后端，增加主功能块/端口块/节点遮罩 |
| function.py | Memory、ResourceGate、Coordinator、XrayTransaction、Discovery、任务执行、安装器和 PanelBackend |
| auth.py | Store、认证/Session/epoch、恢复密钥、API 密文、密钥轮换和备份恢复 |
| active.py | 固定 unit 的实际状态、手动启停、测活和有限恢复，不写配置 |
| requirements-v1.txt | 固定核心 argon2-cffi/cryptography 版本，以及按需 SVG 二维码 qrcode |
| README.md、V1_DEVELOPMENT.md | 开发范围、限制和待实机验收；有累积历史章节，最新状态应以本文/源码为准 |
| .github/workflows/v1-tests.yml | 普通/-O unittest、五模块语法及旧部署文件保护 |
| .github/workflows/v1-stage-verify-once.yml | 失败历史一次性工作流，仍在树中，需清理但不要盲目运行 |
| .gitignore | 缓存/venv 忽略 |
| tests/test_auth_v1.py | 文件存储、认证、epoch、API、备份/恢复、轮换和故障注入 |
| tests/test_control_v1.py | 资源拒绝、门释放、mock 启停/两次失败和测活关闭 |
| tests/test_xray_v1.py | mock 候选配置、确认、用户配置保留、外部修改、失败回滚 |
| tests/test_web_v1.py | 本地 HTTP Session/CSRF/Origin、单入口和默认面板测试 |
| tests/test_panels_v1.py | 节点/名称/QR/端口、取消保护和下载拒绝等 |
| tests/browser/dual_panel_smoke.py | Chromium 模拟后端的双面板功能测试，开发依赖 Playwright |
| tests/V1_BOX_EVIDENCE.md、tests/DUAL_PANEL_EVIDENCE.md | 已执行测试的完整日志，避免重复运行来伪装新证据 |

`install.sh`、`update.sh`、`node-panel.service` 在 plus 相对 main 基线无变化，旧 shell 测试和 old_version 也未由 V1 改动。本文不把旧安装脚本说成已支持新系统。

### 2.1 单监听与调用链

```text
可信 HTTPS 入口 / Cloudflared → 回环 main.Server（唯一监听）
  → Handler 的 Host/Origin、Session/epoch、CSRF 校验
  → Coordinator / PanelBackend
      → auth.Auth（认证、API、密钥、备份）
      → active.Active（状态/启停/测活）
      → XrayTransaction（候选、自测、确认、配置写入、回滚）
```

代理流量不经过 Python。main/panel 共用同一 coordinator/auth/active/store，未建立两套状态。

`main.py:cli()` 是新入口。GET `/` 返回主页面及封装为 `mountDetailed(root,bridge)` 的详细脚本；已认证 POST `/view` 返回独立详细标记/样式。`switchPanel()` 将详细组件装入同页面 ShadowRoot，不使用 iframe，不导航 /main 或 /panel，地址栏不变。两页约300ms过渡，切换不触发启停。内部 JSON API 路径允许存在，不等于通过不同 URL 页面路由切换。

`panel.py:render_components()` 从自己的 PAGE 提取组件，不导入 main.PAGE；`authorize()` 调统一 auth.validate。导入时不读取旧密码文件、不启动服务器。直接运行 panel.py 会提示启动 main.py 后退出。

`PanelBackend.preferences()` 将默认面板和独立节点名称写入私有 `panel-preferences.json`。刷新或重启后读取默认值；切换自身不注销 Session。

### 2.2 鉴权边界

`Handler.do_POST()` 除登录/恢复外先验证 Session 和CSRF；所有 POST 要求精确 Origin/Host。`Auth.session()` 每次检查24h绝对时限和epoch。GET `/`只返回页面代码，不返回节点秘密；view/nodes/share/qr等管理数据均鉴权。无数据库/JWT、无运行时自动pip。

当前 main Handler 有些认证/备份操作直接调用 auth，而服务/节点走 function。这是实际调用边界，**并非所有管理请求都已严格经 function 中转**，Trae需按冻结职责整理，不引入另一套认证。

### 2.3 配置和服务

- `XrayTransaction.confirm/configure/_replace/_rollback/recover`：固定受管tag、文件指纹、候选测试、阶段记录、一次性确认和有界回滚。
- `PanelBackend.schema/_inbound/preview/preview_mapping/apply_candidate/_apply_record`：图形表单→候选→确认→事务；公开端口/地址不猜测，REALITY公私钥本地匹配。底层候选绑定仍需审计，见风险。
- `PanelBackend.prepare_service/apply_service`：项目入站停用/恢复和Cloud操作。Xray共享内核不能以停止整个服务代替关闭某用户入站。跨服务部分提交明确报错，不是全局原子成功。
- `Active.tick()`：60秒、连续2次失败才尝试、窗口内最多3次、退避；Restart!=no时交给systemd；readiness不可用则不启动。desired/attempts目前只在内存，跨进程重启持久化未完成。
- `Memory/ResourceGate`：已读cgroup/可用内存并限制队列，但连续监督、login专用HTTP通道和祖先cgroup真实性未完成，不能承诺200MB安全容量。

### 2.4 部署兼容：优先修复

原 `node-panel.service` 的 ExecStart 仍为 `/usr/bin/python3 /opt/node-panel/panel.py`，Restart=always。新的panel.py不会监听；直接覆盖旧部署会退出并可能进入重启循环。`install.sh` 仍部署旧单面板方式，仅WS/REALITY选择，没有新四模块依赖和七套餐安装。`update.sh`下载URL仍是main/panel_dual.py，而main文件实际改名panel.py，这是已观察的旧路径不匹配。

**新服务应启动main.py，带独立data、resource-profile、port、HTTPS origin；尚无已验收的新统一部署/升级迁移。不要直接替换生产panel文件后沿用旧unit。** 必须先做备份、依赖、端口、回退及入口兼容。不能把测试里的nullcontext资源gate复制进生产。

main CLI还有 `--xray-config`、`--cloud-config`、`--single-writer`、`--task-memory-max`、`--task-slice`；这些需要真实部署与预算，不是可随意打开的开关。Cloudflared readiness 当前明确拒绝未认证的启动验证；不会自行改Tunnel。

## 3. 功能完成度（不是生产验收）

A=已实现且有对应隔离测试；B=实现但测试不足；C=部分/后端；D=尚未实现；E=真实环境缺失未验证。允许同一功能同时A+B/E，说明局部测试不覆盖整体。

| 功能 | 类别 | 文件/关键函数或接口 | 已有证据与剩余工作 |
| --- | --- | --- | --- |
| 初始化、登录、改密、Session/current/all/epoch | A+B | auth.initialize/login/session/change_password；main /login/session/password | AuthTests/WebTests；并发提交、锁、权限部署审查 |
| 恢复密钥/密码 | A+B | Auth.recover，/recover | 摘要/轮换/旧Session失效；网络中断后once语义仍须处理 |
| API管理 | A+B | create/read/delete/verify_api，/api/* | auth/web测试；API调用授权范围、竞态还需完善 |
| LMK/KEK/DEK轮换、清理 | A+B | rotate_keys/collect_objects | RotationTests；崩溃引用集合与事务并发待查 |
| 本地/外部备份、恢复、新机恢复 | A+B | backup/restore_local/export_backup/restore_backup/import_fresh | Auth/DisasterRestoreTests；两槽发布及断电一致性待补 |
| Xray事务和失败回滚 | A+B+C+E | XrayTransaction | 六个mock测试；不是实际Xray或断电实验，自动恢复启动接线不足 |
| WS/XHTTP/RAW REALITY向导 | A+B+C+E | schema/_inbound/preview/apply_candidate | 图形表单接入；无真实Xray兼容/协议互通验证；diff只显示主要端口差异 |
| Xray安装/启动停止 | C+E | ModuleInstaller、Active、prepare_service | 下载拒绝/摘要测试；只安装二进制、不建unit；用户旧入站不接管 |
| Cloudflared指导 | A | guide、cloudGuide | 两规则/复制/不修改配置；非自动配置 |
| 服务开关、一键 | C+E | prepare_service/apply_service | 项目tag基础可用；Cloud启动拒绝；跨服务部分成功不回滚全部 |
| 模块窗口/完整性下载 | A+C+E | modules、ModuleInstaller | 校验/不覆盖/取消测试；完整服务安装链路未完成 |
| 节点/名称/分享下载 | A+B | nodes/rename/share | 无假配置、独立名称；外部真实元数据一致性待验收 |
| QR隐私 | A+B | qr、panel loadConfig | 显式点击请求/隐藏移除src、无预加载；Session失效/导航清理仍需审查 |
| 端口 | A+C+E | ports/port_preview/check_port | 项目Xray入站mock；管理监听端口只读、Cloud端口不自动改 |
| 双面板同URL/默认持久化 | A | switchPanel/preferences/render_components | 本机HTTP+Chromium模拟、无iframe；非实机部署 |
| Panel功能启用/禁用 | D | 当前toggleService(panel)仅跳转 | 本文第6节Trae待办，不能称开关完成 |
| active自愈 | A+B+C+E | tick/set_service/set_heartbeat | 模拟两次失败/尊重stop；真实systemd及跨重启状态未验收 |
| 资源保护 | A+C+E | Memory/ResourceGate/BoundedCommand | fake memory拒绝测试；真实峰值、并发、OOM、任务清理待补 |
| 新统一部署/升级/七套餐 | D+E | 旧install/update/unit尚未适配 | P0部署兼容与Trae待办 |
| Dark Blue-Purple Glassmorphism对齐 | C | main/panel PAGE | 当前仍有亮色实心header和布局差异，用户已指出，不算视觉完成 |

## 4. 已有测试证据：不要重复昂贵测试

### 4.1 可读取证据与适用版本

- [tests/V1_BOX_EVIDENCE.md](tests/V1_BOX_EVIDENCE.md)：输入 `4b40e95…`，普通/-O各52次执行；是旧阶段证据。
- [tests/DUAL_PANEL_EVIDENCE.md](tests/DUAL_PANEL_EVIDENCE.md)：输入 `d98d9c8…`，普通/-O各82次执行，包含完整日志和浏览器结果。
- [tests/browser/dual_panel_smoke.py](tests/browser/dual_panel_smoke.py)：真实Chromium、本机临时vault/mock代理，通过HTTPS测试转发，不是生产TLS或真实Xray。
- [旧失败运行42515065378](https://github.com/universeglitch/vps-monitor-lite/actions/runs/42515065378/job/128836177049)：82 tests OK后旧panel冻结hash失败，不能说该运行成功。
- [当前成功运行42531334835](https://github.com/universeglitch/vps-monitor-lite/actions/runs/42531334835/job/128885978313)：输入7632a8b，本文整理时重新查询success，并取得原始job日志。普通82次/7.767s，优化82次/8.036s，均OK；不含真实代理操作。

### 4.2 命令、数量和限制

| 检查 | 实际命令/方式 | 已有结果 |
| --- | --- | --- |
| Python compile | `python -m py_compile auth.py function.py active.py main.py panel.py` | 退出0；静态检查，无“用例数” |
| 普通模式 | `python -m unittest discover -s tests -p 'test_*v1.py' -v` | 当前CI82次执行、82成功、0失败/错误、0skip |
| 优化模式 | `python -O -m unittest discover -s tests -p 'test_*v1.py' -v` | 当前CI82次执行、82成功、0失败/错误、0skip |
| JavaScript | `node --check /workspace/v1-development/combined-ui.js`（从两个PAGE组合提取） | 旧双面板阶段退出0；本次未重跑 |
| Browser | `/workspace/v1-development/venv/bin/python tests/browser/dual_panel_smoke.py` | 单脚本PASS；内部断言没有独立汇总计数 |
| Diff | `git diff --check` | 旧开发阶段退出0；本文提交前仅再次检查文档diff |
| 安全/故障恢复/事务 | 上述unittest内权限、符号链接、epoch、写失败、恢复、confirm、回滚 | 已包含在82次中，不重复累加或声称全覆盖 |

本轮AST静态统计（没有重新执行unittest）：52个源码声明的test方法；按继承发现82次执行，其中30次重复：RotationTests继承16、InstallerTests继承10、DualEntryTests继承4。**52是方法数，不是穷尽参数/分支后的独立安全场景数；82不是独立用例数量，更不是PASS断言数。** 不与早期shell脚本660/666 PASS断言统计混用。

浏览器证据覆盖同URL/一个HTTP监听、统一Session、双向切换、默认持久化、模块及Cloudflare窗口、QR无预加载/揭示/隐藏、无JS异常。不包含真实扫码设备、协议、像素级比较、生产Cookie/TLS协商验收。

Box历史日志（临时环境不保证永久保存）：`/workspace/v1-development/evidence/final-dual-normal.log`、`final-dual-optimized.log`、`final-browser.log`；本轮取得的CI日志为 `/workspace/v1-development/handover-audit/ci-42531334835.log`。仓库中的证据优先于临时路径。

**全部未真实验证：** 200,000,000 bytes RAM、无Swap、1vCPU限制下峰值/CPU、Xray真实安装/协议互通、Cloudflared运行、真实systemd启停、OOM、真实SIGINT/SIGTERM/SIGKILL全流程恢复、物理断电。Box合成callback失败不等于断电实验。本轮没有重跑完整测试，也没有复活演示后台。

## 5. 已知问题和风险排序

以下分为“源码已确认的缺口”与“待专项验证的风险”，不把推测冒充已证实攻击。

### P0：生产部署之前

1. **确认不兼容：旧unit启动新panel.py会退出。** 必须先完成main.py统一service部署/升级/回滚；不要直接覆盖生产文件。旧update URL不匹配同样必须修复。
2. **源码确认：Store.transaction每次嵌套都直接LOCK_EX/LOCK_UN。** RLock可重入不等于flock计数嵌套；内层结束会释放同一FD跨进程锁。preview/configure持Store锁调用authorize又进入同Store锁。应改最外层锁计数/统一锁顺序并做多进程竞态测试；本次未改代码。
3. **源码确认：Xray日志验证不完整。** pending仅验stage/hash/attempt，old/candidate/meta/was_active/owned及它们间一致性未完整验。恢复前必须强校验；日志损坏不能成为chmod/chown/重启输入。异常写日志失败后的恢复路径也需测试。
4. **待验证高风险：BoundedCommand终止systemd-run客户端不保证终止独立cgroup任务。** 应绑定唯一unit身份、显式取消并等待；不以杀客户端当后台任务已停止证明。

### P1：完整V1之前

5. **确认资源与登录缺口。** HTTP只有共享两个worker；长请求可阻塞登录。Memory把短期任何上升当pressure，祖先隐藏/根预算算法有保守拒绝或计量不准确风险；资源gate并发与队列需负载证明，不能按RSS推断安全。
6. **确认备份多步发布。** previous/latest/metadata不是一次原子提交；auth先commit再backup，BackupPending代表部分提交，不是“旧状态完全保持”。需断电/磁盘满矩阵和恢复选择规则。
7. **确认部分配置提交。** `_apply_record`在Xray COMMITTED后才写node_metadata/disabled_after。若元数据写失败，运行配置已变而恢复开关定义/节点连接信息可能未更新。应把必要元数据纳入事务或有持久恢复流程。
8. **待审计候选确认。** PanelBackend存candidate_hash，但_apply_record未显式比较底层重建candidate_hash；XrayTransaction底层confirm只绑定旧hash/epoch/session，不绑定replacements；还存在Coordinator直接入口。应统一严格候选摘要绑定，展示足够但脱敏的差异（当前主要只显示端口）。
9. **确认单写入者依赖部署。** `--single-writer`为管理员确认，不是对不守锁root的原子CAS；必须禁止其它写入入口，指纹检查仍有窗口。panel目前不再写配置，但其它工具未知。
10. **确认安装链路不完整。** 版本执行+文件安装不能替代unit/config/真正启动后验证；ModuleInstaller始终ready false。ZIP目录/压缩比/二进制版本和完整来源策略需要进一步审计；当前官方API digest缺失即拒绝。
11. **确认Panel管理开关/七套餐尚未实现。** 见第6节，不能把导航/文件存在当启用状态。
12. **确认Cloudflared启动拒绝。** CLI readiness未认证版本/配置验证方式，始终报错；不得为了消除错误跳过验证。一键可能先提交Xray后Cloud失败，只有部分提交提示，无完整跨服务回滚。
13. **确认端口仅部分。** 管理监听端口只读；bind检查与实际启动存在TOCTOU，IPv4/IPv6/UDP/同端口不同listen支持需要验证。不可改用户未选择服务。
14. **认证/密钥待审计。** 大包为有界一次性序列化，多副本内存仍需测；PHC严格固定无升级迁移；Sessions/auth元数据完整类型验证、epoch敏感提交再检查、once网络丢响应等需专项审查。无凭据日志测试不代表Python内存可安全擦除。
15. **GC待审计。** 有当前和两备份引用，但所有未完成事务/轮换/导出根、部分扫描后删除、错误master选择等需故障验证；不能因auth不引用就删除。外部完成包必须自包含密钥，新机恢复测试不等于所有历史版本都可恢复。
16. **确认自愈持久化不足。** desired/failure/attempts内存保存；重启重置。readiness抛AuthError/Busy而tick只捕获ServiceError可能终止监控线程（源码路径需回归验证）。不能宣称无限重启已在全部场景被防止。
17. **UI/秘密生命周期。** 用户已指出主面板亮色header偏离深色磨砂基准。已揭示QR/分享值可能在隐藏view中保留；Session失效后应清除敏感DOM，不能只等下次API拒绝。detailReady缓存可能让node/name/service状态返回时不重新加载，需刷新策略审查。
18. **HTTP边界。** stdlib不直接公网暴露；Cookie仅可信HTTPS。重复Host/Origin、慢头/body、异常JSON/HTTP头限量和工作线程停止清理需要安全测试；所有安全不靠按钮隐藏。恢复/备份等部分路径直接auth不经function，职责统一待整理。

### P2：维护与证据

19. 失败的一次性workflow仍存在，写权限和历史补丁上下文不适合重跑；先审查删除，保留提交历史，不要求用户发送Token。
20. 核心依赖有固定版本，但无完整传递hash锁和当前漏洞评估；目标Debian wheel/包体积待测。Playwright仅开发依赖，不能加到生产启动链路。
21. 原网络诊断会向第三方发送访客IP、浏览器访问固定目标，保留隐私告知和资源限制；不擅自增加诊断目标。Box演示Failed to fetch已观察为后端进程不在，不是认证测试通过后的生产结论。

## 6. 用户确认规则与 Trae 专属待办

### 6.1 不再讨论的冻结规则

- 1vCPU、200,000,000 bytes RAM（十进制）、无Swap、1GB，Xray/Cloudflared优先；内存不足拒绝任务，不自动停代理、不自动降Argon2参数。
- 无SQL（包括SQLite）、Redis、Celery、不必要后台进程/多Web Worker；main/panel/function/auth/active同一个管理服务，代理独立数据面。
- Argon2id v19、8192KiB/t3/p1、salt16/hash32；完整PHC、受限参数、单昂贵KDF、登录限速、有界队列、拒绝未知资源条件。
- 24h绝对Session、多设备独立、退出current、改密/恢复all；Session创建保存epoch，每次验证，提交新epoch后即时失效，清理可延迟。
- 受限本机/SSH首次初始化，不能公网抢占；恢复密钥安全随机、once、哈希验证、限速和事务轮换。
- API可逆AES-256-GCM，12byte每次新nonce、AAD绑定；随机ID防同秒覆盖；删除二次显示名称确认。不得擅加scope、有效期、禁用/改名功能。管理操作必须服务端鉴权/CSRF/Origin。
- 事件本地加密备份两份+独立密码外部导出；不备份活跃Session/明文密码/API；包含异地恢复所需密钥材料。主密钥和密文同机不防root完全失陷。
- 文件原子写/fsync/锁/no-follow/权限和多文件一致性；失败保留有效数据。密钥清理要查当前、两份备份、未完成事务/轮换及外部恢复依赖。
- TLS1.3与X25519+ML-KEM-768目标由支持的TLS终止层实现；auth不拼TLS，pqcrypto不是启动依赖；不宣传全链路PQ已实现。
- Xray配置只由function统一单写入者；候选/备份/自测/指纹/阶段日志/有界回滚。需要重启必须明确授权、绑定候选事务；拒绝/取消不改正式配置，不覆盖用户无关配置。
- Cloudflared只提供Zero Trust手动说明，两规则带复制；不自动改Tunnel/DNS/账户。main/panel共用同一管理端口。
- 测活60秒，连续2次失败后有界自愈；关闭只停周期检测/自动拉起，不stop服务、不改desired，按需查询和手动控制保留；重新开启先读实际状态。
- 两面板保留文件和独立实现，同Session/同后端，一监听一端口同URL，前端状态切换约300ms，无iframe或/main、/panel页面分流；默认持久化。
- panel.py冻结已被用户正式废止，但旧有效功能必须保留。没有配置隐藏节点，不展示虚构链接；名称独立持久化；二维码默认遮罩且无清晰预加载，显式点击才获取，可再次隐藏。
- Git仅plus，不改main、不擅自PR/合并/删除分支。断连先读实际状态；不重复实现已有功能，不将设计/接口/拒绝路径冒充完成。

### 6.2 尚待 Trae 实现：Panel 功能开关

当前是“源码存在=active”和跳转，**没有实现**下述正式要求：

- 未安装不显示正常启停开关；模块窗口显示实际缺失。
- 已安装显示真实功能启用状态，持久化启用/关闭。
- 关闭只禁用详细Panel功能与入口，不能停统一HTTP/main/auth/Xray。
- main/panel切换入口和默认Panel回退反映启用状态。
- 后端view/detail/node/相关功能门禁必须一致，不能只隐藏按钮。

### 6.3 尚待 Trae 实现：install.sh 七套餐

1. 全部基础配置（默认）：WS+TLS、XHTTP+TLS、RAW+REALITY。
2. WS+XHTTP：WS+TLS、XHTTP+TLS。
3. WS+REALITY：WS+TLS、RAW+REALITY。
4. XHTTP+REALITY：XHTTP+TLS、RAW+REALITY。
5. 仅WS+TLS。
6. 仅XHTTP+TLS。
7. 仅RAW+REALITY。

REALITY已确认 **VLESS + TCP（RAW）+ REALITY**。套餐是多个独立入站组合，不是WS+REALITY单入站。安装脚本负责整套新管理系统与套餐选择，复杂协议参数交main图形向导，不能猜端口/路径/证书/Reality值。

### 6.4 尚待 Trae 对齐：原始 Panel 视觉基准

参考固定main提交 `6d3d01b135f1bc5b3c04c149cd18e8dcdecc0002:panel.py` 的PAGE：深蓝黑背景、蓝紫径向渐变、深色半透明磨砂、backdrop-filter、柔和阴影/半透明细边框、蓝紫按钮、原圆角/间距/图标/布局层次。设计名 **Dark Blue-Purple Glassmorphism**；不改Liquid Glass/Bootstrap。主面板目前实心亮蓝紫header已被用户指出不符，需要修正，不是用户已接受的视觉。以原源码/截图同尺寸核验后再称完成。

## 7. Trae 接手建议和最终验收顺序

### 必须优先

1. 核对最新plus、旧未推送提交、CI，阅读本文+源码+diff；保存现场，勿强推。
2. P0部署兼容、Store嵌套锁/锁序、Xray日志完整性和任务cgroup生命周期，先加失败测试再改，不为了通过删除检查。
3. Panel功能开关与服务端门禁、统一unit/升级迁移、七套餐完整安装。新需求允许必要的安装改造，但不得覆写现有生产凭据或未经确认重启。
4. 补候选摘要真正绑定、元数据/配置事务一致性、备份/GC恢复、持续资源准入与login调度、自愈持久化。

### 可并行

- 在稳定业务API后做视觉对齐、说明文本、Cloudflare引导、测试补充和文档；避免两人同时改同一事务/配置写入核心。
- CLI部署配方与浏览器回归可分别开展，但部署前核对端口/路径/权限/预算。

### 测试环境分工

- Box/InstaBox：临时vault、mock服务、文件故障注入、多线程/多进程锁、受限容器、受控进程中断、浏览器/视觉。真实unit/包安装仅可抛弃且有快照的授权环境。
- 真实目标VPS：200MB十进制/noSwap/单核预算、Xray/Cloudflared并发流量和OOM、真实协议、Tunnel、权限/systemd启停、磁盘余量、安装升级回退、真实重启断电。不得用Box大内存通过替代。

验收顺序：源码安全审查 → 普通/-O自动化 → 认证/备份及文件故障 → 配置日志/回滚与确认 → 服务/任务cgroup/资源并发 → 浏览器/二维码/视觉 → 可回滚VM完整安装升级 → 200MB目标机负载/断电恢复 → 确认全部功能与证据 → 发布。

## 8. 交接完成边界

本次只新增本文，并做文档完整性、链接/结构、diff和Git状态核对；复用了已有测试/CI日志，不重跑完整项目。未改main，未PR或合并，未改应用代码，未丢弃旧未推送提交或缓存。

提交后请从本文所在commit获取最终SHA；整合工作区除本文提交外不应有未提交代码。旧工作区plus仍保留3a7cd37及未跟踪缓存，必须明确移交。临时凭据/演示vault留在Box，不加入Git或本文，不应作为生产认证数据。

### 8.1 本轮源码定位索引（快照7632a8b；后续行号可能变化）

| 核验点 | 源码位置 |
| --- | --- |
| 嵌套flock/存储提交 | auth.py:194–203、249–274 |
| epoch/备份提交 | auth.py:388–406、437–465、512–545 |
| 轮换/GC/新机恢复 | auth.py:650–795 |
| 资源准入/队列 | function.py:15–127 |
| 持久事务/确认/回滚 | function.py:178–372 |
| 任务进程生命周期 | function.py:408–445；main.py:282–300 |
| 模块状态/Panel硬编码active | function.py:641–669 |
| 候选预览/实际提交/附属元数据 | function.py:857–932 |
| 官方下载/无覆盖文件安装 | function.py:935–1067 |
| HTTP认证/CSRF/API分发 | main.py:123–236 |
| CLI部署/Cloudflared启动拒绝 | main.py:239–329 |
| 双面板切换/主面板视觉 | main.py:27–58 |
| 测活/重试/Restart协调 | active.py:111–178 |
| 详细组件/QR按需/保留诊断 | panel.py:81–305、406–639 |

本轮读取这些源码及现有日志，只做问题登记，不对上述风险发起修复。

**交接后暂停进一步开发，等待 Trae 接手。**
