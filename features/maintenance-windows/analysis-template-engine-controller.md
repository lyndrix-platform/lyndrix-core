# Analyse: Template Engine und iac-controller (P0-Sweep)

Stand: 2026-10-17. Read-only-Sweep (Sonnet) für den Plan `features/maintenance-windows.md`,
Abschnitt 7.5. Keine Geheimnisse zitiert, kein Code geändert. Der Sweep lieferte den Text als
Bericht zurück; diese Datei ist die unveränderte Ablage.

Pfadkürzel: `TE` = `iac-environment/aac-platform/aac-template-engine`;
`CTRL` = `iac-environment/iac-platform/iac-controller`;
`CORE` = `lyndrix-dev/plugins/lyndrix-plugin-iac-orchestrator/iac_core/app`;
`ORCH` = `lyndrix-dev/plugins/lyndrix-plugin-iac-orchestrator`;
`ANS` = `iac-environment/iac-platform/iac-ansible-automation`;
`APPS` = `iac-environment/aac-platform/aac-application-defenitions/applications-repository`.

## 1. Überblick

- **aac-template-engine v0.0.9** (`TE/version.py:3`). Rendert zur Deploy-Zeit pro Service und Host:
  `service.yml` (+ Controller-Override) → `deployments/ansible_context.json` + Compose
  (`TE/scripts/manifest_generator/main.py:101-104`). Enthält auch die Service-CI
  (`TE/templates/cicd/service-pipeline.yml`).
- **iac-controller**: reines Daten-Repo. Die CI macht nur Pages + Webhook-Sync
  (`CTRL/.gitlab-ci.yml:1-8,52-80`). Inventory/TF-State erzeugt ausschließlich der Orchestrator
  (`iac_core`, `NativeGenerateStage`).
- **iac_core (Sync-Zeit)**: lädt den Controller-Baum pro Site/Stage → Jinja (2 Pässe) → Pydantic →
  `inventory.yml`/`group_vars`/tfvars (`CORE/generator.py:115-134`). Liest **kein** `service.yml`.
- **iac-ansible-automation** macht den eigentlichen Merge `service.yml` + Controller
  `services[].config` und ruft die Engine (`ANS/roles/service_rollout/tasks/prepare_and_run_generator.yml:36-57`;
  `ANS/playbooks/cd_playbooks/cd_rollout_single_service.yml:53-61`).
- `ansible_context.json` liegt nur im Runner-Build-Verzeichnis `/tmp/aac_builds/<host>/<svc>`
  (`prepare_and_run_generator.yml:5,91`), nicht im Inventory-State.
- `TE/documentation.md:209-265` (Promote-Ablauf nach iac-controller) ist veraltet; real:
  `prod-trigger-orchestrator` (`service-pipeline.yml:299-386`).

## 2. `service.yml`-Schema und Validierung

- **Kein formales Schema.** Nur der Ad-hoc-Linter `TE/scripts/validate_ssot.py` (195 Zeilen):
  Pflicht `service.{name,image_repo,image_tag,stage}` (59-65), `deployments.docker_compose` (68-70),
  Dangling Volumes, Typo-Checks, Traefik-Domain, Deprecations, Publishability-/Secret-Gate nur bei
  `schema_version >= 2` (110-118), Inline-Literal-Lint (121-139). `schema_version` aus
  `config.schema_version`, Default 1 (41-45). **Unbekannte Top-Level-Keys werden nicht geprüft**
  (weder Fehler noch Warnung).
- **Laufzeit:** `ContextBuilder.build` (`TE/scripts/manifest_generator/context.py:52-88`) validiert
  nicht; Safety-Net (55-73) + Deep-Merge der gesamten SSoT (77-78). Unbekannte Keys werden
  durchgereicht und landen in `ansible_context.json` (`main.py:101-104`). Beleg: `deployment_strategy`
  (`main.py:68`).
- `_render_recursive` rendert jeden `{{ }}`-String (`context.py:21-50`, max. 5 Pässe).
  Fenster-/Cron-Werte sind jinja-frei und damit sicher.
- Reale Top-Level-Keys (aac-lyndrix-core, authentik, grafana): `service`, `ports`, `config`,
  `stage_overrides`, `secrets`, `volumes`, `deployment_strategy`, `deployments`, `dependencies`, `vars`.
  `schema_version: 2` (`APPS/aac-lyndrix-core/service.yml:32`). `stage_overrides` nutzt real nur
  `prod: {config, service}` (`service.yml:33-38`). `deployment_strategy`: `main → prod`, `dev → dev`,
  `test` `enabled: false` (`service.yml:75-93`).
