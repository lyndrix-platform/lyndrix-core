# Teilplan P6: IaC-Workspace — Übergabe an die IaC-Session

Grundlage: `analysis-template-engine-controller.md`, `00-ergebnis-p0.md` (Abschnitt 2 Schema,
Abschnitt 4 Punkt 10 CI-Vertrag). Repos: `aac-platform/aac-template-engine` (TE),
`iac-platform/iac-controller` (CTRL), `aac-iac-documentation` (Hugo). Die Lyndrix-Seite
(Orchestrator) liest die Werte; die IaC-Seite validiert, dokumentiert und passt die Service-CI an.
Kein `schema_version`-Bump (die Version steuert nur Env/Secret-Verhalten).

## 1. Template Engine 0.0.10

1. **Linter** `scripts/validate_ssot.py` (`validate_ssot()`, nach Block 7b ~Z. 139): optionaler
   Block `maintenance` / `incidents` (Top-Level und unter `stage_overrides.<stage>`):
   `windows[]` je Eintrag genau einer der Keys `weekly|once|cron`; `weekly.days` nicht leer, Werte
   `mon…sun` oder `0…6`; `start`/`end` `HH:MM`; `once.start < once.end` (ISO-8601 mit Offset);
   `cron.expr` 5 Felder, `duration_minutes` int > 0; `tz` über `zoneinfo.ZoneInfo` prüfbar;
   `deploy_policy ∈ {window, immediate}`; `incidents.offline_minutes` int ≥ 0, `severity ∈
   {critical, high, medium, low}`, `enabled`/`auto_resolve` bool. Falsche Typen → Fehler,
   unbekannte Keys unter `maintenance`/`incidents` → Warnung. Nur stdlib (kein `croniter`).
   Kind-Schlüssel unter `incident_defaults` (Controller) sind offen: Slug `^[a-z][a-z0-9_]*$`,
   keine feste Liste (Entscheidung A).
2. **Safety-Net** `scripts/manifest_generator/context.py:55-73`: `"maintenance": {}`,
   `"incidents": {}` als Defaults, damit Templates nicht auf fehlende Keys laufen.
3. **Kein eigener Prozessor**: die effektive Auflösung passiert im Orchestrator (Variante A).
   Die Keys werden wie heute durchgereicht (`ansible_context.json`), damit Ansible-Rollen sie bei
   Bedarf lesen können (z. B. als Label am Container). Optional ein `MaintenanceProcessor`, der
   nur normalisiert — nicht nötig für den ersten Wurf.
4. **Service-CI** `templates/cicd/service-pipeline.yml` (Job `prod-trigger-orchestrator`,
   299-386): Status-Poll mit `?accept=scheduled`; `DEPLOY_STATUS == "scheduled"` → Ausgabe
   `Lyndrix deployment scheduled for ${NOT_BEFORE} (job_id=…) — maintenance window policy` und
   `exit 0` (Job grün, Deployment läuft später). Neue Variable
   `LYNDRIX_DEPLOY_ACCEPT_SCHEDULED: "true"` (Default an). Hinweis in den Kommentaren 40-48.
   Bis dieses Template ausgerollt ist, liefert der Orchestrator ohne `accept` weiter `success`
   mit `job_status: SCHEDULED` (Kompatibilitätsmodus).
5. **Tests** `tests/test_processors.py` (+ ggf. `tests/test_validate_ssot.py` neu): Linter-Fälle
   gültig/ungültig; `stage_overrides.prod.maintenance` ersetzt die Liste
   (`_deep_merge`-Verhalten dokumentieren); Fixture `tests/service-test/service.yml` um einen
   `maintenance`-Block erweitern.
6. **Doku** `documentation.md` Abschnitt 3 (36-165): Unterabschnitt „3.x Wartungsfenster und
   Incident-Regeln" mit dem Schema aus `00-ergebnis-p0.md` 2.2, Vererbungskette und
   `deploy_policy`-Default; Abschnitt 6 (209-265, veraltet) bei der Gelegenheit auf
   `prod-trigger-orchestrator` korrigieren. Hugo `content/aac-services/` nachziehen.
7. `version.py` → 0.0.10, Release wie gewohnt (Base-Image-Kette beachten, Memory
   `iac-base-image-chain`).

## 2. iac-controller

1. **Schema-Doku** `features/007-maintenance-windows.md` (Konvention `features/README.md`
   Index 10-20): Blöcke je Host (`hosts.yml` `maintenance: {windows, inherit}`, optional
   `incidents`), je Hardware-Host (`hardware.yml` `maintenance`), je Service-Instanz
   (`services[].maintenance`), Stage (`stage_vars.maintenance_default`,
   `stage_vars.incident_defaults`), Site (`site_vars.maintenance_default`,
   `site_vars.incident_defaults`, `site_vars.timezone` als tz-Default). Regeln: Werte literal
   (kein Jinja, `{{ }}` würde geleert), Listen werden ersetzt (Fenster je Ebene nur einmal
   definieren), Typ-Schlüssel = Entity-Registry-Kinds (offen; heute `hypervisor`, `host`,
   `service`, `container`, `network` …). Hinweis: ohne Fenster im Horizont deployt der
   Orchestrator sofort und das ITSM legt einen Task „Wartungsfenster definieren" an.
2. **Beispiel-Einträge** in `environments/sites/onprem/stages/prod/stage_vars.yml` (unter
   `stage_vars:`): `maintenance_default: {enabled: false, windows: [{weekly: {days: [sun],
   start: "02:00", end: "04:00"}}]}` und `incident_defaults` mit den Defaults aus dem Ergebnis
   2.3. `enabled: false`, bis Marvin den Schalter umlegt. **Datei zeilenweise editieren**, der
   TF-Backend-Block mit Credential-Key bleibt unangetastet (siehe `features/002-secrets-sops-hardening.md`).
3. **Default-Schalter** (Entscheidung C, 2026-10-18): kein Push-Recht für den Orchestrator.
   Ein/Aus wird im Controller-Repo editiert (Zeile `enabled:` im Block `maintenance_default`),
   die CI (`.gitlab-ci.yml:52-80`) löst beim Commit den Webhook-Sync aus, der Orchestrator
   publiziert neu. Das Rückschreiben aus der Lyndrix-UI kommt später über das
   SSoT-Manager-Plugin (`00-ergebnis-p0.md` Abschnitt 8); dessen Commits beginnen mit
   `ci: maintenance` und lösen keinen Deploy aus. Für diesen Bot-Account ist dann „Allowed to
   push" auf `main` einzurichten.
4. **Hub/MkDocs**: `mkdocs.yml` nimmt die neue Feature-Datei auf (`generate_hub.py` prüfen).

## 3. Renovate

`aac-renovate` `config.js` (nicht ausgewertet): prüfen, ob dort ein `schedule` gesetzt ist. Mit
Fenster-Policy im Orchestrator sollte Renovate **ohne** Zeitfenster mergen dürfen (das Fenster
wacht im Orchestrator), damit nicht zwei Fenster hintereinander warten. Entscheidung IaC-Session.

## 4. Abnahme

- Linter rot bei `weekly.days: []`, grün für das Beispiel aus 2.2; CI `python-unit-tests` grün.
- Ein Service mit `deploy_policy: window` auf `prod`: der CI-Job endet grün mit „scheduled",
  der Orchestrator zeigt den Job unter „Geplante Deployments".
- Controller-Beispielwerte erscheinen nach Sync in `GET /api/plugins/lyndrix.plugin.iac_orchestrator/maintenance-windows`.
