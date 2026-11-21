# Analysis: template engine and iac-controller (P0 sweep)

State: 2026-10-17. Read-only sweep (Sonnet) for the plan `features/maintenance-windows.md`,
section 7.5. No secrets quoted, no code changed. The sweep returned the text as a
report; this file is the unchanged archive copy.

Path abbreviations: `TE` = `iac-environment/aac-platform/aac-template-engine`;
`CTRL` = `iac-environment/iac-platform/iac-controller`;
`CORE` = `lyndrix-dev/plugins/lyndrix-plugin-iac-orchestrator/iac_core/app`;
`ORCH` = `lyndrix-dev/plugins/lyndrix-plugin-iac-orchestrator`;
`ANS` = `iac-environment/iac-platform/iac-ansible-automation`;
`APPS` = `iac-environment/aac-platform/aac-application-defenitions/applications-repository`.

## 1. Overview

- **aac-template-engine v0.0.9** (`TE/version.py:3`). Renders at deploy time per service and host:
  `service.yml` (+ controller override) → `deployments/ansible_context.json` + compose
  (`TE/scripts/manifest_generator/main.py:101-104`). Also contains the service CI
  (`TE/templates/cicd/service-pipeline.yml`).
- **iac-controller**: pure data repo. The CI only does Pages + webhook sync
  (`CTRL/.gitlab-ci.yml:1-8,52-80`). Inventory/TF state is produced exclusively by the orchestrator
  (`iac_core`, `NativeGenerateStage`).
- **iac_core (sync time)**: loads the controller tree per site/stage → Jinja (2 passes) → Pydantic →
  `inventory.yml`/`group_vars`/tfvars (`CORE/generator.py:115-134`). Reads **no** `service.yml`.
- **iac-ansible-automation** does the actual merge of `service.yml` + controller
  `services[].config` and calls the engine (`ANS/roles/service_rollout/tasks/prepare_and_run_generator.yml:36-57`;
  `ANS/playbooks/cd_playbooks/cd_rollout_single_service.yml:53-61`).
- `ansible_context.json` exists only in the runner build directory `/tmp/aac_builds/<host>/<svc>`
  (`prepare_and_run_generator.yml:5,91`), not in the inventory state.
- `TE/documentation.md:209-265` (promote flow to iac-controller) is outdated; in reality:
  `prod-trigger-orchestrator` (`service-pipeline.yml:299-386`).

## 2. `service.yml` schema and validation

- **No formal schema.** Only the ad-hoc linter `TE/scripts/validate_ssot.py` (195 lines):
  required `service.{name,image_repo,image_tag,stage}` (59-65), `deployments.docker_compose` (68-70),
  dangling volumes, typo checks, Traefik domain, deprecations, publishability/secret gate only at
  `schema_version >= 2` (110-118), inline-literal lint (121-139). `schema_version` from
  `config.schema_version`, default 1 (41-45). **Unknown top-level keys are not checked**
  (neither error nor warning).
- **Runtime:** `ContextBuilder.build` (`TE/scripts/manifest_generator/context.py:52-88`) does not
  validate; safety net (55-73) + deep merge of the whole SSoT (77-78). Unknown keys are passed
  through and end up in `ansible_context.json` (`main.py:101-104`). Evidence: `deployment_strategy`
  (`main.py:68`).
- `_render_recursive` renders every `{{ }}` string (`context.py:21-50`, max. 5 passes).
  Window/cron values are Jinja-free and therefore safe.
- Real top-level keys (aac-lyndrix-core, authentik, grafana): `service`, `ports`, `config`,
  `stage_overrides`, `secrets`, `volumes`, `deployment_strategy`, `deployments`, `dependencies`, `vars`.
  `schema_version: 2` (`APPS/aac-lyndrix-core/service.yml:32`). `stage_overrides` really only uses
  `prod: {config, service}` (`service.yml:33-38`). `deployment_strategy`: `main → prod`, `dev → dev`,
  `test` `enabled: false` (`service.yml:75-93`).
