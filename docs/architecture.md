# Architectuur

## Overzicht

Uurlijkse cron-job die Gmail leest, attachments van vooraf-bekende afzenders
opslaat op disk volgens regels uit `tasks.md`, een justai-agent inzet voor
rule-matching + placeholder-substitutie, en Gmail-labels gebruikt als enige
persistente state.

## Modulaire opbouw

```
mailprocessor/
├── pyproject.toml                  # workspace member; deps incl. justai/justlog (lokale paths)
├── tasks.md                        # door HP onderhouden regels (input, gitignored)
├── tasks.md.example                # gesanitiseerd voorbeeld voor de repo
├── .env                            # NOTIFY_TO + secrets (gitignored)
├── src/
│   ├── main.py                     # entry point + SMTP-notify + setup-auth subcommand
│   ├── task_parser.py              # extract_senders(content) -> set[str]
│   ├── gmail_client.py             # OAuth + Gmail-operaties + MailContext
│   ├── disk.py                     # save_attachment_bytes met ALLOWED_ROOTS-guard
│   ├── browser_tools.py            # playwright-tools voor browser-flows
│   └── agent_runner.py             # justai-agent + tool-closures per mail
└── tests/
    ├── conftest.py                 # sys.path + NOTIFY_TO + justlog setup
    ├── test_task_parser.py
    ├── test_disk.py
    ├── test_gmail_client.py
    ├── test_agent_runner.py
    └── fixtures/
```

## Runtime flow

```
cron (hourly)
   |-> main.run()
        |-> load_credentials() (auto-refresh; chmod 600 na write)
        |-> GmailClient.ensure_labels()
        |-> task_parser.extract_senders(tasks.md)  --> senders set
        |-> GmailClient.query_unread(senders)      --> [mid, ...]
        |   (is:unread from:(senders) -label:done -label:failed)
        |-> per mid:
        |     |-> GmailClient.get_message(mid)     --> MailContext
        |     |   (parseaddr + CRLF guard op From-header)
        |     |-> agent_runner.process_mail(...)
        |           |-> Agent(model, tools=[apply_rule_closure, report_no_action])
        |           |-> agent kiest 1 tool
        |                |-> apply_rule:
        |                |     - per attachment: download + disk.save_attachment_bytes
        |                |     - GmailClient.send_reply(To=hp@harmsen.nl, threadId behouden)
        |                |     - GmailClient.mark_read
        |                |     - GmailClient.add_label(done)
        |                |     bij fout: failed-label + SMTP-notify (eigen kanaal)
        |                `-> report_no_action: niets, mail blijft ongelezen
```

## Designprincipes

- **Deterministisch waar mogelijk.** Alleen rule-matching + placeholder-extractie
  raken het LLM. Gmail-API-calls + filesystem zijn vaste Python.
- **Een agent-call per mail.** De agent krijgt `tasks.md` + mail-context in een
  prompt-tempfile, heeft 2 tools, beslist eenmaal, klaar. Geen multi-turn loop.
- **Combined-action tool.** `apply_rule` doet save -> reply -> mark_read ->
  done-label in vaste volgorde. Faalt bij eerste error met failed-label +
  notify, returnt status-string aan de agent.
- **Labels-as-state.** Geen lokale DB. `mailprocessor/done` en
  `mailprocessor/failed` zijn de enige persistente state.
- **Closures voor tool-binding.** `make_apply_rule(gmail, mail_ctx, notifier)`
  retourneert een `apply_rule(...)` waarvan de naam/docstring/type-hints door
  justai geinspecteerd worden. De `list[dict]`-shape voor `attachments` staat
  letterlijk in de prompt omdat justai's schema-builder daar geen item-shape
  voor genereert.
- **Sync cronjob, async agent.** Top-level `asyncio.run(main())`. Geen extra
  abstractielagen.

## Security-keuzes

| Risico                                                   | Mitigatie                                                                 |
| -------------------------------------------------------- | ------------------------------------------------------------------------- |
| LLM-injectie probeert `/etc/passwd` als target           | `disk.ALLOWED_ROOTS` whitelist, `is_relative_to` na `.resolve()`           |
| Re-delivery van zelfde rapport (zelfde bytes)            | Content-hash check -> `identical-already-present`, geen false-failure     |
| Overwrite van bestaand bestand met verschillende inhoud  | `TargetExistsError` -> failed-label + notify                              |
| CRLF-injectie in `From`-header (BCC-spoofing)            | `parseaddr` + expliciete `\r\n`-check in `get_message`                    |
| Auto-reply naar externe afzender                         | `send_reply` gebruikt `NOTIFY_TO` (env-var, geladen uit `.env`)            |
| Token-leak via wereld-leesbare `token.json`              | `chmod 600` na elke `write` in `load_credentials` + `run_oauth_flow`      |
| Secrets in repo                                          | `.gitignore` sluit `.env`, `credentials.json`, `token.json` uit            |
| Logs bevatten subject-lines                              | Logs in `~/Library/Logs/mailprocessor/`, niet in repo                     |

## Failure propagation

- **save/reply/mark_read/add_label fail** -> `_fail` in agent_runner:
  failed-label + SMTP-notify. `apply_rule` returnt `'failed: <step>: <reason>'`.
  Geen exception lekt naar de agent loop.
- **Onbekende exception in process_mail** -> main.py try/except: log +
  SMTP-notify, volgende mail.
- **OAuth-refresh fails** -> SMTP-notify ("run setup-auth") + exit 1.
- **Gmail-query fails** (network down) -> SMTP-notify + exit 1. Volgende cron-tick
  probeert opnieuw.
- **SMTP-notifier zelf faalt** -> alleen gelogd; cron log vangt op.

## State-transitions

Per mail, vanuit Gmail-server-perspectief:

| Begin              | Eind bij done                                    | Eind bij failed                                  | Eind bij no-action  |
| ------------------ | ------------------------------------------------ | ------------------------------------------------ | ------------------- |
| `UNREAD`, geen lbl | `READ` + `mailprocessor/done`                    | `UNREAD` + `mailprocessor/failed`                | `UNREAD`, geen lbl  |

De query filtert op `-label:done -label:failed`, dus een mail kan niet
dubbel verwerkt worden zolang de labels intact blijven. Een failed-mail
opnieuw aanbieden: verwijder het failed-label handmatig en kickstart.

## Externe afhankelijkheden

- **`justai`** (lokaal `/Users/hp/proj/justai`) -- `Agent` class voor de
  per-mail run.
- **`justlog`** (lokaal `/Users/hp/proj/justlog`) -- structured logging met
  rotation. `lg.info/error/warning` ondersteunen kwargs; `lg.exception`
  niet -- gebruik `lg.error(..., exc_info=True)`.
- **`google-api-python-client`** + **`google-auth-*`** -- Gmail API + OAuth.
- **cron** (HP's eigen scheduler) -- uurlijks: `0 * * * *`.

## Wat is bewust *niet* gebouwd

- Geen lokale DB / state-file (labels doen het werk).
- Geen retry-loop binnen apply_rule (failed-label + handmatige inspectie).
- Geen aparte notifier-module (1 functie in main.py + `SimpleNamespace`-wrapper).
- Geen aparte setup_auth.py (subcommand van `main.py`).
- Geen runtime-config voor `ALLOWED_ROOTS` (blast-radius is code-niveau).
