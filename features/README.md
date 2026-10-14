# `features/` — geplante Erweiterungen & neue Plugins

Dieser Ordner sammelt **Design-Dokumente für geplante Erweiterungen** von Lyndrix —
neue Plugins, größere Umbauten am Core, plattformweite Konzepte. Jede Idee bekommt
ein `.md`-File, **bevor** Code entsteht: Motivation, Architektur-Entscheidung,
Migrationspfad, offene Fragen. So bleibt die Richtung diskutierbar, solange sie noch
billig zu ändern ist.

Konvention (wie im restlichen Repo): **Prosa auf Deutsch, Code/Identifier/APIs auf
Englisch.** Ein Doc ist „erledigt", wenn seine Punkte umgesetzt und released sind —
dann wandert der bleibende Teil in die reguläre `documentation.md` / das Plugin-Repo,
und das Feature-Doc darf weg (oder wird als „shipped" markiert).

## Index

| Doc | Thema | Status |
|---|---|---|
| [`lyndrix-agent-framework.md`](./lyndrix-agent-framework.md) | Verschlüsselter Host-Agent (`lyndrix-agent`) + `agent-management`-Plugin — ersetzt Klartext docker-proxy/dockermon/docker-to-dns | 📐 Design |
| [`high-availability.md`](./high-availability.md) | Hochverfügbarkeit: N Backends / Frontends / DB-Knoten / Vault-Knoten — Zustandsinventar Core + alle Plugins, Zielarchitektur (DB-basierte Koordinationsschicht, Leader-Lease, `placement`), Galera-Single-Writer, Vault-Raft, Phasenplan, Deployment-Profile Compose×3 vs. k3s | 🚧 Phase 0+1 umgesetzt (Core 1.4.0 / API 1.7) |
| [`high-availability-implementation.md`](./high-availability-implementation.md) | HA-Umsetzung modular für Docker **und** Kubernetes: Tier-Modell, Konfigurationsoberfläche (`LYNDRIX_CLUSTER_MODE` …), `cluster`-/`storage`-Komponente, Migrate-CLI, Drain, LB-Vertrag + Optionen für den separaten Docker-LB, Repo `lyndrix-deploy`, neuer Doku-Baum `docs/ha/`, Meilensteine M0–M6 | 🚧 M0+M1 umgesetzt, Betreiber-Doku in `docs/ha/`; M2–M6 offen |
