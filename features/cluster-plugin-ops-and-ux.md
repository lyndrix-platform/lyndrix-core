# Feature-Plan: Cluster-weite Plugin-Operationen, Plugin-UX und Rechteverwaltung

**Status:** 🚧 in Umsetzung (begonnen 2026-10-16)
**Auslöser:** Marvin, 2026-10-16 — nach dem Rollout von Core 1.7.x im k3s-Cluster:
Benachrichtigungs-Sturm bei Plugin-Installationen über mehrere Knoten, fehlendes „Alle
aktualisieren“, kein einheitlicher Einstieg in die Plugin-Einstellungen, External Services ohne
Konfigurationsseite, Monitoring-500, unübersichtliche Rechte-/Gruppenverwaltung.
**Betroffene Repos:** `lyndrix-core`, `lyndrix-ui`, `lyndrix-plugin-external-services`

## Befunde

- **Monitoring-500 (behoben in Core 1.7.2).** `PluginRouterRegistry.register()` hat bei einem
  Reload den neuen Router nur *angehängt*; die alten Routen blieben vorne und antworteten mit
  Handlern des entladenen Moduls (ORM-Klassen verworfen → `ArgumentError`). Jetzt ersetzt
  `register()` die alten Routen, `unload_module()` hängt sie ebenfalls aus.
- **Benachrichtigungen sind pro Instanz.** `NotificationService` schreibt `notifications.json`
  in `STORAGE_DIR`; jede Instanz hat ihre eigene Glocke. Die Plugin-Lebenszyklus-Events
  (`plugin:installed`, `plugin:install_failed`, `plugin:rolled_back`, `plugin:update_available`)
  erzeugen auf *jeder* Instanz eine Notification — bei sechs Instanzen sechs Meldungen pro
  Rollout, und der Update-Checker läuft ebenfalls auf jeder Instanz.
- **Themes lagen im Image** (`app/assets/themes`), also pro Pod und bis zum nächsten Image-Update.
  Seit 1.7.2 liegen Uploads unter `STORAGE_DIR/themes` (RWX-PVC, alle Pods).
- **External Services** hat nur eine NiceGUI-Oberfläche; im Cluster (UI-Engine `api`) gibt es
  keine Konfigurationsseite. Die Dienste selbst erscheinen seit Core 1.7.0 / Shell 1.5.0 über
  `ctx.register_nav_entries` in der Sidebar.
- **Rechteverwaltung:** Rollen (Registry, aus Plugin-Manifesten) hängen ausschließlich an Gruppen
  (`Group.roles`); `User.roles` sind System-Flags (`superadmin`, `bot`). Das Detail-Panel
  (`max-w-md`) zeigt Gruppen als Chip-Liste und Einzelrechte als flache Buttons ohne Hinweis,
  welche Rechte durch andere bereits enthalten sind (`api:read` ⇒ jedes `plugin:*:api:read`,
  `plugin:<id>` ⇒ Sichtbarkeit + alle Routen).

## Arbeitspakete

### WS1 — Cluster-weite Plugin-Operationen (Core 1.8.0)

Eine Installation/ein Upgrade/ein Rollback ist *eine* Operation, die der Cluster verfolgt — nicht
N lokale Ereignisse.

- Tabellen `plugin_operations` (id, plugin_id, action, version, requested_by, requested_at,
  status `running|done|failed|partial`, expected_instances, summary) und
  `plugin_operation_instances` (operation_id, instance_id, status `pending|installing|verifying|
  done|failed|reverted|skipped`, message, updated_at).
- Die **führende Instanz** (die den Operator-Befehl ausführt; `lead=True`, siehe 1.7.0) legt die
  Operation an und trägt die zu dem Zeitpunkt lebenden Instanzen als `expected` ein. Jede Instanz
  meldet ihren Ausgang nach `_handle_plugin_install` (auch die Mitläufer des Reconcile) in ihre
  Zeile. Wer die letzte offene Zeile schließt, schließt die Operation und erzeugt **genau eine**
  Notification (`Plugin aktualisiert: state_monitoring 1.6.1 auf 6/6 Instanzen` bzw. mit Liste
  der fehlgeschlagenen Instanzen). Zeitüberschreitung (10 min, Leader prüft periodisch) →
  `partial` mit den offenen Instanzen; ein später nachziehender Knoten ergänzt seine Zeile.
- Per-Instanz-Notifications der Lebenszyklus-Hooks entfallen; `plugin:rolled_back`/
  `install_failed` werden zu Instanz-Zeilen. Der Update-Checker läuft nur noch auf dem Leader
  (eine `plugin_update_available`-Meldung pro Plugin).
- Fortschritt live: Event `plugin:operation_changed` (SSE), `GET /api/plugins/operations?active=1`
  und `GET /api/plugins/operations/{id}`; die Plugins-Seite zeigt laufende Operationen als Leiste
  („Upgrade state_monitoring → 1.6.1 · 3/6 Instanzen“) mit Aufklappen pro Instanz.