- **Consequence:** `maintenance:`/`incidents:` already survive unchanged to the output today. A schema
  would be an addition (linter), **no `schema_version` bump needed** (the version only controls
  env/secret behaviour, `TE/tests/test_processors.py:189,216`).

## 3. Render path and merge order

1. The orchestrator places `service.yml` (`prepare_and_run_generator.yml:3,8-17`), copy into an isolated
   build path (25-29).
2. Ansible merge (36-46): `service.yml` → `combine(ansible_host_ip)` →
   `combine(service_item.config, recursive)` [controller `hosts.yml services[].config`] →
   `combine(service.stage)` → `combine(inventory_hostname)`.
3. `python -m manifest_generator.main --ssot-json --stage` (`main → prod`) (50-57).
4. `main.py:66-75` strategy/kill switch (stage via `deployment_strategy.target_stage`);
   `ContextBuilder`; `stage_overrides[stage]` is deep-merged **last** (`context.py:80-84`);
   Jinja recursion (86-88).
5. Processor chain (`main.py:85-95`): imports (`catalog/*.yml`), metadata, ports, environment,
   networks, ingress, spec, volume, ansible (produces `ansible_directories`, `processors/ansible.py:108`).
6. `ansible_context.json` is always written, also when `deployment_enabled=false`
   (`main.py:101-110`); afterwards `engine.render_all/documentation/files/ansible` (`main.py:112-136`;
   `engine.py:15,41,72,110`); Ansible reads `final_ssot_vars` (`prepare_and_run_generator.yml:88-92`).

- **Effective order:** SSoT → controller `config` → `stage_overrides`. `stage_overrides`
  therefore beats the controller override.
- Lists are **replaced** (`_deep_merge`, `context.py:11-19`; Ansible `combine` = default, not
  verified).
- Controller values must be literal: they pass through `CORE/services/renderer.py:105-120`
  (2 passes, `PermissiveUndefined` → empty; only `ansible_host_ip`/`ansible_host`/`inventory_hostname`
  stay literal, line 63). Window data is literal, hence uncritical.

## 4. iac-controller: structure of hosts, VMs and stages

- `environments/global/{01_global_vars,02_service_catalog,03_profiles,04_agents}.yml`;
  `sites/<site>/{site_vars,networks,hardware}.yml` (`site = onprem | hetzner`);
  `sites/<site>/stages/<stage>/{stage,stage_vars,hosts}.yml` (`stage = dev | test | prod`).
- Load order: `CORE/services/loader.py:28-50`; lists are **united**
  (`unique_list_merge`, `loader.py:6-15`).
- `stage.yml`: metadata only (the top key there is called `sage_details`, probably a typo, harmless).
  `stage_vars.yml`: `stage_vars.{ansible_vars, terraform_vars}`.
- `hosts.yml` (top key `hosts`; onprem/prod 10 hosts): `hostname`, `ansible_host`, `profiles[]`,
  `swarm`, `terraform{is_managed,node_name,vm_id,ip,cores,ram,…}`, `ansible_groups[]`,
  `baseline_roles[]`, `services[]{name,state,deploy_type,config{…}}`. Models
  `StandardHost`/`ServiceDefinition` (`CORE/validate/models.py:6-12,44-53`), `extra="allow"`.
- Hardware: `sites/<site>/hardware.yml` → `hardware_hosts` (hydra, cerberus, truenas) + `cluster`
  (olympus, nodes); `HardwareNode`/`HardwareCluster` (`models.py:55-75`), `extra=allow`. No
  `services:` block (`CTRL/features/001-hypervisor-services-block.md`, OPEN).
- VMs/LXC: `terraform.is_managed` → container in tfvars (`CORE/gen/terraform/mapper.py:8`);
  `is_used` → provider (`mapper.py:6`). A `maintenance:` next to `terraform:` does not touch TF
  (the mapper only reads the `terraform` dict, `mapper.py:75,123`).
