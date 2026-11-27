# Architectuur

## Overzicht

Uurlijkse cron-job die Gmail leest, attachments van vooraf bekende afzenders
opslaat op disk volgens regels uit `tasks.md`, een justai-agent inzet voor
rule-matching en placeholder-substitutie, en Gmail-labels gebruikt als enige
persistente state.

Alle Gmail-toegang loopt via Google's Gmail MCP-server
(`https://gmailmcp.googleapis.com/mcp/v1self`). Het Workspace AI Tools Addendum
eist dat sinds 1 februari 2027 voor tools die door een AI-agent worden
aangestuurd. Mailprocessor is zelf de MCP-client; de agent ziet geen MCP-tools,
alleen de afgeschermde tools hieronder.

## Modulaire opbouw

```
mailprocessor/
├── pyproject.toml          # deps: justai (lokaal), justlog, mcp, google-auth-oauthlib, pypdf, playwright
├── tasks.md                # door HP onderhouden regels (input, gitignored)
├── tasks.md.example        # gesanitiseerd voorbeeld
├── .env                    # EMAILME_TOKEN + browser-credentials (gitignored)
├── credentials_mcp.json    # Desktop OAuth-client met agent-vlag (gitignored)
├── token_mcp.json          # OAuth-token, chmod 600 (gitignored)
├── scripts/
│   └── mcp_probe.py        # diagnose: ruwe MCP-responses, bijlagen uit RAW
├── src/
│   ├── main.py             # entry point + setup-auth subcommand
│   ├── task_parser.py      # extract_senders(content) -> set[str]
│   ├── gmail_mcp.py        # OAuth, MCP-sessie in worker-thread, MIME-parsing, MailContext
│   ├── disk.py             # save_attachment_bytes met ALLOWED_ROOTS-guard
│   ├── browser_tools.py    # playwright-tools voor browser-flows
│   └── agent_runner.py     # justai-agent + tool-closures per mail, emailme-bevestiging
└── tests/
```

## Runtime flow

```
cron (hourly)
  └─ main.run()
       ├─ gmail_mcp.load_credentials(token_mcp.json)   (refresh + chmod 600)
       ├─ task_parser.extract_senders(tasks.md)        -> senders
       ├─ GmailMcp(creds)                              (MCP-sessie in eigen thread)
       │    ├─ ensure_labels()                         list_labels / create_label
       │    └─ query_pending(senders)                  search_threads, per bericht gefilterd
       ├─ per message-id:
       │    ├─ get_message(mid)                        get_message RAW -> lokaal geparsed
       │    │                                          (From-guard, Message-ID, bijlagen per partId)
       │    └─ agent_runner.process_mail(...)
       │         └─ Agent(tools=[read_pdf_*, apply_rule, apply_downloaded,
       │                         report_no_action, report_failure, browser_*, wait_for_email])
       │              ├─ apply_rule / apply_downloaded:
       │              │     save -> emailme-bevestiging (In-Reply-To) -> UNREAD eraf -> done-label
       │              ├─ report_no_action: skipped-label
       │              └─ report_failure / fout in een stap: failed-label + Janitor
       └─ GmailMcp.close()                             (ook bij fouten)
```

## Gmail via MCP

- **Endpoint `/mcp/v1self`.** `/mcp/v1` vraagt bij gevoelige acties goedkeuring
  via MCP elicitation, wat in een onbeheerde cron niet kan. `/mcp/v1self` heeft
  alle lees- en labeltools maar geen `reply`, `send_message` of `forward`.
- **Synchrone brug.** De `mcp` SDK is async en justai roept tools synchroon aan
  binnen een draaiende loop. `GmailMcp` draait de sessie daarom in een eigen
  thread met een eigen event loop en een queue, naar het patroon van
  `BrowserSession`. Eén sessie per run.
- **Eén RAW-fetch per mail.** `get_message` in `RAW` levert het volledige
  MIME-bericht. Python parset dat lokaal (stdlib `email`) en haalt er de echte
  headers, de body (HTML wordt tekst plus linklijst) en de bijlagen uit. Bijlagen
  worden genummerd zoals Gmail's partId (`0`, `1`, `1.0`, ...). Het resultaat
  wordt per run gecachet, dus `read_pdf_excerpt` en `apply_rule` kosten geen
  extra call. De SSE-eventlimiet van de SDK (1 MB) staat uit, anders past een
  factuur-PDF niet.
- **Zoeken per bericht.** `search_threads` geeft hele threads terug (maximaal 5
  berichten; bij meer volgt `get_thread`). `query_pending` filtert daarom lokaal:
  INBOX aanwezig, geen eigen label, afzender in `tasks.md`.
