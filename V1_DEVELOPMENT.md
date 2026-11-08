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

- Durable Xray PREPARED/CONFIG_APPLIED/SERVICE_VERIFIED/COMMITTED/ROLLBACK_REQUIRED transaction and executable readiness validation are not implemented. Coordinator rejects configuration/start routes until implemented; no guessed XHTTP/WS+REALITY/TLS parameters.
- Installer/updater task recipes and per-task cgroups not implemented. Requests refused, not silently delegated to legacy installation scripts.
- Continuous memory sampling/ancestor namespace validation, calibrated resource profiles and cgroup deployment policy need completion and target-machine testing. A resource profile is not supplied with invented measured values.
- Fresh-machine external restore CLI, local-backup restore, LMK/KEK rotation and reference-aware garbage collection not yet implemented. No premature deletion of old keys/objects.
- PHC upgrades require an explicit future supported-parameter migration policy; current exact frozen PHC enforced, no silent downgrade.
- Session/API cardinality caps (32/64) are protective implementation limits, not tested capacity guarantees.
- Backups currently use a hard-bounded small envelope (payload 256 KiB, encrypted envelope 1 MiB), not a general streaming archive. Larger imports refused. No claim of streaming large data.
- Durable local backup latest/previous metadata transaction recovery and directory-data-set import need further failure-injection coverage; current metadata is advisory, ciphertext validated directly.
- New/legacy panel integration remains absent. panel.py is never imported, modified or exposed through the new server. Legacy port/auth remains independently managed; no claim of unified protection or dual-way synchronization.
- Web password recovery, backup/restore and key-rotation dialogs are not connected. Existing methods are not exposed without approved auth/CSRF flows.
- Service sliders and all-on switch are deliberately disabled until readiness/config/restart confirmation paths are implemented. They do not pretend to succeed.
- UI uses specified blue/purple palette, cards and 2x2 layout; original V1 screenshot pixel-level comparison has not been performed. Password-change prompt fields require replacement by masked modal controls before production.
- stdlib HTTP frontend is not for direct Internet exposure; exact HTTPS trusted termination required. Post-init bootstrap is CLI-only. No TLS/PQ handshake implementation or verification.

## Frozen files and deployment

panel.py, install.sh, update.sh and node-panel.service are untouched. Legacy update.sh still references panel_dual.py although main renamed it to panel.py: observed pre-existing mismatch, not modified in this scope. Existing service starts the frozen legacy panel; no replacement service is installed automatically. No startup pip install/update.
Core dependency versions are pinned in requirements-v1.txt; transitive hashes, security maintenance and target Debian compatibility still require validation. Existing legacy panel dependencies remain unchanged.

## Tests and acceptance

CI executes Python syntax and isolated unittest discovery. No real systemd, Xray or Cloudflared actions in unit tests. A successful hosted runner is not evidence of 200 MB peak compliance.

Pending real-machine acceptance: cgroup memory.current/peak/events, MemAvailable, PSS/RSS, CPU, disk/inodes, login+proxy pressure, backup/import memory, second Xray test process, exact 200 MB no-Swap OOM behavior, install/update peaks, actual service recovery, interruption/power loss, Cloudflared coexistence, TLS hybrid negotiation. All remain **unverified**.
