# mailprocessor

Een uurlijkse cron-job die Gmail leest, attachments van vooraf-bekende afzenders
opslaat op disk volgens regels uit `tasks.md`, een justai-agent inzet voor
rule-matching + placeholder-substitutie, en Gmail-labels gebruikt als enige
persistente state.

## Setup

### 1. Google Cloud Console (eenmalig)

1. Cloud Console > nieuw project "mailprocessor" > enable Gmail API.
2. OAuth consent screen > User type **External** > add `hp@harmsen.nl` als test user.
3. **Publishing > Publish app**. Status moet "In production" worden,
   anders verloopt de refresh-token na 7 dagen en sterft de cron stil.
4. Credentials > Create OAuth client > type **Desktop app** > download als
   `credentials.json` in deze directory.

### 2. Local install

```bash
cd /Users/hp/proj/research/mailprocessor
uv sync
mkdir -p ~/Library/Logs/mailprocessor
```

`.env` moet minstens bevatten:

- `NOTIFY_TO` — e-mailadres waar alle replies naartoe gaan.
- `EMAILME_TOKEN` — shared secret voor de emailme-endpoint. Zonder dit token
  bereiken foutmeldingen Janitor niet en klaagt de run op stderr.

Overige env-vars (bv. `ODIDO_USER` / `ODIDO_PASSWORD`) zijn alleen nodig voor
regels die die credentials via `browser_fill_credential` gebruiken.

### 3. OAuth bootstrap

```bash
uv run src/main.py setup-auth
```

Opent een browser. Accepteer de "unverified app" warning. `token.json` wordt
weggeschreven met mode 0600.

### 4. Handmatig testen

```bash
uv run src/main.py
```

### 5. Schedulen via `cron` (HP's eigen scheduler)

```bash
cron add "0 * * * *" "/Users/hp/proj/research/.venv/bin/python /Users/hp/proj/research/mailprocessor/src/main.py"
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
# subject:  [ERROR] mailprocessor: TimeoutError in gmail_client.py:query_pending
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

Gmail-calls lopen via `_execute()` in `gmail_client.py` en retryen transient
falen (429, 5xx, timeouts, connection resets, DNS-hikken) 3x met oplopende
backoff. Elke retry is een WARNING; pas als de laatste poging faalt propageert
de exception en logt de caller één ERROR. Een netwerkblip levert dus geen
Janitor-alert meer op.

## Troubleshooting

- **Token expired / RefreshError** -> Janitor-alert komt binnen. Run `uv run src/main.py setup-auth`.
- **Mail met failed-label** -> check `~/Library/Logs/mailprocessor/app.log`, fix
  oorzaak, verwijder het `mailprocessor/failed`-label in Gmail, en kickstart de job.
- **Cron job lijkt niet te lopen** -> `cron show <id>` toont last_run + status; `cron log <id>` toont laatste output.

## Tests

```bash
uv run pytest
```

## Architectuur

Zie [docs/architecture.md](docs/architecture.md).
