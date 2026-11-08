# V1 Box development checks

Input implementation commit: `4b40e950cea40d2b40527da998c5c0c565d9918d`.

Simular Box development host, not target VPS. No real service operations or installation. Tests use temporary data and fake service callbacks.

Commands:

- `python -m py_compile auth.py function.py active.py main.py`
- `python -m unittest discover -s tests -p test_*v1.py -v`
- `python -O -m unittest discover -s tests -p test_*v1.py -v`
- `node --check` on extracted inline UI JavaScript
- `git diff --check`
- Frozen file blob equality against `6d3d01b135f1bc5b3c04c149cd18e8dcdecc0002`.

Both unittest runs exit 0. 52 test executions per run include 16 inherited repeated auth tests in RotationTests; not 52 distinct security scenarios. No memory-peak, power-failure, real proxy or pixel-level UI claim.

## final-normal.log

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
Ran 52 tests in 4.813s

OK

```

## final-optimized.log

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
Ran 52 tests in 4.590s

OK

```
