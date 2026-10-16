# Feature-Plan: KI-Assistent (Claude Code) und globale Panels

**Status:** 🚧 in Umsetzung (begonnen 2026-10-16, nachts)
**Auslöser:** Marvin, 2026-10-16: „KI in die Anwendung bringen, zuerst gezielt Claude Code“ —
Claude-Code-Repo im Dateisystem, Claude Code bevorzugt in eigenem Container mit
Lyndrix-Grundinstruktionen, jedes Plugin kann eigene Instruktionen beisteuern, Chat als eigener
Nav-Tab oder schwebendes Fenster; dafür im Core die Möglichkeit „interaktive, pro Benutzer
angepinnte Popups“ global bereitzustellen. Später weitere LLMs, möglichst über Hermes.
**Betroffene Repos:** `lyndrix-core` (Panels, Manifest-Felder), `lyndrix-ui` (Panel-Host),
neues Plugin `lyndrix-plugin-ai-assistant` (+ Runner-Image `lyndrix-ai-runner`).

## Recherche-Ergebnis (Hermes / Subscription)

Hermes Agent (Nous Research) spricht Claude über OAuth an, aber laut Nous-Doku wird dabei nur
„extra usage“ verbraucht, nicht das Max-Kontingent; Anthropic erlaubt Drittanbietern den
claude.ai-Login ohne Freigabe nicht, GitHub-Issues melden unerwartete Abrechnung. Nous' eigener
Ausweg ist ein experimentelles Plugin, das die Claude-Code-CLI aufruft. **Entscheidung:** Der
Claude-Teil läuft über Claude Code selbst (Agent SDK im Container); Hermes wird später der
Provider für API-Key-basierte Modelle. Auth für Claude Code: `claude setup-token` (Pro/Max,
langlebiges OAuth-Token) als `CLAUDE_CODE_OAUTH_TOKEN`, kein `ANTHROPIC_API_KEY` im Container
(Vorrang → Token-Abrechnung).

## Standardannahmen (falls Marvin nichts anderes sagt)

- Auth: Subscription-Token aus `claude setup-token`, in Vault (Plugin-Setting `secret`).
- Workspace: neues Repo `lyndrix-platform/lyndrix-ai-workspace`, geklont nach
  `STORAGE_DIR/ai_workspace/repo`; der Container sieht nur den Workspace.
- Tool-Freigaben interaktiv im Chat (Allow / Deny / Immer erlauben), Standard-Permission-Mode
  `default`; `bypassPermissions` nur per Setting.

## Architektur

### Core (1.9.0, API 1.11) — Panels + lesbare Manifeste

- `ModuleManifest.panels: List[dict]` — `{id, label, icon, description, default_size:{width,height},
  permission?}`; in `PluginOut.panels`.
- `ModuleManifest.ai_instructions: str | None` — Markdown, das ein Plugin dem KI-Assistenten
  über sich mitgibt (Zweck, API-Pfade, Begriffe, Vorsicht).
- `core.api.module_registry` (lazy, read-only): `get_manifests()` — Plugins, die Manifeste anderer
  Plugins lesen müssen (KI-Instruktionen sammeln), brauchen keinen Griff in Interna mehr.
- Per-User-Panel-Zustand im bestehenden Präferenzdokument (`/api/me/preferences`, Namespace
  `panels`): `{"<plugin>/<panel>": {open, pinned, minimized, x, y, width, height, z}}`.

### Shell (1.7.0) — Panel-Host

- `PanelHost` in der AuthShell: rendert offene Panels als schwebende, verschieb- und
  größenveränderbare Fenster (Titelleiste: Icon, Label, Minimieren, Anpinnen, Schließen),
  z-Ebene unter Dialogen. Inhalt = Plugin-Bundle (`loadPluginModule`) mit
  `<PluginApp panel={{ id, pluginId }} />` — Plugins ohne Panel-Bewusstsein rendern ihre
  Standardansicht, Panel-bewusste Plugins (KI-Chat) schalten um.
