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
