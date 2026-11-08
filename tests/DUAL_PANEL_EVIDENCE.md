# 双面板阶段实际测试证据

代码输入HEAD：`d98d9c821c4c3053aa2f7ee26428fb3e83717704`。

Simular Box隔离开发环境，不是真实VPS。临时vault、模拟服务和本机HTTP代理；没有运行真实Xray/Cloudflared或安装操作。

命令：Python py_compile五个模块；unittest发现test_*v1.py；python -O相同测试；node --check组合JavaScript；Playwright tests/browser/dual_panel_smoke.py；git diff --check。

普通与-O各82次执行（含继承重复用例，不是82个独立场景）；均退出0。浏览器检查同URL/单监听、统一鉴权、双向切换、默认持久化、模块与Cloudflare窗口、二维码无预加载/点击显示隐藏、无JS异常。没有像素基准或真实代理结论。

## final-dual-normal.log

```text
test_ancestor_symlink_refused (test_auth_v1.AuthTests.test_ancestor_symlink_refused) ... ok
test_api_independent_objects_and_delete (test_auth_v1.AuthTests.test_api_independent_objects_and_delete) ... ok
test_backup_failure_explicit (test_auth_v1.AuthTests.test_backup_failure_explicit) ... ok
test_backup_previous_valid_and_nonce_fresh (test_auth_v1.AuthTests.test_backup_previous_valid_and_nonce_fresh) ... ok
test_epoch_immediate_without_cleanup (test_auth_v1.AuthTests.test_epoch_immediate_without_cleanup) ... ok
test_export_restore_and_wrong_password_preserve (test_auth_v1.AuthTests.test_export_restore_and_wrong_password_preserve) ... ok
test_fixed_phc_and_no_plaintext (test_auth_v1.AuthTests.test_fixed_phc_and_no_plaintext) ... ok
test_initialization_cannot_overwrite (test_auth_v1.AuthTests.test_initialization_cannot_overwrite) ... ok
test_missing_gate_denies (test_auth_v1.AuthTests.test_missing_gate_denies) ... ok
test_oversized_kdf_rejected (test_auth_v1.AuthTests.test_oversized_kdf_rejected) ... ok
test_post_replace_sync_failure_poisoned (test_auth_v1.AuthTests.test_post_replace_sync_failure_poisoned) ... ok
test_pre_replace_failure_old_authority_survives (test_auth_v1.AuthTests.test_pre_replace_failure_old_authority_survives) ... ok
test_rate_limited (test_auth_v1.AuthTests.test_rate_limited) ... ok
test_recovery_rotation (test_auth_v1.AuthTests.test_recovery_rotation) ... ok
test_sessions_logout_current_expiry (test_auth_v1.AuthTests.test_sessions_logout_current_expiry) ... ok
test_symlink_hardlink_mode_and_duplicate_rejected (test_auth_v1.AuthTests.test_symlink_hardlink_mode_and_duplicate_rejected) ... ok
test_new_machine_restore (test_auth_v1.DisasterRestoreTests.test_new_machine_restore) ... ok
test_ancestor_symlink_refused (test_auth_v1.RotationTests.test_ancestor_symlink_refused) ... ok
test_api_independent_objects_and_delete (test_auth_v1.RotationTests.test_api_independent_objects_and_delete) ... ok
test_backup_failure_explicit (test_auth_v1.RotationTests.test_backup_failure_explicit) ... ok
test_backup_previous_valid_and_nonce_fresh (test_auth_v1.RotationTests.test_backup_previous_valid_and_nonce_fresh) ... ok
test_epoch_immediate_without_cleanup (test_auth_v1.RotationTests.test_epoch_immediate_without_cleanup) ... ok
test_export_restore_and_wrong_password_preserve (test_auth_v1.RotationTests.test_export_restore_and_wrong_password_preserve) ... ok
test_fixed_phc_and_no_plaintext (test_auth_v1.RotationTests.test_fixed_phc_and_no_plaintext) ... ok
test_gc_keeps_previous_backup_keys (test_auth_v1.RotationTests.test_gc_keeps_previous_backup_keys) ... ok
test_initialization_cannot_overwrite (test_auth_v1.RotationTests.test_initialization_cannot_overwrite) ... ok
test_missing_gate_denies (test_auth_v1.RotationTests.test_missing_gate_denies) ... ok
test_oversized_kdf_rejected (test_auth_v1.RotationTests.test_oversized_kdf_rejected) ... ok
test_post_replace_sync_failure_poisoned (test_auth_v1.RotationTests.test_post_replace_sync_failure_poisoned) ... ok
test_pre_replace_failure_old_authority_survives (test_auth_v1.RotationTests.test_pre_replace_failure_old_authority_survives) ... ok
test_rate_limited (test_auth_v1.RotationTests.test_rate_limited) ... ok
test_recovery_rotation (test_auth_v1.RotationTests.test_recovery_rotation) ... ok
test_rotation_preserves_api_and_backups (test_auth_v1.RotationTests.test_rotation_preserves_api_and_backups) ... ok
test_sessions_logout_current_expiry (test_auth_v1.RotationTests.test_sessions_logout_current_expiry) ... ok
test_symlink_hardlink_mode_and_duplicate_rejected (test_auth_v1.RotationTests.test_symlink_hardlink_mode_and_duplicate_rejected) ... ok
test_gate_releases_on_failure (test_control_v1.ControlTests.test_gate_releases_on_failure) ... ok
test_heartbeat_off_no_service_calls (test_control_v1.ControlTests.test_heartbeat_off_no_service_calls) ... ok
test_manual_stop_does_not_self_heal (test_control_v1.ControlTests.test_manual_stop_does_not_self_heal) ... ok
test_missing_profile_denies (test_control_v1.ControlTests.test_missing_profile_denies) ... ok
test_pressure_denies (test_control_v1.ControlTests.test_pressure_denies) ... ok
test_two_failures_before_restart (test_control_v1.ControlTests.test_two_failures_before_restart) ... ok
test_unknown_service_denied (test_control_v1.ControlTests.test_unknown_service_denied) ... ok
test_cancel_config_does_not_write_disabled_state (test_panels_v1.InstallerTests.test_cancel_config_does_not_write_disabled_state) ... ok
test_checksum_mismatch_no_install (test_panels_v1.InstallerTests.test_checksum_mismatch_no_install) ... ok
test_cloud_guide_does_not_mutate (test_panels_v1.InstallerTests.test_cloud_guide_does_not_mutate) ... ok
test_default_persisted (test_panels_v1.InstallerTests.test_default_persisted) ... ok
test_download_host_rejected (test_panels_v1.InstallerTests.test_download_host_rejected) ... ok
test_import_no_legacy_auth_files_and_no_listener (test_panels_v1.InstallerTests.test_import_no_legacy_auth_files_and_no_listener) ... ok
test_name_changes_only_metadata (test_panels_v1.InstallerTests.test_name_changes_only_metadata) ... ok
test_no_secret_or_qr_preload (test_panels_v1.InstallerTests.test_no_secret_or_qr_preload) ... ok
test_node_hidden_without_real_config (test_panels_v1.InstallerTests.test_node_hidden_without_real_config) ... ok
test_panel_install_confirmation_required (test_panels_v1.InstallerTests.test_panel_install_confirmation_required) ... ok
test_preview_candidate_bound_and_cancel_no_change (test_panels_v1.InstallerTests.test_preview_candidate_bound_and_cancel_no_change) ... ok
test_qr_on_demand_auth (test_panels_v1.InstallerTests.test_qr_on_demand_auth) ... ok
test_unowned_port_rejected (test_panels_v1.InstallerTests.test_unowned_port_rejected) ... ok
test_unsupported_protocol_no_change (test_panels_v1.InstallerTests.test_unsupported_protocol_no_change) ... ok
test_cloud_guide_does_not_mutate (test_panels_v1.PanelTests.test_cloud_guide_does_not_mutate) ... ok
test_default_persisted (test_panels_v1.PanelTests.test_default_persisted) ... ok
test_import_no_legacy_auth_files_and_no_listener (test_panels_v1.PanelTests.test_import_no_legacy_auth_files_and_no_listener) ... ok
test_name_changes_only_metadata (test_panels_v1.PanelTests.test_name_changes_only_metadata) ... ok
test_no_secret_or_qr_preload (test_panels_v1.PanelTests.test_no_secret_or_qr_preload) ... ok
test_node_hidden_without_real_config (test_panels_v1.PanelTests.test_node_hidden_without_real_config) ... ok
test_preview_candidate_bound_and_cancel_no_change (test_panels_v1.PanelTests.test_preview_candidate_bound_and_cancel_no_change) ... ok
test_qr_on_demand_auth (test_panels_v1.PanelTests.test_qr_on_demand_auth) ... ok
test_unowned_port_rejected (test_panels_v1.PanelTests.test_unowned_port_rejected) ... ok
test_unsupported_protocol_no_change (test_panels_v1.PanelTests.test_unsupported_protocol_no_change) ... ok
test_bootstrap_not_exposed (test_web_v1.DualEntryTests.test_bootstrap_not_exposed) ... ok
test_missing_config_hides_nodes (test_web_v1.DualEntryTests.test_missing_config_hides_nodes) ... ok
test_origin_rejected (test_web_v1.DualEntryTests.test_origin_rejected) ... ok
test_same_entry_no_panel_route (test_web_v1.DualEntryTests.test_same_entry_no_panel_route) ... ok
test_session_and_csrf (test_web_v1.DualEntryTests.test_session_and_csrf) ... ok
test_unauthenticated_direct_status_denied (test_web_v1.DualEntryTests.test_unauthenticated_direct_status_denied) ... ok
test_bootstrap_not_exposed (test_web_v1.WebTests.test_bootstrap_not_exposed) ... ok
test_origin_rejected (test_web_v1.WebTests.test_origin_rejected) ... ok
test_session_and_csrf (test_web_v1.WebTests.test_session_and_csrf) ... ok
test_unauthenticated_direct_status_denied (test_web_v1.WebTests.test_unauthenticated_direct_status_denied) ... ok
test_commit_preserves_unrelated (test_xray_v1.XrayTests.test_commit_preserves_unrelated) ... ok
test_existing_unowned_tag_rejected (test_xray_v1.XrayTests.test_existing_unowned_tag_rejected) ... ok
test_external_change_invalidates_confirmation (test_xray_v1.XrayTests.test_external_change_invalidates_confirmation) ... ok
test_no_confirmation_no_change (test_xray_v1.XrayTests.test_no_confirmation_no_change) ... ok
test_start_failure_rolls_back_once (test_xray_v1.XrayTests.test_start_failure_rolls_back_once) ... ok
test_test_failure_does_not_publish (test_xray_v1.XrayTests.test_test_failure_does_not_publish) ... ok

----------------------------------------------------------------------
Ran 82 tests in 9.901s

OK

```