- Launcher im Header (Liste aller Panels sichtbarer Plugins, Toggle); Zustand debounced
  gespeichert; unter `md` als Bottom-Sheet.

### Plugin `lyndrix-plugin-ai-assistant` (0.1.0)

- `placement="pinned:runner"`: läuft auf dem Runner-Host (Docker-Socket über den Core-Socket-
  Manager, wie die OpenTofu-Runner des Orchestrators).
- **Runner-Container** `lyndrix-ai-runner` (Image `ghcr.io/lyndrix-platform/lyndrix-ai-runner`,
  langlebig, kein `--rm`): Node 22 + Claude-Code-CLI + Python-Bridge (FastAPI, `claude-agent-sdk`).
  Bridge-API: `GET /health`, `POST /sessions`, `GET /sessions`, `POST /sessions/{id}/messages`,
  `GET /sessions/{id}/events` (SSE: `assistant_text`, `tool_use`, `tool_result`,
  `permission_request`, `result`, `error`, `status`), `POST /sessions/{id}/approvals/{aid}`,
  `DELETE /sessions/{id}`. Shared Secret `X-Bridge-Token`. Workspace unter `/workspace`.
  Freigaben über `can_use_tool` des SDK → Event → Button im Chat → Antwort an die Bridge.
- **Plugin-Backend** (`app/logic`): `RunnerManager` (Container sicherstellen, Netzwerk des
  eigenen Containers ermitteln, Host-Pfad des Workspace über `docker:resolve_mounts`),
  `WorkspaceManager` (Repo klonen/pullen, `CLAUDE.md` generieren: Lyndrix-Grundinstruktionen +
  `.lyndrix/instructions/<plugin>.md` aus `ai_instructions` aller geladenen Plugins),
  `ProviderRegistry` (`claude-code` jetzt, `hermes` später), Sessions pro Benutzer in der DB
  (`ai_sessions`, `ai_messages` als Verlauf).
- **Plugin-API** (`/api/plugins/lyndrix.plugin.ai_assistant/`): `GET /status`, `POST /sessions`,
  `GET /sessions`, `GET /sessions/{id}`, `POST /sessions/{id}/messages`,
  `GET /sessions/{id}/events` (SSE-Proxy), `POST /sessions/{id}/approvals/{aid}`,
  `DELETE /sessions/{id}`, `POST /runner/restart`, `POST /workspace/sync`,
  `GET /instructions` (Vorschau der generierten CLAUDE.md).
- **React-UI**: Route `/ai/chat` (Sidebar) und Panel `chat` (dieselbe Komponente), Route
  `/ai/settings` (Status Runner/Auth/Workspace, Sync, Instruktions-Vorschau);
  Core-Settings-Dialog für Token/Modell/Permission-Mode.

## Reihenfolge

1. Core 1.9.0 + Shell 1.7.0 (Panels, Manifest-Felder, `module_registry`) — Agent.
2. Runner-Image + Bridge (Dockerfile, FastAPI, SDK, GitHub-Workflow) — Agent.
3. Plugin-Backend (Manifest, Settings, Runner-/Workspace-Manager, API, Instruktionen) — Lead.
4. Chat-UI + Settings-UI — Agent, sobald die Plugin-API steht.
5. Workspace-Repo anlegen, Token von Marvin eintragen, erster Chat auf Prod (Runner-Host).

## Offen / Risiken

- Netzwerk Core-Runner ↔ AI-Runner: gleicher Docker-Network-Name wie der Core-Container
  (`docker inspect` der eigenen Container-ID); Fallback Host-Port.
- Token-Rotation: `setup-token` läuft ab (ca. 1 Jahr); Status zeigt Auth-Fehler.
- Mehrere Benutzer teilen einen Workspace (ein Repo); Sessions sind getrennt, Dateiänderungen
  nicht — Freigaben pro Benutzer, Audit über den Verlauf.
