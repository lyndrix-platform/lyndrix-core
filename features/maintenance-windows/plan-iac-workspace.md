# Sub-plan P6: IaC workspace — handover to the IaC session

Basis: `analysis-template-engine-controller.md`, `00-ergebnis-p0.md` (section 2 schema,
section 4 point 10 CI contract). Repos: `aac-platform/aac-template-engine` (TE),
`iac-platform/iac-controller` (CTRL), `aac-iac-documentation` (Hugo). The Lyndrix side
(orchestrator) reads the values; the IaC side validates, documents and adapts the service CI.
No `schema_version` bump (the version only controls env/secret behavior).

## 1. Template engine 0.0.10

1. **Linter** `scripts/validate_ssot.py` (`validate_ssot()`, after block 7b ~line 139): optional
   block `maintenance` / `incidents` (top level and under `stage_overrides.<stage>`):
   `windows[]` per entry exactly one of the keys `weekly|once|cron`; `weekly.days` not empty, values
   `mon…sun` or `0…6`; `start`/`end` `HH:MM`; `once.start < once.end` (ISO-8601 with offset);
   `cron.expr` 5 fields, `duration_minutes` int > 0; `tz` checkable via `zoneinfo.ZoneInfo`;
   `deploy_policy ∈ {window, immediate}`; `incidents.offline_minutes` int ≥ 0, `severity ∈
   {critical, high, medium, low}`, `enabled`/`auto_resolve` bool. Wrong types → error,
   unknown keys under `maintenance`/`incidents` → warning. Only stdlib (no `croniter`).
   Kind keys under `incident_defaults` (controller) are open: slug `^[a-z][a-z0-9_]*$`,
   no fixed list (decision A).
2. **Safety net** `scripts/manifest_generator/context.py:55-73`: `"maintenance": {}`,
   `"incidents": {}` as defaults, so that templates do not run into missing keys.
3. **No own processor**: the effective resolution happens in the orchestrator (variant A).
   The keys are passed through as today (`ansible_context.json`), so that Ansible roles can read them
   if needed (e.g. as a label on the container). Optionally a `MaintenanceProcessor` that
   only normalizes — not needed for the first release.
4. **Service CI** `templates/cicd/service-pipeline.yml` (job `prod-trigger-orchestrator`,
   299-386): status poll with `?accept=scheduled`; `DEPLOY_STATUS == "scheduled"` → output
   `Lyndrix deployment scheduled for ${NOT_BEFORE} (job_id=…) — maintenance window policy` and
   `exit 0` (job green, deployment runs later). New variable
   `LYNDRIX_DEPLOY_ACCEPT_SCHEDULED: "true"` (default on). Note in the comments 40-48.
   Until this template is rolled out, the orchestrator continues to deliver `success` without `accept`
   with `job_status: SCHEDULED` (compatibility mode).
5. **Tests** `tests/test_processors.py` (+ possibly `tests/test_validate_ssot.py` new): linter
   cases valid/invalid; `stage_overrides.prod.maintenance` replaces the list
   (document `_deep_merge` behavior); fixture `tests/service-test/service.yml` extended by a
   `maintenance` block.
6. **Docs** `documentation.md` section 3 (36-165): subsection "3.x Maintenance windows and
   incident rules" with the schema from `00-ergebnis-p0.md` 2.2, inheritance chain and
   `deploy_policy` default; section 6 (209-265, outdated) corrected to `prod-trigger-orchestrator` at the same time. Update Hugo `content/aac-services/`.
7. `version.py` → 0.0.10, release as usual (observe the base image chain, memory
   `iac-base-image-chain`).

## 2. iac-controller

1. **Schema docs** `features/007-maintenance-windows.md` (convention `features/README.md`
   index 10-20): blocks per host (`hosts.yml` `maintenance: {windows, inherit}`, optional
   `incidents`), per hardware host (`hardware.yml` `maintenance`), per service instance
   (`services[].maintenance`), stage (`stage_vars.maintenance_default`,
   `stage_vars.incident_defaults`), site (`site_vars.maintenance_default`,
   `site_vars.incident_defaults`, `site_vars.timezone` as tz default). Rules: values literal
   (no Jinja, `{{ }}` would be emptied), lists are replaced (define windows only once per level),
   type keys = entity registry kinds (open; today `hypervisor`, `host`,
   `service`, `container`, `network` …). Note: without windows on the horizon the
   orchestrator deploys immediately and the ITSM creates a task "Define maintenance window".
2. **Example entries** in `environments/sites/onprem/stages/prod/stage_vars.yml` (under
   `stage_vars:`): `maintenance_default: {enabled: false, windows: [{weekly: {days: [sun],
   start: "02:00", end: "04:00"}}]}` and `incident_defaults` with the defaults from result
   2.3. `enabled: false` until Marvin flips the switch. **Edit the file line by line**; the TF backend block with the credential key stays untouched (see `features/002-secrets-sops-hardening.md`).
3. **Default switch** (decision C, 2026-10-18): no push right for the orchestrator.
   On/off is edited in the controller repo (line `enabled:` in block `maintenance_default`),
   CI (`.gitlab-ci.yml:52-80`) triggers the webhook sync on commit, the orchestrator republishes.
   Write-back from the Lyndrix UI comes later via the SSoT manager plugin
   (`00-ergebnis-p0.md` section 8); its commits begin with
   `ci: maintenance` and trigger no deploy. For this bot account "Allowed to
   push" on `main` must then be set up.
4. **Hub/MkDocs**: `mkdocs.yml` includes the new feature file (check `generate_hub.py`).

## 3. Renovate

`aac-renovate` `config.js` (not evaluated): check whether a `schedule` is set there. With
the window policy in the orchestrator, Renovate should be allowed to merge **without** a time window
(the window is guarded by the orchestrator), so that two windows do not wait one after the other. Decision IaC session.

## 4. Acceptance

- Linter red for `weekly.days: []`, green for the example from 2.2; CI `python-unit-tests` green.
- A service with `deploy_policy: window` on `prod`: the CI job ends green with "scheduled",
  the orchestrator shows the job under "Scheduled deployments".
- Controller example values appear after sync in `GET /api/plugins/lyndrix.plugin.iac_orchestrator/maintenance-windows`.