- Profiles (`03_profiles.yml`) are merged under the host data (`CORE/generator.py:55-88`)
  = a further inheritance level.
- `site_vars.timezone: Europe/Berlin` exists
  (`CTRL/environments/sites/{onprem,hetzner}/site_vars.yml:4`), usable as a tz default.
- Transport: git sync + webhook `/api/iac/webhook/sync` (`CTRL/.gitlab-ci.yml:52-80`),
  self-healing loop as a backstop. The controller has no schema/unit tests.

## 5. Inheritance/precedence mechanics today and candidate for the effective resolution

Existing (all deep merge, the more specific wins): global → site → stage files
(`loader.py:33-49`); profile → host (`generator.py:55-88`); global/site/stage_vars → every host,
hardware without stage_vars (`generator.py:148-181`); SSoT → controller `services[].config`
(`prepare_and_run_generator.yml:36-46`); → `stage_overrides` (`context.py:80-84`). There is **no**
service → host → stage-default resolution (today's direction is the reverse: the controller
overrides the SSoT).

- **Variant A (iac_core/orchestrator, sync time):** knows hosts, stage, site, profiles, `services[]`
  (`generator.py:115-181`; `ansible_gen.py:78`); the path to monitoring/registry exists
  (`ORCH/app/controller/engine.py:1161-1196` `_build_inventory_hosts`, `1271-1286`
  `iac:inventory_updated`); windows are needed **before** the deploy. Con: it reads no
  `service.yml`; the orchestrator does have the files, though (`engine.py:956-1010` `rglob service.yml` in
  `services_dir`) and would have to read `maintenance`/`incidents` (+ `stage_overrides`) as well.
  `_build_inventory_hosts` has a fixed field list, new fields must be added explicitly.
- **Variant B (template engine, deploy time):** sees `service.yml` completely incl.
  `stage_overrides`; result as `final_ssot_vars`. Con: only at deploy, does not know host/stage defaults,
  the result does not reach the orchestrator via the inventory; chicken-and-egg for scheduled deployments.
- **Factual conclusion:** the resolution belongs in **A**; B can validate/pass through the service level.
  Decision open.

## 6. Passing inventory variables through (pattern)

- `process_host` writes the whole host dict as hostvars (`CORE/gen/ansible_gen.py:59-63`);
  `extra=allow` (`models.py:53,64`). A host key `maintenance:` becomes a hostvar without a code change.
- `stage_vars`/`site_vars`/`global_vars` → `group_vars` (`ansible_gen.py:44-54`; `generator.py:136-146`)
  **and** merged into every host (`env_context`, `generator.py:148-181`). A key at the root level of
  `stage.yml` does **not** end up in `group_vars` (`ansible_gen.py:46-48`), hence
  place `maintenance_default`/`incident_defaults` **under `stage_vars:`**. Hardware hosts get
  no stage_vars, so hypervisor defaults go into `site_vars`.
- Pattern `ansible_host_ip`: preserved as literal Jinja in the orchestrator (`renderer.py:63,99-102`), set in
  Ansible via `combine` (`prepare_and_run_generator.yml:40-42`), consumed in
  `processors/ports.py:37`, `ingress.py:81`. `maintenance_effective` would run identically via
  `combine` in variant B (lines 40-45). Service level in the inventory: `services[]` per host + groups `service_<name>`
  (`ansible_gen.py:78-80`).

## 7. Test status and documentation locations

- **TE:** pytest, `PYTHONPATH=$(pwd)/scripts pytest tests/ -v`; CI `python-unit-tests`
  (`TE/.gitlab-ci.yml`, python:3.12, pytest/pyyaml/jinja2) + `yaml-syntax-check`.
  `tests/test_processors.py` (382 lines, 15 tests; the only `ContextBuilder` test line 237, **no
  `stage_overrides` test**), `test_ansible_render.py`, `test_template_render.py`,
  `test_render_files.py`; fixture `tests/service-test/service.yml` (`stage_overrides` line 29).
  `validate_ssot.py` has no tests; invocation in CI not verified. Migration scripts
  `TE/scripts/migration/`.
