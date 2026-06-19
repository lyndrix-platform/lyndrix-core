---
name: run-lyndrix-core
description: Build, launch, and drive the lyndrix-core app (FastAPI + NiceGUI platform). Use to run/start lyndrix-core, screenshot its UI (login, dashboard, Plugin Manager) at desktop + mobile widths, smoke-test the HTTP API, or verify a UI/backend change in the actually-running app.
---

# Run lyndrix-core

lyndrix-core is a **FastAPI + NiceGUI** app: the dashboard runs in-process with
the API (`ui.run_with(app, ...)`), so every page is an SPA shell that paints
over a websocket. `curl` only ever sees the empty shell — to *see* the UI you
need a real browser. The driver here, **`.claude/skills/run-lyndrix-core/driver.py`**,
uses Playwright/Chromium to log in and screenshot rendered pages at desktop and
mobile widths. For backend/API checks it can also just hit `/api/health`.

The full app needs **Vault + MariaDB + the boot sequence**, so it runs as a
docker-compose stack, not a bare process. The compose file already exists.

All paths below are relative to the `lyndrix-core/` directory.

## Prerequisites

Driver runtime (Python venv + Chromium — `node`/`chromium-cli` are NOT needed):

```bash
python3 -m venv .dev/run-venv
. .dev/run-venv/bin/activate
pip install playwright
python -m playwright install chromium
sudo $(which python) -m playwright install-deps chromium   # installs libnspr4/libnss3/... (26 pkgs)
```

The last line is required — without the system libs Chromium fails to launch with
`libnspr4.so: cannot open shared object file`.

## Start the stack

If it's already up, skip this (`docker ps` shows `lyndrix-core-dev` on `:8081`).
From a clean machine:

```bash
docker compose -f docker/docker-compose.dev.yml up -d --build
```

Endpoints: app `http://localhost:8081`, Vault `:8200`, docs `:8000`. Confirm the
app is healthy (also prints the running version):

```bash
curl -s http://localhost:8081/api/health
# {"status":"unknown","core_version":"0.1.2","api_version":"1.2.0","plugins":{...6 plugins...}}
```

> `status`/per-plugin `"unknown"` is the default health aggregation, not an error.
> `core_version` + `api_version` are the useful signal.

## Run (agent path) — driver.py

The driver logs in and screenshots a set of routes at **desktop (1440×900)** and
**mobile (390×844)**. The admin password is read from the environment (never
hardcoded) — source it from the dev env file:

```bash
cd .claude/skills/run-lyndrix-core
. ../../../.dev/run-venv/bin/activate
export LYNDRIX_ADMIN_PASSWORD="$(grep -E '^LYNDRIX_ADMIN_PASSWORD=' ../../../docker/.env.dev | cut -d= -f2-)"
python driver.py
```

Output → `.claude/skills/run-lyndrix-core/shots/<route>.<desktop|mobile>.png`
(plus `login.desktop.png`). Verified shots this session: `login`, `root`,
`dashboard`, `plugins`, `settings` — the Plugin Manager renders a 3-col grid on
desktop and a 1-col stack on mobile.

Useful variants:

```bash
python driver.py --routes /plugins                 # just the Plugin Manager
python driver.py --routes /plugins --no-mobile     # desktop only
python driver.py --health-only                     # no browser; print /api/health and exit
python driver.py --base http://localhost:8081 --user admin   # explicit target/user
```

After it runs, **open the PNGs and look** — a blank or login-looped image means
the flow failed (usually a wrong password).

## Direct API smoke (no browser)

The REST surface is broad (`curl -s http://localhost:8081/openapi.json`). Quick
unauthenticated checks:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8081/         # 200 (login shell)
curl -s http://localhost:8081/api/health | python3 -m json.tool
```

## Run (human path)

`docker compose ... up` then browse to `http://localhost:8081` and log in as
`admin`. Useless headless — that's what the driver is for.

## Test

No test suite exists in core itself yet (`pytest` from the repo root collects
nothing). Lint/type/i18n gates: `ruff check app/`, `black app/`,
`python scripts/check_i18n_keys.py`.

## Gotchas

- **NiceGUI is a websocket SPA.** `curl` of any page returns a ~19 KB shell, not
  the UI (`x-nicegui-content: page`). Only the browser driver shows real content;
  the driver sleeps ~1.5 s after `networkidle` so the socket can paint.
- **No `node`, no `chromium-cli`** on this host — the driver is Python Playwright
  on purpose.
- **Login form has no stable names/ids** (Quasar-rendered). The driver targets
  the password field by `input[type=password]` and the username by the first
  `input:not([type=password])`, then submits with Enter (falling back to a
  Login/Anmelden button). Works against the current "Lyndrix Login" card.
- **Secrets stay out of the repo.** The driver refuses to run without
  `LYNDRIX_ADMIN_PASSWORD` in the env rather than carrying a default.
- **Sibling plugin repos are volume-mounted** by `docker-compose.dev.yml`
  (e.g. `../../lyndrix-plugin-*` → `/app/plugins/*`), so plugin code edits on the
  host are live in the container.
- **The venv + screenshots are gitignored** (`.dev/` repo-wide; `shots/` in the
  skill dir) — only `SKILL.md` and `driver.py` are committed.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `libnspr4.so: cannot open shared object file` / `TargetClosedError: BrowserType.launch` | Run `sudo $(which python) -m playwright install-deps chromium`. |
| `error: set LYNDRIX_ADMIN_PASSWORD ...` | `export LYNDRIX_ADMIN_PASSWORD="$(grep -E '^LYNDRIX_ADMIN_PASSWORD=' docker/.env.dev \| cut -d= -f2-)"`. |
| Driver hangs on `/login` / shots show the login card | Wrong password — re-check the value in `docker/.env.dev`. |
| `curl: connection refused` on :8081 | Stack not up — `docker compose -f docker/docker-compose.dev.yml up -d`. |
