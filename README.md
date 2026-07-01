# mailprocessor

Een uurlijkse launchd-job die Gmail leest, attachments van vooraf-bekende afzenders
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

`.env` moet `EMAIL_PASSWORD_HP` bevatten voor de SMTP-fallback (failure notifications).

### 3. OAuth bootstrap

```bash
uv run main.py setup-auth
```

Opent een browser. Accepteer de "unverified app" warning. `token.json` wordt
weggeschreven met mode 0600.

### 4. Handmatig testen

```bash
uv run main.py
```

### 5. Schedulen via `cron` (HP's eigen scheduler)

```bash
cron add "0 * * * *" "/Users/hp/proj/research/.venv/bin/python /Users/hp/proj/research/mailprocessor/main.py"
cron list                          # noteer ID
cron rename <id> Mailprocessor
cron run <id>                      # forceer een directe run
cron log <id>                      # bekijk laatste output
```

#### Alternatief: launchd

Als je de standalone launchd-route prefereert (geen afhankelijkheid van `cron`):

```bash
cp launchd/com.harmsen.mailprocessor.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.harmsen.mailprocessor.plist
launchctl print gui/$(id -u)/com.harmsen.mailprocessor      # verifieer
launchctl kickstart -k gui/$(id -u)/com.harmsen.mailprocessor  # forceer een run
```

## Nieuwe regel toevoegen

Bewerk `tasks.md`. De agent leest de markdown bij elke run -- geen code-wijziging
nodig. Wel: nieuwe afzenders/paden moeten binnen `ALLOWED_ROOTS` in `disk.py` vallen.
Toevoeging van een nieuwe root is een bewuste code-wijziging (de blast-radius mag
niet runtime-configureerbaar zijn).

## Troubleshooting

- **Token expired / RefreshError** -> SMTP-notify komt binnen. Run `uv run main.py setup-auth`.
- **Mail met failed-label** -> check `~/Library/Logs/mailprocessor/app.log`, fix
  oorzaak, verwijder het `mailprocessor/failed`-label in Gmail, en kickstart de job.
- **Cron job lijkt niet te lopen** -> `cron show <id>` toont last_run + status; `cron log <id>` toont laatste output.
- **launchd job lijkt niet te lopen** -> `launchctl print gui/$(id -u)/com.harmsen.mailprocessor`
  toont laatste exit code + last-run-time.

## Tests

```bash
uv run pytest
```

## Architectuur

Zie [docs/architecture.md](docs/architecture.md).