- **Folge:** `maintenance:`/`incidents:` überleben schon heute unverändert bis ans Output. Ein Schema
  wäre ein Zusatz (Linter), **kein `schema_version`-Bump nötig** (die Version steuert nur
  Env-/Secret-Verhalten, `TE/tests/test_processors.py:189,216`).

## 3. Render-Pfad und Merge-Reihenfolge

1. Orchestrator legt `service.yml` ab (`prepare_and_run_generator.yml:3,8-17`), Kopie in isolierten
   Build-Pfad (25-29).
2. Ansible-Merge (36-46): `service.yml` → `combine(ansible_host_ip)` →
   `combine(service_item.config, recursive)` [Controller `hosts.yml services[].config`] →
   `combine(service.stage)` → `combine(inventory_hostname)`.
3. `python -m manifest_generator.main --ssot-json --stage` (`main → prod`) (50-57).
4. `main.py:66-75` Strategy/Kill-Switch (Stage per `deployment_strategy.target_stage`);
   `ContextBuilder`; `stage_overrides[stage]` wird **zuletzt** deep-gemerged (`context.py:80-84`);
   Jinja-Rekursion (86-88).
5. Prozessor-Kette (`main.py:85-95`): Imports (`catalog/*.yml`), Metadata, Ports, Environment,
   Networks, Ingress, Spec, Volume, Ansible (erzeugt `ansible_directories`, `processors/ansible.py:108`).
6. `ansible_context.json` wird immer geschrieben, auch bei `deployment_enabled=false`
   (`main.py:101-110`); danach `engine.render_all/documentation/files/ansible` (`main.py:112-136`;
   `engine.py:15,41,72,110`); Ansible liest `final_ssot_vars` (`prepare_and_run_generator.yml:88-92`).

- **Effektive Reihenfolge:** SSoT → Controller-`config` → `stage_overrides`. `stage_overrides`
  schlägt also den Controller-Override.
- Listen werden **ersetzt** (`_deep_merge`, `context.py:11-19`; Ansible `combine` = Standard, nicht
  verifiziert).
- Controller-Werte müssen literal sein: sie durchlaufen `CORE/services/renderer.py:105-120`
  (2 Pässe, `PermissiveUndefined` → leer; nur `ansible_host_ip`/`ansible_host`/`inventory_hostname`
  bleiben Literal, Z. 63). Fensterdaten sind literal, also unkritisch.

## 4. iac-controller: Struktur von Hosts, VMs und Stages

- `environments/global/{01_global_vars,02_service_catalog,03_profiles,04_agents}.yml`;
  `sites/<site>/{site_vars,networks,hardware}.yml` (`site = onprem | hetzner`);
  `sites/<site>/stages/<stage>/{stage,stage_vars,hosts}.yml` (`stage = dev | test | prod`).
- Ladereihenfolge: `CORE/services/loader.py:28-50`; Listen werden **vereinigt**
  (`unique_list_merge`, `loader.py:6-15`).
- `stage.yml`: nur Metadaten (Top-Key heißt dort `sage_details`, vermutlich Tippfehler, harmlos).
  `stage_vars.yml`: `stage_vars.{ansible_vars, terraform_vars}`.
- `hosts.yml` (Top-Key `hosts`; onprem/prod 10 Hosts): `hostname`, `ansible_host`, `profiles[]`,
  `swarm`, `terraform{is_managed,node_name,vm_id,ip,cores,ram,…}`, `ansible_groups[]`,
  `baseline_roles[]`, `services[]{name,state,deploy_type,config{…}}`. Modelle
  `StandardHost`/`ServiceDefinition` (`CORE/validate/models.py:6-12,44-53`), `extra="allow"`.
- Hardware: `sites/<site>/hardware.yml` → `hardware_hosts` (hydra, cerberus, truenas) + `cluster`
  (olympus, nodes); `HardwareNode`/`HardwareCluster` (`models.py:55-75`), `extra=allow`. Kein
  `services:`-Block (`CTRL/features/001-hypervisor-services-block.md`, OPEN).
- VMs/LXC: `terraform.is_managed` → Container im tfvars (`CORE/gen/terraform/mapper.py:8`);
  `is_used` → Provider (`mapper.py:6`). Ein `maintenance:` neben `terraform:` berührt TF nicht
  (der Mapper liest nur das `terraform`-Dict, `mapper.py:75,123`).
- Profile (`03_profiles.yml`) werden unter die Host-Daten gemerged (`CORE/generator.py:55-88`)
  = weitere Vererbungsstufe.
