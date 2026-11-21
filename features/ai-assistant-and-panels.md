# Feature plan: AI assistant (Claude Code) and global panels

**Status:** 🚧 in implementation (started 2026-10-16, at night)
**Trigger:** Marvin, 2026-10-16: "Bring AI into the application, starting specifically with Claude Code" —
Claude Code repo in the file system, Claude Code preferably in its own container with
Lyndrix base instructions, every plugin can contribute its own instructions, chat as its own
nav tab or floating window; for this, the core should provide "interactive, per-user pinned popups"
globally. Later more LLMs, preferably via Hermes.
**Affected repos:** `lyndrix-core` (panels, manifest fields), `lyndrix-ui` (panel host),
new plugin `lyndrix-plugin-ai-assistant` (+ runner image `lyndrix-ai-runner`).

## Research result (Hermes / subscription)

Hermes Agent (Nous Research) connects to Claude via OAuth, but according to the Nous docs only
"extra usage" is consumed in this way, not the Max quota; Anthropic does not allow third parties to
use claude.ai login without approval, and GitHub issues report unexpected billing. Nous' own
workaround is an experimental plugin that calls the Claude Code CLI. **Decision:** The
Claude part runs via Claude Code itself (Agent SDK in the container); Hermes will later become the
provider for API-key-based models. Auth for Claude Code: `claude setup-token` (Pro/Max,
long-lived OAuth token) as `CLAUDE_CODE_OAUTH_TOKEN`, no `ANTHROPIC_API_KEY` in the container
(precedence → token billing).

## Default assumptions (if Marvin says nothing else)

- Auth: subscription token from `claude setup-token`, in Vault (plugin setting `secret`).
- Workspace: new repo `lyndrix-platform/lyndrix-ai-workspace`, cloned to
  `STORAGE_DIR/ai_workspace/repo`; the container sees only the workspace.
- Tool approvals interactively in the chat (Allow / Deny / Always allow), default permission mode
  `default`; `bypassPermissions` only via setting.

## Architecture

### Core (1.9.0, API 1.11) — panels + readable manifests

- `ModuleManifest.panels: List[dict]` — `{id, label, icon, description, default_size:{width,height},
  permission?}`; in `PluginOut.panels`.
- `ModuleManifest.ai_instructions: str | None` — Markdown that a plugin provides to the AI assistant
  about itself (purpose, API paths, terms, caution).
- `core.api.module_registry` (lazy, read-only): `get_manifests()` — plugins that need to read manifests
  of other plugins (collecting AI instructions) no longer need to reach into internals.
- Per-user panel state in the existing preferences document (`/api/me/preferences`, namespace
  `panels`): `{"<plugin>/<panel>": {open, pinned, minimized, x, y, width, height, z}}`.

### Shell (1.7.0) — panel host

- `PanelHost` in the AuthShell: renders open panels as floating windows that can be moved and resized
  (title bar: icon, label, minimize, pin, close), z-layer below dialogs. Content = plugin bundle (`loadPluginModule`) with
  `<PluginApp panel={{ id, pluginId }} />` — plugins without panel awareness render their
  default view, panel-aware plugins (AI chat) switch over.
- Launcher in the header (list of all panels of visible plugins, toggle); state saved debounced;
  below `md` as bottom sheet.

### Plugin `lyndrix-plugin-ai-assistant` (0.1.0)

- `placement="pinned:runner"`: runs on the runner host (Docker socket via the core socket
  manager, like the OpenTofu runners of the orchestrator).
- **Runner container** `lyndrix-ai-runner` (image `ghcr.io/lyndrix-platform/lyndrix-ai-runner`,
  long-lived, no `--rm`): Node 22 + Claude Code CLI + Python bridge (FastAPI, `claude-agent-sdk`).
  Bridge API: `GET /health`, `POST /sessions`, `GET /sessions`, `POST /sessions/{id}/messages`,
  `GET /sessions/{id}/events` (SSE: `assistant_text`, `tool_use`, `tool_result`,
  `permission_request`, `result`, `error`, `status`), `POST /sessions/{id}/approvals/{aid}`,
  `DELETE /sessions/{id}`. Shared secret `X-Bridge-Token`. Workspace under `/workspace`.
  Approvals via `can_use_tool` of the SDK → event → button in the chat → response to the bridge.
- **Plugin backend** (`app/logic`): `RunnerManager` (ensure container, determine network of its own
  container, host path of the workspace via `docker:resolve_mounts`),
  `WorkspaceManager` (clone/pull repo, generate `CLAUDE.md`: Lyndrix base instructions +
  `.lyndrix/instructions/<plugin>.md` from `ai_instructions` of all loaded plugins),
  `ProviderRegistry` (`claude-code` now, `hermes` later), sessions per user in the DB
  (`ai_sessions`, `ai_messages` as history).
- **Plugin API** (`/api/plugins/lyndrix.plugin.ai_assistant/`): `GET /status`, `POST /sessions`,
  `GET /sessions`, `GET /sessions/{id}`, `POST /sessions/{id}/messages`,
  `GET /sessions/{id}/events` (SSE proxy), `POST /sessions/{id}/approvals/{aid}`,
  `DELETE /sessions/{id}`, `POST /runner/restart`, `POST /workspace/sync`,
  `GET /instructions` (preview of the generated CLAUDE.md).
- **React UI**: route `/ai/chat` (sidebar) and panel `chat` (same component), route
  `/ai/settings` (status runner/auth/workspace, sync, instruction preview);
  core settings dialog for token/model/permission mode.

## Sequence

1. Core 1.9.0 + shell 1.7.0 (panels, manifest fields, `module_registry`) — agent.
2. Runner image + bridge (Dockerfile, FastAPI, SDK, GitHub workflow) — agent.
3. Plugin backend (manifest, settings, runner/workspace manager, API, instructions) — lead.
4. Chat UI + settings UI — agent, as soon as the plugin API is in place.
5. Create workspace repo, Marvin enters token, first chat on prod (runner host).

## Open items / risks

- Network core runner ↔ AI runner: same Docker network name as the core container
  (`docker inspect` of the own container ID); fallback host port.
- Token rotation: `setup-token` expires (approx. 1 year); status shows auth error.
- Multiple users share one workspace (one repo); sessions are separate, file changes are not —
  approvals per user, audit via the history.
