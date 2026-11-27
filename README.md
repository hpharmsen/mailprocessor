# mailprocessor

Een uurlijkse cron-job die Gmail leest, attachments van vooraf-bekende afzenders
opslaat op disk volgens regels uit `tasks.md`, een justai-agent inzet voor
rule-matching + placeholder-substitutie, en Gmail-labels gebruikt als enige
persistente state.

Alle Gmail-toegang loopt via Google's officiële Gmail MCP-server
(`gmailmcp.googleapis.com/mcp/v1self`). Sinds 1 februari 2027 moeten AI-agents
op Workspace-data een Google MCP gebruiken (Workspace AI Tools Addendum). De
enige uitzondering is `src/gmail_rest.py`: de MCP-server kan geen bijlagen
downloaden, dus bijlage-bytes en de `Message-ID` komen via de REST API, buiten de
agent om.

## Setup

### 1. Google Cloud (eenmalig)

Zie [docs/solutions/integrations/gmail-mcp-setup.md](docs/solutions/integrations/gmail-mcp-setup.md).
Kort:

- `gcloud services enable gmail.googleapis.com gmailmcp.googleapis.com --project=mailprocessor-499408`
- Twee Desktop OAuth-clients in de Console, in deze directory:
  - `credentials_mcp.json` **met** "Use this client for an AI-powered agent".
    Zonder die vlag weigert de MCP-server het token (`invalid_token`).
  - `credentials.json` zonder die vlag, voor `gmail_rest.py`.
- Admin console (admin.google.com) > Agents > Instellingen > Gebruikersbeoordeling
  voor externe agents: **Toegang tot externe gegevens** op **Toestaan**. Anders
  ziet de MCP-server geen mail van externe afzenders.

### 2. Local install

```bash
cd /Users/hp/proj/mailprocessor
uv sync
mkdir -p ~/Library/Logs/mailprocessor
```

`.env` moet minstens bevatten:

- `EMAILME_TOKEN`: shared secret voor de emailme-endpoint. Daarmee gaan zowel
  de bevestigingen na een opgeslagen factuur als de foutmeldingen naar Janitor.

Overige env-vars (bv. `ODIDO_USER` / `ODIDO_PASSWORD`) zijn alleen nodig voor
regels die die credentials via `browser_fill_credential` gebruiken.

### 3. OAuth bootstrap

```bash
uv run src/main.py setup-auth
```

Opent twee keer een browser, één keer per OAuth-client. `token_mcp.json` en
`token.json` worden weggeschreven met mode 0600.

### 4. Handmatig testen

```bash
uv run src/main.py
```

### 5. Schedulen via `cron` (HP's eigen scheduler)

```bash
cron add "0 * * * *" "cd /Users/hp/proj/mailprocessor && uv run src/main.py"
cron list                          # noteer ID
cron rename <id> Mailprocessor
cron run <id>                      # forceer een directe run
cron log <id>                      # bekijk laatste output
```

## Nieuwe regel toevoegen

Bewerk `tasks.md`. De agent leest de markdown bij elke run -- geen code-wijziging
nodig. Wel: nieuwe afzenders/paden moeten binnen `ALLOWED_ROOTS` in `disk.py` vallen.
Toevoeging van een nieuwe root is een bewuste code-wijziging (de blast-radius mag
niet runtime-configureerbaar zijn).

## Foutmeldingen

Alles op ERROR-niveau gaat via justlog's `JanitorWebhookHandler` naar Janitor
(`errors+mailprocessor@harmsen.nl`). Er is geen aparte SMTP-notify meer.

`setup_logging(..., capture=('justai',))` is nodig omdat justai naar zijn eigen
logger schrijft, die naar de root propageert in plaats van naar de onze. Zonder
die regel bereikt een mislukte LLM-call het logbestand noch Janitor.

Janitor dedupliceert op fingerprint = (project, logger, exception-klasse,
bovenste stackframe). Zonder exception valt die sleutel sinds justlog 0.8.9
terug op de logregel zelf, dus je krijgt hoe dan ook bruikbare deduplicatie.
Geef `exc_info` toch mee waar je een exception in handen hebt:

```python
lg.error('gmail query failed', mail_id=mid, exc_info=exc)
# subject:  [ERROR] mailprocessor: TimeoutError in gmail_mcp.py:query_pending
# body:     de melding, de kwargs en de volledige traceback

lg.error('gmail query failed', mail_id=mid, error=str(exc))
# subject:  [ERROR] mailprocessor: gmail query failed
# body:     de melding en de kwargs, geen traceback
```

Het verschil zit dus niet meer in botsende fingerprints maar in wat je in de
alert ziet: zonder `exc_info` geen traceback en geen exception-klasse in het
subject. Twee verschillende bugs die toevallig dezelfde logregel gebruiken
vallen dan bovendien samen, terwijl `exc_info` ze uit elkaar houdt op de plek
waar ze ontstaan.

Versie-eisen: `exc_info` op de LogRecord vereist justlog >= 0.8.6, de terugval
op de logregel >= 0.8.9, en `capture=` eveneens >= 0.8.9.

MCP- en REST-calls lopen via `retry()` in `gmail_mcp.py` en retryen transient
falen (HTTP-fouten, timeouts, connection resets) 3x met oplopende backoff. Elke
retry is een WARNING; pas als de laatste poging faalt propageert de exception
en logt de caller één ERROR. Een netwerkblip levert dus geen Janitor-alert op.

## Labels

| Label | Betekenis | Opnieuw verwerken |
| --- | --- | --- |
| `mailprocessor/done` | opgeslagen, bevestiging verstuurd | n.v.t. |
| `mailprocessor/failed` | regel past, uitvoering mislukt (Janitor-alert) | label verwijderen |
| `mailprocessor/skipped` | geen regel in `tasks.md` past | label verwijderen |

## Troubleshooting

- **Token expired / RefreshError / invalid_token** -> Janitor-alert komt binnen. Run `uv run src/main.py setup-auth`.
- **MCP-server direct bevragen** -> `uv run scripts/mcp_probe.py tools`, `... call <tool> '<json>'`
  of `... attachments <message_id>` (downloadt de bijlagen zoals mailprocessor dat doet en toont grootte en sha256).
- **Zoeken geeft `omittedContent: DATA_PROTECTION_POLICY` of "requires user confirmation"** ->
  de admin-instelling "Toegang tot externe gegevens" staat niet op Toestaan (zie Setup).
- **Mail met failed-label** -> check `~/Library/Logs/mailprocessor/app.log`, fix
  oorzaak, verwijder het `mailprocessor/failed`-label in Gmail, en kickstart de job.
- **Cron job lijkt niet te lopen** -> `cron show <id>` toont last_run + status; `cron log <id>` toont laatste output.

## Tests

```bash
uv run pytest
```

## Architectuur

Zie [docs/architecture.md](docs/architecture.md).