- `site_vars.timezone: Europe/Berlin` existiert
  (`CTRL/environments/sites/{onprem,hetzner}/site_vars.yml:4`), nutzbar als tz-Default.
- Transport: Git-Sync + Webhook `/api/iac/webhook/sync` (`CTRL/.gitlab-ci.yml:52-80`),
  Self-Healing-Loop als Backstop. Der Controller hat keine Schema-/Unit-Tests.

## 5. Vererbungs-/Precedence-Mechanik heute und Kandidat für die effektive Auflösung

Bestehend (alles Deep-Merge, spezifisch gewinnt): global → site → stage Dateien
(`loader.py:33-49`); Profil → Host (`generator.py:55-88`); global/site/stage_vars → jeder Host,
Hardware ohne stage_vars (`generator.py:148-181`); SSoT → Controller `services[].config`
(`prepare_and_run_generator.yml:36-46`); → `stage_overrides` (`context.py:80-84`). Es gibt **keine**
Service → Host → Stage-Default-Auflösung (die heutige Richtung ist umgekehrt: Controller
überschreibt SSoT).

- **Variante A (iac_core/Orchestrator, Sync-Zeit):** kennt Hosts, Stage, Site, Profile, `services[]`
  (`generator.py:115-181`; `ansible_gen.py:78`); der Weg zu Monitoring/Registry existiert
  (`ORCH/app/controller/engine.py:1161-1196` `_build_inventory_hosts`, `1271-1286`
  `iac:inventory_updated`); Fenster werden **vor** dem Deploy gebraucht. Contra: liest kein
  `service.yml`; der Orchestrator hat die Dateien aber (`engine.py:956-1010` `rglob service.yml` in
  `services_dir`) und müsste `maintenance`/`incidents` (+ `stage_overrides`) mitlesen.
  `_build_inventory_hosts` hat eine feste Feldliste, neue Felder explizit ergänzen.
- **Variante B (Template Engine, Deploy-Zeit):** sieht `service.yml` komplett inkl.
  `stage_overrides`; Ergebnis als `final_ssot_vars`. Contra: nur beim Deploy, kennt Host-/Stage-Defaults
  nicht, Ergebnis erreicht den Orchestrator nicht über das Inventory; Henne-Ei für geplante Deployments.
- **Faktenfazit:** die Auflösung gehört nach **A**; B kann die Service-Ebene validieren/durchreichen.
  Entscheidung offen.

## 6. Inventory-Variablen-Durchreichung (Muster)

- `process_host` schreibt das gesamte Host-Dict als Hostvars (`CORE/gen/ansible_gen.py:59-63`);
  `extra=allow` (`models.py:53,64`). Ein Host-Key `maintenance:` wird ohne Codeänderung Hostvar.
- `stage_vars`/`site_vars`/`global_vars` → `group_vars` (`ansible_gen.py:44-54`; `generator.py:136-146`)
  **und** in jeden Host gemerged (`env_context`, `generator.py:148-181`). Ein Key auf Wurzelebene von
  `stage.yml` landet **nicht** in `group_vars` (`ansible_gen.py:46-48`), daher
  `maintenance_default`/`incident_defaults` **unter `stage_vars:`** ablegen. Hardware-Hosts bekommen
  keine stage_vars, also Hypervisor-Defaults in `site_vars`.
- Muster `ansible_host_ip`: im Orchestrator als Literal-Jinja erhalten (`renderer.py:63,99-102`), in
  Ansible per `combine` gesetzt (`prepare_and_run_generator.yml:40-42`), konsumiert in
  `processors/ports.py:37`, `ingress.py:81`. `maintenance_effective` liefe bei Variante B identisch per
  `combine` (Z. 40-45). Service-Ebene im Inventory: `services[]` je Host + Gruppen `service_<name>`
  (`ansible_gen.py:78-80`).

## 7. Teststand und Doku-Orte

- **TE:** pytest, `PYTHONPATH=$(pwd)/scripts pytest tests/ -v`; CI `python-unit-tests`
  (`TE/.gitlab-ci.yml`, python:3.12, pytest/pyyaml/jinja2) + `yaml-syntax-check`.
  `tests/test_processors.py` (382 Z., 15 Tests; einziger `ContextBuilder`-Test Z. 237, **kein
  `stage_overrides`-Test**), `test_ansible_render.py`, `test_template_render.py`,
  `test_render_files.py`; Fixture `tests/service-test/service.yml` (`stage_overrides` Z. 29).
  `validate_ssot.py` hat keine Tests; Aufruf in CI nicht verifiziert. Migrationsskripte
  `TE/scripts/migration/`.
