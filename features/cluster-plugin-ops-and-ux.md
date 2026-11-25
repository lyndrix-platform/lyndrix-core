# Feature plan: cluster-wide plugin operations, plugin UX and rights management

**Status:** 🚧 in implementation (started 2026-10-16) — all five work packages implemented (Core 1.8.0, Shell 1.6.1, External Services 1.4.0); rollout + live check of the first operation pending
**Trigger:** Marvin, 2026-10-16 — after the rollout of Core 1.7.x in the k3s cluster:
notification storm on plugin installations across multiple nodes, missing "Update all",
no uniform entry point to plugin settings, External Services without a configuration page,
monitoring 500, confusing rights/group management.
**Affected repos:** `lyndrix-core`, `lyndrix-ui`, `lyndrix-plugin-external-services`

## Findings

- **Monitoring 500 (fixed in Core 1.7.2).** `PluginRouterRegistry.register()` only *appended* the new router
  on a reload; the old routes remained in front and answered with handlers of the unloaded module
  (ORM classes discarded → `ArgumentError`). Now `register()` replaces the old routes, and `unload_module()` removes them too.
- **Notifications are per instance.** `NotificationService` writes `notifications.json`
  in `STORAGE_DIR`; every instance has its own bell. The plugin lifecycle events
  (`plugin:installed`, `plugin:install_failed`, `plugin:rolled_back`, `plugin:update_available`)
  create a notification on *every* instance — with six instances six messages per
  rollout, and the update checker also runs on every instance.
- **Themes lived in the image** (`app/assets/themes`), therefore per pod and until the next image update.
  Since 1.7.2 uploads are stored under `STORAGE_DIR/themes` (RWX PVC, all pods).
- **External Services** only has a NiceGUI interface; in the cluster (UI engine `api`) there is
  no configuration page. The services themselves have appeared in the sidebar since Core 1.7.0 / Shell 1.5.0 via
  `ctx.register_nav_entries`.
- **Rights management:** roles (registry, from plugin manifests) are attached exclusively to groups
  (`Group.roles`); `User.roles` are system flags (`superadmin`, `bot`). The detail panel
  (`max-w-md`) shows groups as a chip list and individual rights as flat buttons without any indication
  of which rights are already contained in others (`api:read` ⇒ every `plugin:*:api:read`,
  `plugin:<id>` ⇒ visibility + all routes).

## Work packages

### WS1 — cluster-wide plugin operations (Core 1.8.0)

An install/an upgrade/a rollback is *one* operation that the cluster tracks — not
N local events.

- Tables `plugin_operations` (id, plugin_id, action, version, requested_by, requested_at,
  status `running|done|failed|partial`, expected_instances, summary) and
  `plugin_operation_instances` (operation_id, instance_id, status `pending|installing|verifying|
  done|failed|reverted|skipped`, message, updated_at).
- The **leading instance** (which executes the operator command; `lead=True`, see 1.7.0) creates the
  operation and enters the instances alive at that time as `expected`. Every instance reports its outcome
  to its row after `_handle_plugin_install` (including the followers of the reconcile). Whoever closes the last
  open row closes the operation and generates **exactly one** notification (`Plugin updated: state_monitoring 1.6.1 on 6/6 instances` or with the list
  of failed instances). Timeout (10 min, leader checks periodically) →
  `partial` with the open instances; a node that catches up later fills in its row.
- Per-instance notifications of the lifecycle hooks are dropped; `plugin:rolled_back`/
  `install_failed` become instance rows. The update checker now runs only on the leader
  (one `plugin_update_available` message per plugin).
- Live progress: event `plugin:operation_changed` (SSE), `GET /api/plugins/operations?active=1`
  and `GET /api/plugins/operations/{id}`; the plugins page shows running operations as a bar
  ("Upgrade state_monitoring → 1.6.1 · 3/6 instances") with expansion per instance.
- **Notification store in the database** (tables `notifications`,
  `notification_user_state`), so the bell shows the same thing on every instance. The file store
  remains a fallback without DB (boot) and is imported once.

### WS2 — "Update all" (Core + shell)

`POST /api/plugins/upgrade-all` starts one operation for each plugin with `update_available`
(one after another, leading instance) and returns the list. The plugins page gets the button in the
header with a confirmation dialog (plugins + target versions); progress via WS1.

### WS3 — entry point to plugin settings (shell)

Decision: **gear at the top right of the header**, as soon as a plugin route (`/apps/<id>/…`)
is open and the plugin has `settings_schema` or `settings_ui_route`. The gear opens the
core settings dialog or navigates to the plugin's settings route. No additional entry
in the profile menu (plugins with their own button such as Server Manager/Orchestrator remain unchanged;
the header path is the standard for all others).

### WS4 — External Services: React configuration, optional grid (plugin 1.4.0, Core API 1.10, shell)

- React bundle following the template (`plugins/plugin-ui-template`): route `/external/settings`
  (table of all services, create/edit/delete: name, URL, icon, open as iframe/new
  tab, in sidebar, active; "Reload") and route `/external/overview` (tile grid of all
  services). Both `sidebar_visible: false`; `settings_ui_route="/external/settings"`.
- Core API 1.10: nav entries get `open_mode: "route"` (the target is a path *in the shell*,
  e.g. `/apps/lyndrix-plugin-external_services/external/overview`). The plugin thereby registers —
  controlled by the setting `show_overview_in_nav` (default off) — the entry "All services" under *Services*.
- Shell: sidebar lists only plugins with at least one `sidebar_visible` route (today it
  falls back to `react_routes[0]`); `route` entries are rendered as internal links.
- NiceGUI pages remain for the NiceGUI engine.

### WS5 — Users page: roles first, understandable rights (shell + core)

- Core: `GET /api/permissions/catalog` additionally returns `implies` (computed server-side:
  `api:read`/`api:write` ⇒ all `plugin:*:api:read|write`, `plugin:<id>` ⇒ `plugin:<id>:*`) — one
  source for shell and backend (`has_permission`). Registry roles may additionally hang **directly on the
  user** (`User.roles` accepts registry role IDs; `get_effective_permissions` expands
  them); the system flags remain separate.
- Shell, detail panel wider (`max-w-2xl`) with tabs **Profile · Groups · Rights · API keys**.
  Tab *Rights*: at the top **roles** as cards (description, expandable "contains …", origin:
  direct / via group X), below that **additional individual rights** grouped by category with
  search; a right that is already contained through role/group/implication is marked as
  "contained through …" and cannot be selected separately.
- Groups tab following the same pattern (roles at the top, individual rights below, members on the right).

## Sequence and releases

1. Core 1.7.2 (monitoring fix, themes) — rolled out, marvin theme uploaded.
2. WS1 + WS2 + WS5 core + nav `route` → Core 1.8.0 (API 1.10).
3. WS3 + WS2 UI + WS4 shell + WS5 shell → Shell 1.6.0.
4. WS4 plugin → External Services 1.4.0.
5. Rollout k3s (core/shell), plugin upgrade via the API, check of the bell on multiple pods.

## Verification

- Plugin upgrade in the cluster creates exactly one notification (visible on every pod), the
  operation shows 6/6 instances; `plugin_states` changes exactly once.
- "Update all" pulls all plugins with an update; progress visible.
- Gear appears on plugin pages with settings, opens dialog/route.
- External Services: create a service via the settings route → entry appears in the sidebar
  (all pods, thanks to `external_services:changed`); grid optional via setting.
- Users: assign a role directly → effective rights visible; implied rights marked;
  panel readable at 1280 px width.