- **Notification-Store auf die Datenbank** (Tabellen `notifications`,
  `notification_user_state`), damit die Glocke auf jeder Instanz dasselbe zeigt. Datei-Store
  bleibt Fallback ohne DB (Boot) und wird einmalig importiert.

### WS2 — „Alle aktualisieren“ (Core + Shell)

`POST /api/plugins/upgrade-all` startet für jedes Plugin mit `update_available` eine Operation
(nacheinander, führende Instanz) und liefert die Liste. Die Plugins-Seite bekommt den Button in
der Kopfzeile mit Bestätigungsdialog (Plugins + Zielversionen); Fortschritt über WS1.

### WS3 — Einstieg in die Plugin-Einstellungen (Shell)

Entscheidung: **Zahnrad oben rechts im Header**, sobald eine Plugin-Route (`/apps/<id>/…`)
geöffnet ist und das Plugin `settings_schema` oder `settings_ui_route` hat. Zahnrad öffnet den
Core-Einstellungsdialog bzw. navigiert zur Settings-Route des Plugins. Kein zusätzlicher Eintrag
im Profilmenü (Plugins mit eigenem Button wie Server Manager/Orchestrator bleiben unverändert;
der Header-Weg ist der Standard für alle anderen).

### WS4 — External Services: React-Konfiguration, optionales Grid (Plugin 1.4.0, Core API 1.10, Shell)

- React-Bundle nach dem Template (`plugins/plugin-ui-template`): Route `/external/settings`
  (Tabelle aller Dienste, Anlegen/Bearbeiten/Löschen: Name, URL, Icon, Öffnen als iframe/neuer
  Tab, in Sidebar, aktiv; „Neu laden“) und Route `/external/overview` (Kachel-Grid aller
  Dienste). Beide `sidebar_visible: false`; `settings_ui_route="/external/settings"`.
- Core API 1.10: Nav-Einträge bekommen `open_mode: "route"` (Ziel ist ein Pfad *in der Shell*,
  z. B. `/apps/lyndrix-plugin-external_services/external/overview`). Das Plugin registriert damit
  — gesteuert durch die Einstellung `show_overview_in_nav` (Standard aus) — den Eintrag
  „Alle Dienste“ unter *Dienste*.
- Shell: Sidebar listet nur Plugins mit mindestens einer `sidebar_visible`-Route (heute fällt sie
  auf `react_routes[0]` zurück); `route`-Einträge werden als interne Links gerendert.
- NiceGUI-Seiten bleiben für die NiceGUI-Engine erhalten.

### WS5 — Users-Seite: Rollen zuerst, verständliche Rechte (Shell + Core)

- Core: `GET /api/permissions/catalog` liefert zusätzlich `implies` (serverseitig berechnet:
  `api:read`/`api:write` ⇒ alle `plugin:*:api:read|write`, `plugin:<id>` ⇒ `plugin:<id>:*`) — eine
  Quelle für Shell und Backend (`has_permission`). Registry-Rollen dürfen zusätzlich **direkt am
  User** hängen (`User.roles` akzeptiert Registry-Rollen-IDs; `get_effective_permissions` expandiert
  sie); die System-Flags bleiben separat.
- Shell, Detail-Panel breiter (`max-w-2xl`) mit Tabs **Profil · Gruppen · Rechte · API-Keys**.
  Tab *Rechte*: oben **Rollen** als Karten (Beschreibung, aufklappbar „enthält …“, Herkunft:
  direkt / über Gruppe X), darunter **zusätzliche Einzelrechte** gruppiert nach Kategorie mit
  Suche; ein Recht, das durch Rolle/Gruppe/Implikation bereits enthalten ist, wird als
  „enthalten durch …“ markiert und ist nicht separat wählbar.
- Gruppen-Tab nach demselben Muster (Rollen oben, Einzelrechte unten, Mitglieder rechts).

## Reihenfolge und Releases

1. Core 1.7.2 (Monitoring-Fix, Themes) — ausgerollt, marvin-Theme hochgeladen.
2. WS1 + WS2 + WS5-Core + Nav `route` → Core 1.8.0 (API 1.10).
3. WS3 + WS2-UI + WS4-Shell + WS5-Shell → Shell 1.6.0.
4. WS4 Plugin → External Services 1.4.0.
5. Rollout k3s (Core/Shell), Plugin-Upgrade über die API, Prüfung der Glocke auf mehreren Pods.

## Verifikation

- Plugin-Upgrade im Cluster erzeugt genau eine Notification (auf jedem Pod sichtbar), die
  Operation zeigt 6/6 Instanzen; `plugin_states` wechselt genau einmal.
- „Alle aktualisieren“ zieht alle Plugins mit Update nach; Fortschritt sichtbar.
- Zahnrad erscheint auf Plugin-Seiten mit Einstellungen, öffnet Dialog/Route.
- External Services: Dienst anlegen über die Settings-Route → Eintrag erscheint in der Sidebar
  (alle Pods, dank `external_services:changed`); Grid optional per Einstellung.
- Users: Rolle direkt zuweisen → effektive Rechte sichtbar; implizierte Rechte markiert;
  Panel lesbar auf 1280 px Breite.
