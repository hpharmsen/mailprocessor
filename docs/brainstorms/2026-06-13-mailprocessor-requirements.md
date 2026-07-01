---
date: 2026-06-13
topic: mailprocessor
input_source: file
input_path: docs/doel.md
---

# Mailprocessor

## Problem Frame
HP krijgt regelmatig e-mails met attachments (maandrapporten, facturen) van vaste afzenders, die telkens op dezelfde manier moeten worden afgehandeld: bijlage opslaan op een specifieke locatie, mail markeren als gelezen, en zichzelf een bevestiging sturen. Dat is repetitief en foutgevoelig. Mailprocessor automatiseert dit op basis van regels die HP zelf in natuurlijke taal beschrijft in `tasks.md`.

## Requirements

- **R1.** Cronjob draait elk uur. Bij start parseert het script `tasks.md` deterministisch (regex) en haalt daaruit alle e-mailadressen die als afzender genoemd worden. Dat is de witte lijst voor deze run.
- **R1a.** Gmail-query haalt alleen ongelezen mails op die (a) afzender hebben in de witte lijst van R1 én (b) geen `mailprocessor/done` of `mailprocessor/failed` label hebben. Als de query nul mails oplevert, exit het script zonder enige LLM-call.
- **R2.** Voor elke mail die R1a oplevert, beoordeelt een justai-agent welke regel uit `tasks.md` van toepassing is. `tasks.md` is de bron van waarheid: HP beschrijft daar in natuurlijke taal welke afzender + attachment-naam-patroon op welke locatie moet worden opgeslagen, en welke vervolgacties (markeer gelezen, reply) horen erbij.
- **R3.** De agent beschikt over tools voor (a) Gmail-toegang via Gmail API met OAuth (lezen, attachments downloaden, labels zetten, markeer-gelezen, reply sturen) en (b) lokale disk-toegang (mappen aanmaken, bestand opslaan).
- **R4.** Bij een succesvolle verwerking voert de agent de acties uit in deze volgorde: (1) attachment opslaan op de doelpad, (2) reply sturen naar hp@harmsen.nl met bevestiging + opslagpad, (3) mail markeren als gelezen, (4) label `mailprocessor/done` toevoegen.
- **R5.** Bij een fout in stap (1)–(3) van R4: agent voegt label `mailprocessor/failed` toe en stuurt een mail naar hp@harmsen.nl met foutmelding, afzender, onderwerp en welke stap faalde. Mail wordt niet als gelezen gemarkeerd.
- **R6.** Mails die wél door de afzender-witte-lijst kwamen maar waar het LLM geen passende regel vindt (bv. afzender klopt, maar attachment-naam past niet), blijven onveranderd (geen label, blijven ongelezen). Worden volgende run opnieuw beoordeeld. Mails van afzenders buiten de witte lijst raakt het script niet aan.
- **R7.** Doelpaden in `tasks.md` mogen placeholders bevatten die de agent invult op basis van mailinhoud of attachment-naam (bv. `{yyyy}`, `{mm}`, `{JAAR}`, `[NAAM VAN DE PDF]`). De agent gebruikt het LLM om deze waarden af te leiden.
- **R8.** Logging gaat via justlog. Per verwerkte mail: één regel met afzender, onderwerp, toegepaste regel, uitgevoerde acties of fout.
- **R9.** Secrets (Gmail OAuth credentials/tokens) komen uit `.env` of een lokaal token-bestand; niet gecommit.

## Success Criteria

- Een nieuwe LEAP- of Anthropic-mail die binnen het uur binnenkomt, is binnen 1 cron-cyclus opgeslagen op de juiste plek, gelezen-gemarkeerd, beantwoord, en `done`-gelabeld zonder handmatige tussenkomst.
- HP kan een nieuwe regel toevoegen door alleen tasks.md uit te breiden, zonder code te wijzigen.
- Een falende verwerking levert binnen 1 cyclus een mail in HP's inbox op met genoeg context om handmatig op te lossen.

## Scope Boundaries

- **Geen** UI of dashboard. Alle interactie loopt via tasks.md, e-mail en Gmail-labels.
- **Geen** support voor andere mailproviders dan Gmail.
- **Geen** ondersteuning voor inline-content of body-only mails — alleen attachments worden opgeslagen.
- **Geen** automatische retry-logica bij transient errors (kan later). Bij fout → failed-label + notificatie, klaar.
- **Geen** ondersteuning voor multi-user. Eén Gmail-account (HP's).

## Key Decisions

- **Agent met tools (justai)**: gekozen door HP. Maakt nieuwe regels in tasks.md inzichtbaar zonder code-wijziging.
- **Harde sender-filter vóór LLM**: bij elke cron-run wordt eerst regex-matig uit tasks.md de afzenderlijst gehaald, en alleen mails van die afzenders gaan door de agent. Lege Gmail-query → exit zonder LLM-call. Houdt 99% van de runs gratis.
- **Gmail API + OAuth**: native fit voor HP's mailbox, geeft toegang tot labels, threads en replies die de IMAP-route niet kent.
- **tasks.md blijft natuurlijke taal**: één bron voor mensen en agent, geen aparte YAML.
- **Gmail labels als state**: `mailprocessor/done` en `mailprocessor/failed` zijn de enige persistente state. Geen lokale database, geen extra backup-behoefte.
- **Acties-volgorde markeer-gelezen na save+reply**: bij een crash tussen stappen blijft de mail ongelezen en wordt hij volgende run gewoon opnieuw geprobeerd.
- **Hourly cron**: facturen/rapporten hoeven niet realtime; uurfrequentie houdt kosten en API-load laag.

## Dependencies / Assumptions

- `justai` ondersteunt agent-mode met custom tools (bekend uit andere projecten van HP).
- `justlog` is beschikbaar als logging-laag.
- HP heeft of maakt een Google Cloud project met Gmail API ingeschakeld + OAuth-credential voor desktop-app.
- Cronjob draait op een machine die altijd aan staat (Mac of server) — welke precies is nog open.

## Outstanding Questions

### Resolve Before Planning

(Geen — alle product-beslissingen zijn rond.)

### Deferred to Planning

- [Affects R1][Technical] Op welke machine draait de cronjob (lokale Mac vs Hetzner/Heroku-achtige server)? Bepaalt deployment-route en hoe OAuth-tokens persistent worden bewaard.
- [Affects R4][Technical] Bij meerdere matchende attachments in één mail: één reply met alle paden of één per attachment? Aanname voorlopig: één reply met alle paden.
- [Affects R1][Technical] Regex voor afzender-extractie uit `tasks.md`: standaard e-mailadres-regex volstaat, maar bevestig dat HP altijd het volledige e-mailadres letterlijk in tasks.md zet (niet alleen "LEAP" of "Pelle").
- [Affects R3][Needs research] Concrete tool-interface voor de justai-agent: welke Gmail API endpoints worden tools, en wat is de signatuur (parameters, return-types)?
- [Affects R7][Needs research] Hoe robuust kan het LLM `{yyyy}`/`{mm}`/`[JAAR]` extraheren uit attachment-namen — moet de planner de prompt en fallbacks ontwerpen.
- [Affects R9][Technical] OAuth refresh-token rotatie: hoe vangt de cronjob het op als een token verloopt of revoke'd is?

## Next Steps

→ `/hp:plan` voor de concrete implementatieplanning.