- **Controller:** no schema/unit tests; `iac_core` tests only `ORCH/iac_core/tests/test_terraform_gen.py`.
- **Docs (DE):** `TE/documentation.md` (465 lines; section 3 `service.yml`, lines 36-165 → new
  subsection 3.x; section 6 outdated), Hugo `aac-platform/aac-iac-documentation/content/aac-services/`,
  `CTRL/README.md` (boilerplate), `CTRL/features/README.md` (index; new file
  `007-maintenance-windows.md` by convention), MkDocs hub `CTRL/mkdocs.yml` +
  `.gitlab-ci/scripts/generate_hub.py`.

## 8. Renovate → webhook → deploy (IaC side)

- `renovate.json` in TE, CTRL, `APPS/aac-lyndrix-core` contains only `$schema`. The real configuration
  lives in the Renovate service `APPS/aac-renovate/service.yml` (`RENOVATE_CONFIG_FILE=/config/config.js`,
  lines 72-83) + `custom_templates` (not evaluated).
- **Flow:** Renovate MR → MR pipeline (`generate`/`validate` on `merge_request_event` against
  `PROD_BRANCH`, `service-pipeline.yml:194-237`) → merge `main` → `prod-generate` → `prod-validate` →
  `prod-trigger-orchestrator` (`service-pipeline.yml:299-386`):
  `POST $LYNDRIX_ORCHESTRATOR_URL/api/iac/deploy/service/$CI_PROJECT_NAME/gitlab`, body
  `{"branch": PROD_BRANCH}`, header `X-Gitlab-Token`; rule `LYNDRIX_DIRECT_TRIGGER_ENABLED == "true"
  && branch == PROD_BRANCH` (line 386; default `"true"` lines 40-43). Afterwards a **wait loop** on
  `…/gitlab/status` (`LYNDRIX_DEPLOY_WAIT_*`, timeout 1800 s, lines 44-48). Memory: parallel
  GitLab webhooks only with `pipeline_events=False`.
- **Important for the plan:** a deploy parked as `SCHEDULED` would block/time out the wait loop
  and turn the GitLab job red. This must be coordinated with the orchestrator sweep.

## 9. Attachment points for section 7.5

1. `TE/scripts/validate_ssot.py`, `validate_ssot()` after block 7b (~line 139): optional block
   `maintenance`/`incidents` (`weekly.days`, `HH:MM`, `tz` via `zoneinfo`, `once.start < end`, `cron`
   5 fields + `duration_minutes > 0`, `deploy_policy in {window, immediate}`, `offline_minutes >= 0`,
   `severity`). Errors only for wrong types, otherwise warnings; unknown keys under `maintenance` →
   warning.
2. `TE/…/context.py` safety net (lines 55-73): optional `"maintenance": {}`, `"incidents": {}`.
3. New `TE/…/processors/maintenance.py` + entry in `main.py:85-95` (after `AnsibleProcessor`):
   normalises weekly/once/cron, sets the default `deploy_policy` by stage (prod → window, otherwise
   immediate), writes `context['maintenance_effective']` (service level).
4. `TE/tests/test_processors.py`: normalisation, `stage_overrides.prod.maintenance` (list replaced),
   linter cases.
5. `TE/documentation.md`: new section 3.x.
6. `CTRL hosts.yml` (per host) / `hardware.yml` (per hardware host): `maintenance: {windows, inherit}`
   next to `terraform:`/`services:`; runs through to the hostvars without a code change.
7. `CTRL stages/<stage>/stage_vars.yml` **under `stage_vars:`**: `maintenance_default {enabled, windows}`,
   `incident_defaults {service, host, vm, container}`. Hardware: `site_vars.yml`. Global type defaults
   make sense in `global/01_global_vars.yml` (`global_vars:`).