- **Controller:** keine Schema-/Unit-Tests; `iac_core`-Tests nur `ORCH/iac_core/tests/test_terraform_gen.py`.
- **Doku (DE):** `TE/documentation.md` (465 Z.; Abschnitt 3 `service.yml`, Z. 36-165 → neuer
  Unterabschnitt 3.x; Abschnitt 6 veraltet), Hugo `aac-platform/aac-iac-documentation/content/aac-services/`,
  `CTRL/README.md` (Boilerplate), `CTRL/features/README.md` (Index; neue Datei
  `007-maintenance-windows.md` nach Konvention), MkDocs-Hub `CTRL/mkdocs.yml` +
  `.gitlab-ci/scripts/generate_hub.py`.

## 8. Renovate → Webhook → Deploy (IaC-Seite)

- `renovate.json` in TE, CTRL, `APPS/aac-lyndrix-core` enthalten nur `$schema`. Die echte Konfiguration
  liegt im Renovate-Service `APPS/aac-renovate/service.yml` (`RENOVATE_CONFIG_FILE=/config/config.js`,
  Z. 72-83) + `custom_templates` (nicht ausgewertet).
- **Fluss:** Renovate-MR → MR-Pipeline (`generate`/`validate` bei `merge_request_event` gegen
  `PROD_BRANCH`, `service-pipeline.yml:194-237`) → Merge `main` → `prod-generate` → `prod-validate` →
  `prod-trigger-orchestrator` (`service-pipeline.yml:299-386`):
  `POST $LYNDRIX_ORCHESTRATOR_URL/api/iac/deploy/service/$CI_PROJECT_NAME/gitlab`, Body
  `{"branch": PROD_BRANCH}`, Header `X-Gitlab-Token`; Regel `LYNDRIX_DIRECT_TRIGGER_ENABLED == "true"
  && branch == PROD_BRANCH` (Z. 386; Default `"true"` Z. 40-43). Danach **Wait-Loop** auf
  `…/gitlab/status` (`LYNDRIX_DEPLOY_WAIT_*`, Timeout 1800 s, Z. 44-48). Memory: parallele
  GitLab-Webhooks nur mit `pipeline_events=False`.
- **Wichtig für den Plan:** Ein auf `SCHEDULED` geparkter Deploy würde die Wait-Schleife
  blockieren/timeouten und den GitLab-Job rot färben. Muss mit dem Orchestrator-Sweep abgestimmt werden.

## 9. Einhängepunkte für Abschnitt 7.5

1. `TE/scripts/validate_ssot.py`, `validate_ssot()` nach Block 7b (~Z. 139): optionaler Block
   `maintenance`/`incidents` (`weekly.days`, `HH:MM`, `tz` via `zoneinfo`, `once.start < end`, `cron`
   5 Felder + `duration_minutes > 0`, `deploy_policy in {window, immediate}`, `offline_minutes >= 0`,
   `severity`). Nur bei falschen Typen Fehler, sonst Warnungen; unbekannte Keys unter `maintenance` →
   Warnung.
2. `TE/…/context.py` Safety-Net (Z. 55-73): optional `"maintenance": {}`, `"incidents": {}`.
3. Neu `TE/…/processors/maintenance.py` + Eintrag in `main.py:85-95` (nach `AnsibleProcessor`):
   normalisiert weekly/once/cron, setzt Default `deploy_policy` nach Stage (prod → window, sonst
   immediate), schreibt `context['maintenance_effective']` (Service-Ebene).
4. `TE/tests/test_processors.py`: Normalisierung, `stage_overrides.prod.maintenance` (Liste ersetzt),
   Linter-Fälle.
5. `TE/documentation.md`: neuer Abschnitt 3.x.
6. `CTRL hosts.yml` (je Host) / `hardware.yml` (je Hardware-Host): `maintenance: {windows, inherit}`
   neben `terraform:`/`services:`; läuft ohne Codeänderung bis in die Hostvars.
7. `CTRL stages/<stage>/stage_vars.yml` **unter `stage_vars:`**: `maintenance_default {enabled, windows}`,
   `incident_defaults {service, host, vm, container}`. Hardware: `site_vars.yml`. Globale Typ-Defaults
   sinnvoll in `global/01_global_vars.yml` (`global_vars:`).
8. `CORE/validate/models.py` `StandardHost`/`HardwareNode` (Z. 44-72): optionales Pydantic-Submodell
   `MaintenanceConfig`/`IncidentRule` (einziger formaler Controller-Validierungsort; Absprache mit der
   Orchestrator-Session), sonst reicht `extra=allow`.
