# Lyndrix Production-Readiness & Governance Audit

_Audit of `lyndrix-core` + the 7 shipped plugins. "Safe fixes" from this audit have been
applied (governance files, broken-README rewrites, a non-breaking insecure-default warning);
the heavier items are listed as follow-ups._

## Per-repo summary

| Repo | Governance | Quality tooling | Tests | Gut-check |
|---|---|---|---|---|
| **lyndrix-core** | LICENSE (Apache-2.0) | ruff/black/mypy/pytest in `requirements-dev.txt`; CI runs lint (`dev-checks.yml`) | **none in `app/`** | needs-work |
| **iac-orchestrator** | CHANGELOG (LICENSE added) | full dev deps; docs CI only | `tests/` ~278 lines (real) | good |
| **server-manager** | LICENSE + CHANGELOG | full dev deps; docs CI only | smoke (~151 lines) | good |
| **docker-manager** | none (added) | none | none | rough |
| **monitoring** | LICENSE + CHANGELOG | full dev deps; docs CI only | smoke (~41 lines) | good |
| **external-services** | LICENSE + CHANGELOG | full dev deps; docs CI only | smoke (~55 lines) | good |
| **discord-notifier** | CHANGELOG (LICENSE added) | full dev deps; docs CI only | smoke (~39 lines) | needs-work |
| **meeting-bingo** | none (added) | none | none | rough |

## Key findings

- **Testing:** `lyndrix-core` has **no real test suite** despite shipping the test toolchain;
  plugins have only smoke tests. No coverage gates anywhere.
- **CI:** core runs ruff/black; **plugins run only the docs workflow** — no lint/test gate.
- **Silent exceptions:** `except Exception: pass` is widespread — ~57 in core, ~28 in
  iac-orchestrator, 1–4 in the others (mostly UI/discovery error-swallowing).
- **Insecure default:** `lyndrix-core/app/config.py` ships `VAULT_SKIP_VERIFY=True`.
- **Secrets:** otherwise clean — credentials come from env/Vault, no hardcoded secrets found.
- **Docs:** core, iac-orchestrator, external-services, monitoring have strong READMEs; the
  **docker-manager and discord-notifier READMEs were copy-paste of meeting-bingo** (now fixed).
- **Governance:** no `CODE_OF_CONDUCT.md` / `SECURITY.md` existed anywhere; several plugins
  lacked a `LICENSE`.

## Safe fixes applied (this pass)

1. Rewrote the broken **docker-manager** and **discord-notifier** READMEs to describe the real plugin.
2. Added **`CODE_OF_CONDUCT.md`** (Contributor Covenant) and **`SECURITY.md`** to all 8 repos.
3. Added **`LICENSE`** (Apache-2.0, copied from core) to iac-orchestrator, docker-manager, discord-notifier, meeting-bingo.
   - ⚠️ Note: core's `README.md` says "MIT" but the actual `LICENSE` file is **Apache-2.0** — the
     README line should be corrected to Apache-2.0 (flagged; not auto-changed).
4. Added a **non-breaking warning** for `VAULT_SKIP_VERIFY` in `warn_insecure_defaults()` (logs in
   non-dev environments). The default is intentionally **not** flipped — see follow-ups.

## Follow-ups (not done — need a decision / larger effort)

1. **Core test suite** — add real unit tests under `app/` (services: auth, plugins, messaging, theming).
2. **CI gates in plugins** — add ruff/black/mypy/pytest workflows (mirror core's `dev-checks.yml`).
3. **`VAULT_SKIP_VERIFY` hard-gate** — default to `False` in prod **only after** confirming prod
   Vault presents a valid (non-self-signed) certificate; otherwise the warning stays.
4. **Audit the `except Exception: pass` blocks** — replace silent swallowing with scoped handling +
   logging, starting with `lyndrix-core` (secret/plugin/auth paths) and iac-orchestrator.
5. **Tooling baseline** — add `requirements-dev.txt` + lint config to docker-manager and meeting-bingo;
   add a `.pre-commit-config.yaml` across repos (a `pre-commit` dep is already present in core).
6. **`bus.py` TODO** — redact sensitive keys from event payloads by default.
7. **Plugin-source hardening** (`plugin_service.py` TODOs) — signed/allowlisted sources + hash-pinned
   requirements.