8. `CORE/validate/models.py` `StandardHost`/`HardwareNode` (lines 44-72): optional Pydantic submodel
   `MaintenanceConfig`/`IncidentRule` (the only formal controller validation location; coordinate with the
   orchestrator session), otherwise `extra=allow` suffices.
9. `ORCH/app/controller/engine.py` `_build_inventory_hosts` (lines 1161-1196): `maintenance_effective`/
   `incidents_effective` per host and service in the `iac:inventory_updated` payload; resolve the inheritance
   here (variant A).
10. `CTRL/features/007-maintenance-windows.md` + `features/README.md` (index lines 10-20).
11. `ANS prepare_and_run_generator.yml:40-45` and `cd_rollout_single_service.yml:57-61`: combine chain
    only for variant B.

- **Writing the default switch back:** target file `stage_vars.yml` → `stage_vars.maintenance_default.enabled`.
  The file contains a TF backend block with a credential key (only the key name seen):
  preserve comments/order, **do not dump it raw again** (see `CTRL/features/002-secrets-sops-hardening.md`).
- **Risks:** (a) literal-only in the controller (two Jinja passes with an empty fallback, `renderer.py:71-120`;
  `{{ }}` in `once.note` would be rendered/emptied). (b) Lists: the loader unites (`loader.py:6-15`),
  Ansible/`_deep_merge` replace; define defaults per key on **one** level only.
  (c) `stage_overrides` beats the controller override (only relevant for variant B). (d) Hardware without
  stage_vars. (e) no `schema_version` bump needed; a bump to 3 would touch migration scripts,
  not recommended. (f) Idempotency: `write_yaml_if_changed` (`ansible_gen.py:12-38`); no volatile
  values (e.g. "next window") in the inventory, otherwise a commit per sync. (g) The inventory state is
  git/SOPS-versioned; a pure `maintenance` change might trigger an Ansible run via `pipeline_rules.yml`
  (not verified).

## 10. Deviations from the plan and open questions

1. Plan 4.1 "template engine validates": in reality no validation in the render path, only an optional
   linter; unknown keys are silently passed through. Validation would be new development.
2. Plan 4.2 "rendering (template engine / iac_core) resolves inheritance": the template engine does not know
   host/stage defaults and only runs at deploy; `iac_core` knows no `service.yml`.
   Decision needed (the facts favour A with `service.yml` reading in the orchestrator).
3. The plan mentions `stages/<stage>.yml`: in reality `stage.yml`/`stage_vars.yml`; it only becomes a
   hostvar under `stage_vars:`; hardware has no stage vars.
4. The stage concept is doubled: controller directory (`stages/dev|test|prod` per site) vs. branch/
   `target_stage` in `service.yml` (`main → prod`, `test` disabled in real services). The resolution
   must use the controller stage of the host.
5. Service on several hosts: does the SSoT window apply to all instances or can it be overridden per host via
   `services[].config.maintenance` (mechanism exists)?
6. Orchestrator wait loop vs. `SCHEDULED` deploy (section 8): cross-sweep question.
7. Writing the default switch back produces commits on `iac-controller/main` → webhook sync → new
   run; check the loop risk (ignore the commit author or make it idempotent).
8. The TE image only has pyyaml, jinja2, mkdocs-material (`TE/Dockerfile`, `requirements.txt`), no
   cron library; the linter only with stdlib (`zoneinfo`) / its own 5-field parser; cron evaluation
   stays a core library.
9. Renovate schedule/automerge (`APPS/aac-renovate` `config.js`) not evaluated; clarify whether
   Renovate controls merges in time (avoid double windows).
10. Not verified: Ansible `combine` list behaviour, rollout triggering by an
    inventory-state change (`pipeline_rules.yml`), whether `validate_ssot.py` runs in a CI.