9. `ORCH/app/controller/engine.py` `_build_inventory_hosts` (Z. 1161-1196): `maintenance_effective`/
   `incidents_effective` pro Host und Service im `iac:inventory_updated`-Payload; hier die Vererbung
   auflösen (Variante A).
10. `CTRL/features/007-maintenance-windows.md` + `features/README.md` (Index Z. 10-20).
11. `ANS prepare_and_run_generator.yml:40-45` und `cd_rollout_single_service.yml:57-61`: combine-Kette
    nur bei Variante B.

- **Default-Schalter-Rückschreiben:** Zieldatei `stage_vars.yml` → `stage_vars.maintenance_default.enabled`.
  Die Datei enthält einen TF-Backend-Block mit einem Credential-Key (nur der Keyname gesehen):
  Kommentare/Reihenfolge erhalten, **nicht roh umdumpen** (siehe `CTRL/features/002-secrets-sops-hardening.md`).
- **Risiken:** (a) literal-only im Controller (zwei Jinja-Pässe mit Leer-Fallback, `renderer.py:71-120`;
  `{{ }}` in `once.note` würde gerendert/geleert). (b) Listen: Loader vereinigt (`loader.py:6-15`),
  Ansible/`_deep_merge` ersetzen; Defaults pro Key nur auf **einer** Ebene definieren.
  (c) `stage_overrides` schlägt den Controller-Override (nur bei Variante B relevant). (d) Hardware ohne
  stage_vars. (e) kein `schema_version`-Bump nötig; ein Bump auf 3 würde Migrationsskripte berühren,
  nicht empfohlen. (f) Idempotenz: `write_yaml_if_changed` (`ansible_gen.py:12-38`); keine volatilen
  Werte (z. B. „nächstes Fenster") ins Inventory, sonst Commit pro Sync. (g) Inventory-State ist
  Git/SOPS-versioniert; eine reine `maintenance`-Änderung könnte per `pipeline_rules.yml` einen
  Ansible-Lauf auslösen (nicht verifiziert).

## 10. Abweichungen vom Plan und offene Fragen

1. Plan 4.1 „Template Engine validiert": real keine Validierung im Render-Pfad, nur ein optionaler
   Linter; unbekannte Keys werden still durchgereicht. Validierung wäre eine Neuentwicklung.
2. Plan 4.2 „Rendering (Template Engine / iac_core) löst Vererbung auf": die Template Engine kennt
   Host-/Stage-Defaults nicht und läuft erst beim Deploy; `iac_core` kennt kein `service.yml`.
   Entscheidung nötig (die Fakten sprechen für A mit `service.yml`-Lesen im Orchestrator).
3. Plan nennt `stages/<stage>.yml`: real `stage.yml`/`stage_vars.yml`; nur unter `stage_vars:` wird es
   Hostvar; Hardware ohne Stage-Vars.
4. Stage-Begriff doppelt: Controller-Verzeichnis (`stages/dev|test|prod` je Site) vs. Branch/
   `target_stage` im `service.yml` (`main → prod`, `test` in realen Services disabled). Die Auflösung
   muss die Controller-Stage des Hosts nehmen.
5. Service auf mehreren Hosts: gilt das SSoT-Fenster für alle Instanzen oder per
   `services[].config.maintenance` je Host übersteuerbar (Mechanik vorhanden)?
6. Orchestrator-Wait-Schleife vs. `SCHEDULED`-Deploy (Abschnitt 8): Cross-Sweep-Frage.
7. Default-Schalter-Rückschreiben erzeugt Commits auf `iac-controller/main` → Webhook-Sync → neuer
   Lauf; Schleifenrisiko prüfen (Commit-Autor ignorieren oder idempotent).
8. TE-Image hat nur pyyaml, jinja2, mkdocs-material (`TE/Dockerfile`, `requirements.txt`), keine
   Cron-Bibliothek; Linter nur mit stdlib (`zoneinfo`) / eigenem 5-Felder-Parser; die Cron-Auswertung
   bleibt Core-Bibliothek.
9. Renovate-Schedule/Automerge (`APPS/aac-renovate` `config.js`) nicht ausgewertet; klären, ob
   Renovate Merges zeitlich steuert (Doppel-Fenster vermeiden).
10. Nicht verifiziert: Ansible-`combine`-Listenverhalten, Rollout-Auslösung durch
    Inventory-State-Änderung (`pipeline_rules.yml`), ob `validate_ssot.py` in einer CI läuft.