## final-dual-optimized.log

```text
test_ancestor_symlink_refused (test_auth_v1.AuthTests.test_ancestor_symlink_refused) ... ok
test_api_independent_objects_and_delete (test_auth_v1.AuthTests.test_api_independent_objects_and_delete) ... ok
test_backup_failure_explicit (test_auth_v1.AuthTests.test_backup_failure_explicit) ... ok
test_backup_previous_valid_and_nonce_fresh (test_auth_v1.AuthTests.test_backup_previous_valid_and_nonce_fresh) ... ok
test_epoch_immediate_without_cleanup (test_auth_v1.AuthTests.test_epoch_immediate_without_cleanup) ... ok
test_export_restore_and_wrong_password_preserve (test_auth_v1.AuthTests.test_export_restore_and_wrong_password_preserve) ... ok
test_fixed_phc_and_no_plaintext (test_auth_v1.AuthTests.test_fixed_phc_and_no_plaintext) ... ok
test_initialization_cannot_overwrite (test_auth_v1.AuthTests.test_initialization_cannot_overwrite) ... ok
test_missing_gate_denies (test_auth_v1.AuthTests.test_missing_gate_denies) ... ok
test_oversized_kdf_rejected (test_auth_v1.AuthTests.test_oversized_kdf_rejected) ... ok
test_post_replace_sync_failure_poisoned (test_auth_v1.AuthTests.test_post_replace_sync_failure_poisoned) ... ok
test_pre_replace_failure_old_authority_survives (test_auth_v1.AuthTests.test_pre_replace_failure_old_authority_survives) ... ok
test_rate_limited (test_auth_v1.AuthTests.test_rate_limited) ... ok
test_recovery_rotation (test_auth_v1.AuthTests.test_recovery_rotation) ... ok
test_sessions_logout_current_expiry (test_auth_v1.AuthTests.test_sessions_logout_current_expiry) ... ok
test_symlink_hardlink_mode_and_duplicate_rejected (test_auth_v1.AuthTests.test_symlink_hardlink_mode_and_duplicate_rejected) ... ok
test_new_machine_restore (test_auth_v1.DisasterRestoreTests.test_new_machine_restore) ... ok
test_ancestor_symlink_refused (test_auth_v1.RotationTests.test_ancestor_symlink_refused) ... ok
test_api_independent_objects_and_delete (test_auth_v1.RotationTests.test_api_independent_objects_and_delete) ... ok
test_backup_failure_explicit (test_auth_v1.RotationTests.test_backup_failure_explicit) ... ok
test_backup_previous_valid_and_nonce_fresh (test_auth_v1.RotationTests.test_backup_previous_valid_and_nonce_fresh) ... ok
test_epoch_immediate_without_cleanup (test_auth_v1.RotationTests.test_epoch_immediate_without_cleanup) ... ok
test_export_restore_and_wrong_password_preserve (test_auth_v1.RotationTests.test_export_restore_and_wrong_password_preserve) ... ok
test_fixed_phc_and_no_plaintext (test_auth_v1.RotationTests.test_fixed_phc_and_no_plaintext) ... ok
test_gc_keeps_previous_backup_keys (test_auth_v1.RotationTests.test_gc_keeps_previous_backup_keys) ... ok
test_initialization_cannot_overwrite (test_auth_v1.RotationTests.test_initialization_cannot_overwrite) ... ok
test_missing_gate_denies (test_auth_v1.RotationTests.test_missing_gate_denies) ... ok
test_oversized_kdf_rejected (test_auth_v1.RotationTests.test_oversized_kdf_rejected) ... ok
test_post_replace_sync_failure_poisoned (test_auth_v1.RotationTests.test_post_replace_sync_failure_poisoned) ... ok
test_pre_replace_failure_old_authority_survives (test_auth_v1.RotationTests.test_pre_replace_failure_old_authority_survives) ... ok
test_rate_limited (test_auth_v1.RotationTests.test_rate_limited) ... ok
test_recovery_rotation (test_auth_v1.RotationTests.test_recovery_rotation) ... ok
test_rotation_preserves_api_and_backups (test_auth_v1.RotationTests.test_rotation_preserves_api_and_backups) ... ok
test_sessions_logout_current_expiry (test_auth_v1.RotationTests.test_sessions_logout_current_expiry) ... ok
test_symlink_hardlink_mode_and_duplicate_rejected (test_auth_v1.RotationTests.test_symlink_hardlink_mode_and_duplicate_rejected) ... ok
test_gate_releases_on_failure (test_control_v1.ControlTests.test_gate_releases_on_failure) ... ok
test_heartbeat_off_no_service_calls (test_control_v1.ControlTests.test_heartbeat_off_no_service_calls) ... ok
test_manual_stop_does_not_self_heal (test_control_v1.ControlTests.test_manual_stop_does_not_self_heal) ... ok
test_missing_profile_denies (test_control_v1.ControlTests.test_missing_profile_denies) ... ok
test_pressure_denies (test_control_v1.ControlTests.test_pressure_denies) ... ok
test_two_failures_before_restart (test_control_v1.ControlTests.test_two_failures_before_restart) ... ok
test_unknown_service_denied (test_control_v1.ControlTests.test_unknown_service_denied) ... ok
test_cancel_config_does_not_write_disabled_state (test_panels_v1.InstallerTests.test_cancel_config_does_not_write_disabled_state) ... ok
test_checksum_mismatch_no_install (test_panels_v1.InstallerTests.test_checksum_mismatch_no_install) ... ok
test_cloud_guide_does_not_mutate (test_panels_v1.InstallerTests.test_cloud_guide_does_not_mutate) ... ok
test_default_persisted (test_panels_v1.InstallerTests.test_default_persisted) ... ok
test_download_host_rejected (test_panels_v1.InstallerTests.test_download_host_rejected) ... ok
test_import_no_legacy_auth_files_and_no_listener (test_panels_v1.InstallerTests.test_import_no_legacy_auth_files_and_no_listener) ... ok
test_name_changes_only_metadata (test_panels_v1.InstallerTests.test_name_changes_only_metadata) ... ok
test_no_secret_or_qr_preload (test_panels_v1.InstallerTests.test_no_secret_or_qr_preload) ... ok
test_node_hidden_without_real_config (test_panels_v1.InstallerTests.test_node_hidden_without_real_config) ... ok
test_panel_install_confirmation_required (test_panels_v1.InstallerTests.test_panel_install_confirmation_required) ... ok
test_preview_candidate_bound_and_cancel_no_change (test_panels_v1.InstallerTests.test_preview_candidate_bound_and_cancel_no_change) ... ok
test_qr_on_demand_auth (test_panels_v1.InstallerTests.test_qr_on_demand_auth) ... ok
test_unowned_port_rejected (test_panels_v1.InstallerTests.test_unowned_port_rejected) ... ok
test_unsupported_protocol_no_change (test_panels_v1.InstallerTests.test_unsupported_protocol_no_change) ... ok
test_cloud_guide_does_not_mutate (test_panels_v1.PanelTests.test_cloud_guide_does_not_mutate) ... ok
test_default_persisted (test_panels_v1.PanelTests.test_default_persisted) ... ok
test_import_no_legacy_auth_files_and_no_listener (test_panels_v1.PanelTests.test_import_no_legacy_auth_files_and_no_listener) ... ok
test_name_changes_only_metadata (test_panels_v1.PanelTests.test_name_changes_only_metadata) ... ok
test_no_secret_or_qr_preload (test_panels_v1.PanelTests.test_no_secret_or_qr_preload) ... ok
test_node_hidden_without_real_config (test_panels_v1.PanelTests.test_node_hidden_without_real_config) ... ok
test_preview_candidate_bound_and_cancel_no_change (test_panels_v1.PanelTests.test_preview_candidate_bound_and_cancel_no_change) ... ok
test_qr_on_demand_auth (test_panels_v1.PanelTests.test_qr_on_demand_auth) ... ok
test_unowned_port_rejected (test_panels_v1.PanelTests.test_unowned_port_rejected) ... ok
test_unsupported_protocol_no_change (test_panels_v1.PanelTests.test_unsupported_protocol_no_change) ... ok
test_bootstrap_not_exposed (test_web_v1.DualEntryTests.test_bootstrap_not_exposed) ... ok
test_missing_config_hides_nodes (test_web_v1.DualEntryTests.test_missing_config_hides_nodes) ... ok
test_origin_rejected (test_web_v1.DualEntryTests.test_origin_rejected) ... ok
test_same_entry_no_panel_route (test_web_v1.DualEntryTests.test_same_entry_no_panel_route) ... ok
test_session_and_csrf (test_web_v1.DualEntryTests.test_session_and_csrf) ... ok
test_unauthenticated_direct_status_denied (test_web_v1.DualEntryTests.test_unauthenticated_direct_status_denied) ... ok
test_bootstrap_not_exposed (test_web_v1.WebTests.test_bootstrap_not_exposed) ... ok
test_origin_rejected (test_web_v1.WebTests.test_origin_rejected) ... ok
test_session_and_csrf (test_web_v1.WebTests.test_session_and_csrf) ... ok
test_unauthenticated_direct_status_denied (test_web_v1.WebTests.test_unauthenticated_direct_status_denied) ... ok
test_commit_preserves_unrelated (test_xray_v1.XrayTests.test_commit_preserves_unrelated) ... ok
test_existing_unowned_tag_rejected (test_xray_v1.XrayTests.test_existing_unowned_tag_rejected) ... ok
test_external_change_invalidates_confirmation (test_xray_v1.XrayTests.test_external_change_invalidates_confirmation) ... ok
test_no_confirmation_no_change (test_xray_v1.XrayTests.test_no_confirmation_no_change) ... ok
test_start_failure_rolls_back_once (test_xray_v1.XrayTests.test_start_failure_rolls_back_once) ... ok
test_test_failure_does_not_publish (test_xray_v1.XrayTests.test_test_failure_does_not_publish) ... ok

----------------------------------------------------------------------
Ran 82 tests in 8.972s

OK

```

## final-browser.log

```text
PASS same URL; one listener; authenticated detail mount; QR reveal/hide on demand; default persists; bidirectional switch; no JS exceptions

```
