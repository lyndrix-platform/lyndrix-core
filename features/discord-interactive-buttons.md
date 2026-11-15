# Discord: interaktive Buttons (Bestätigen / Übernehmen)

**Status:** 💡 Idee / geplant — im Plugin seit `discord-notifier` 1.3.0 bewusst **abgeschaltet**.
**Betrifft:** `lyndrix-plugin-discord-notifier`, Messaging-Gateway im Core, später ITSM als erster Nutzer.
**Herkunft:** Audit-Befund DN-03 (2026-11).

## Motivation

Eine Benachrichtigung in Discord soll Aktionen tragen können, zum Beispiel einen Incident
bestätigen oder übernehmen, ohne in die Lyndrix-UI zu wechseln.

## Warum es heute aus ist

Die Ed25519-Signatur einer Interaction beweist nur, dass die Anfrage von Discord kommt. Sie
sagt nichts darüber, wer geklickt hat: Jedes Mitglied, das die Nachricht sieht, könnte die
Aktion auslösen, und kein Discord-Nutzer ist einer Lyndrix-Identität zugeordnet. Das Plugin
beantwortet einen Klick deshalb mit einer ephemeren Meldung „not linked / not authorized" und
leitet nichts auf den Bus weiter. Interaction-Token und Member-Objekt gelangen nie in die
Event-Metadaten. Heute sendet kein Plugin Aktions-Buttons, es fällt also nichts weg.

## Voraussetzungen für die Wiederaktivierung

1. **Account-Linking** — Zuordnung Discord-User-ID → Lyndrix-User. Der Nutzer legt sie
   angemeldet in Lyndrix an (nie anhand eines Namens aus Discord), sie liegt im Core und ist
   durch den Nutzer und durch einen Administrator widerrufbar.
2. **Berechtigungsprüfung je Aktion** — der aufgelöste Lyndrix-User braucht im Moment des
   Klicks die Permission, die die Aktion im zuständigen Plugin verlangt. Mitgliedschaft im
   Discord-Kanal reicht nie.
3. **Audit-Trail** — jeder angenommene und jeder abgelehnte Klick wird festgehalten:
   Lyndrix-User, Discord-User-ID, Aktion, Zielobjekt, Ergebnis.
4. **Platz im Gateway, nicht im Provider-Plugin** — Auflösung und Prüfung passieren im
   Messaging-Gateway, bevor das Resume-Topic emittiert wird. So gilt dieselbe Garantie für
   jeden Provider (Discord, später andere).

## Offene Fragen

- Wo lebt die Zuordnung (eigene Tabelle `user_external_identities` oder Erweiterung der
  bestehenden Identitäts-Links aus OIDC/LDAP)?
- Wie wird der Link bestätigt — Einmal-Code aus der Lyndrix-UI, den der Nutzer per
  Slash-Command in Discord einlöst, oder OAuth gegen Discord?
- Gilt eine Aktion pro Klick oder braucht es für kritische Aktionen eine zweite Bestätigung
  in Lyndrix?
- Absenderidentität auf dem Bus (heute kann jedes Plugin ein Resume-Topic emittieren) — hängt
  am gleichnamigen offenen Punkt aus dem Core-Audit.

## Anknüpfungspunkt im Code

`DiscordGatewayAdapter.lyndrix_user_for(discord_user_id)` im Plugin liefert bis dahin für
jeden `None` (fail closed). Plugin-Doku: `docs/planned-features.md` im Plugin-Repo.