- **Retry.** Transient falen (de SDK maakt van elke HTTP-fout `INTERNAL_ERROR`,
  plus netwerkfouten) krijgt 3 pogingen met een WARNING per retry. Een tool-fout
  van de server (`is_error`) wordt niet herhaald.

## Designprincipes

- **Deterministisch waar mogelijk.** Alleen rule-matching en placeholder-extractie
  raken het LLM. MCP-calls, MIME-parsing en filesystem zijn vaste Python.
- **Combined-action tool.** `apply_rule` doet save, bevestiging, mark_read en
  done-label in vaste volgorde. Faalt bij de eerste fout met failed-label en
  Janitor-alert, en geeft een statusregel terug aan de agent.
- **Labels-as-state.** Geen lokale DB. `mailprocessor/done`, `/failed` en
  `/skipped` zijn de enige persistente state.
- **Closures voor tool-binding.** `make_apply_rule(gmail, mail_ctx)` geeft een
  `apply_rule(...)` terug waarvan naam, docstring en type-hints door justai worden
  geïnspecteerd. De `list[dict]`-shape voor `attachments` staat letterlijk in de
  prompt, omdat justai's schema-builder daar geen item-shape voor maakt.

## Security-keuzes

| Risico | Mitigatie |
| --- | --- |
| Agent krijgt schrijfrechten op Gmail | Geen ruwe MCP-tools naar de agent; alleen afgeschermde closures |
| Bijlage-bytes in de LLM-context | Bytes gaan van RAW direct naar `disk`; tools geven alleen namen en statusregels terug |
| LLM-injectie probeert `/etc/passwd` als target | `disk.ALLOWED_ROOTS` whitelist, `is_relative_to` na `.resolve()` |
| Re-delivery van zelfde rapport | Content-hash check -> `identical-already-present` |
| Overwrite met andere inhoud | `TargetExistsError` -> failed-label + Janitor |
| CRLF in `From` (encoded-word) | stdlib weigert CR/LF in adresdelen -> `InvalidSenderError`, mail overgeslagen |
| CRLF in Subject of Message-ID naar uitgaande headers | Subject wordt genormaliseerd; Message-ID alleen als één `<...>`-token |
| Bevestiging naar externe afzender | emailme stuurt altijd naar hp@harmsen.nl |
| MCP-server weigert gewone OAuth-clients | Aparte Desktop-client met agent-vlag (`credentials_mcp.json`) |
| Token-leak | `chmod 600` na elke write |
| Secrets in repo | `.gitignore` sluit `.env`, `credentials*.json`, `token*.json` uit |

## Failure propagation

- **save/confirm/mark_read/add_done faalt** -> `_fail` in agent_runner:
  failed-label + Janitor. `apply_rule` geeft `'failed: <step>: <reason>'` terug.
- **Agent meldt `report_failure`** -> zelfde route als hierboven.
- **Onbekende exception in process_mail** -> main.py logt ERROR, volgende mail.
- **OAuth-refresh faalt transient** -> WARNING per retry, ERROR na de laatste.
- **Geen geldig token** -> ERROR met "run setup-auth", exit 1.
- **Eerste MCP-call faalt** (server onbereikbaar of `invalid_token`) -> ERROR
  met setup-auth-hint, exit 1. Volgende cron-tick probeert opnieuw.

## State-transitions

| Begin | done | failed | no-action |
| --- | --- | --- | --- |
| inbox, geen eigen label | gelezen + `mailprocessor/done` | `mailprocessor/failed` | `mailprocessor/skipped` |

De query sluit alle drie de labels uit. Opnieuw verwerken: label verwijderen.

## Externe afhankelijkheden

- **Gmail MCP-server** (`gmailmcp.googleapis.com`), Standard-quota (5.000
  requests per dag; `search_threads` telt als 10).
- **`mcp`** (Python SDK, gepind op 2.3.0): Streamable HTTP-client.
- **`google-auth-oauthlib`**: OAuth-flow en token-refresh.
- **emailme** (`https://www.harmsen.nl/emailme/`, altijd met www): bevestigingen
  en Janitor-alerts, met `X-Emailme-Token`.
- **`justai`** (lokaal `/Users/hp/proj/justai`): `Agent` per mail.
- **`justlog`**: logging met rotation en `JanitorWebhookHandler`.
- **cron** (HP's eigen scheduler): `0 * * * *`.

## Wat is bewust *niet* gebouwd

- Geen lokale DB of state-file (labels doen het werk).
- Geen retry binnen apply_rule (failed-label + handmatige inspectie).
- Geen MCP-ondersteuning in justai; een eigen kleine client in `gmail_mcp.py`.
- Geen runtime-config voor `ALLOWED_ROOTS` (blast-radius is code-niveau).
