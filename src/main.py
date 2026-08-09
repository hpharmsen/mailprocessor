'''mailprocessor entry point.

Two modes:
  uv run src/main.py             # the cron run
  uv run src/main.py setup-auth  # interactive OAuth bootstrap
'''
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')

from google.auth.exceptions import RefreshError, TransportError  # noqa: E402
from justlog import lg, setup_logging  # noqa: E402
from justlog.handlers import JanitorWebhookHandler  # noqa: E402

import agent_runner  # noqa: E402
import task_parser  # noqa: E402
from gmail_client import (  # noqa: E402
    GmailClient, NoValidTokenError, load_credentials, run_oauth_flow, GMAIL_DEEPLINK,
)

TOKEN_PATH = ROOT / 'token.json'
CREDENTIALS_PATH = ROOT / 'credentials.json'
TASKS_MD_PATH = ROOT / 'tasks.md'
LOG_PATH = Path('/Users/hp/Library/Logs/mailprocessor/app.log')


async def run() -> int:
    # capture: justai logs to its own logger, which propagates to root instead of
    # to ours. Without it, an LLM failure never reaches the log file or Janitor.
    setup_logging(
        str(LOG_PATH), max_bytes=1_000_000, backup_count=5, capture=('justai',),
    )

    # Every lg.error below is an alert: JanitorWebhookHandler ships it to
    # Janitor, which dedups on the exception fingerprint and mails the rest.
    if not os.getenv('EMAILME_TOKEN'):
        print('ERROR: EMAILME_TOKEN not set; errors will not reach Janitor.',
              file=sys.stderr)
    lg.addHandler(JanitorWebhookHandler(project='mailprocessor'))

    try:
        creds = load_credentials(TOKEN_PATH, CREDENTIALS_PATH)
    except TransportError:
        # load_credentials already alerted Janitor after its final retry.
        lg.warning('run aborted: oauth refresh unavailable')
        return 1
    except (NoValidTokenError, RefreshError) as e:
        lg.error(
            'oauth bootstrap required\n'
            f'Actie: cd {ROOT} && uv run src/main.py setup-auth\n',
            exc_info=e,
        )
        return 1

    gmail = GmailClient(creds)
    gmail.ensure_labels()

    tasks_md = TASKS_MD_PATH.read_text()
    senders = task_parser.extract_senders(tasks_md)
    if not senders:
        lg.warning('no senders in tasks.md; nothing to do')
        return 0

    try:
        mids = gmail.query_pending(senders)
    except Exception as e:
        lg.error('gmail query failed', exc_info=e)
        return 1

    if not mids:
        lg.info('no new mails', senders=len(senders))
        return 0

    lg.info('processing mails', count=len(mids), senders=len(senders))

    for mid in mids:
        try:
            mail_ctx = gmail.get_message(mid)
        except Exception as e:
            lg.error(f'get_message failed\nMail-id: {mid}\n', exc_info=e)
            continue
        try:
            outcome = await agent_runner.process_mail(gmail, mail_ctx, tasks_md)
            lg.info(
                'processed',
                sender=mail_ctx.sender_address,
                subject=mail_ctx.subject,
                outcome=outcome,
            )
        except Exception as e:
            lg.error(
                f'crash in process_mail\n'
                f'Mail:    {mail_ctx.subject}\n'
                f'Van:     {mail_ctx.sender_address}\n'
                f'Gmail:   {GMAIL_DEEPLINK}{mail_ctx.id}\n',
                exc_info=e,
            )
    return 0


def setup_auth() -> int:
    if not CREDENTIALS_PATH.exists():
        print(f'ERROR: {CREDENTIALS_PATH} missing.')
        print('Download the OAuth Desktop client from Google Cloud Console first.')
        return 1
    print('Opening browser for OAuth consent...')
    run_oauth_flow(CREDENTIALS_PATH, TOKEN_PATH)
    print(f'Token written to {TOKEN_PATH} (chmod 600).')
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == 'setup-auth':
        return setup_auth()
    return asyncio.run(run())


if __name__ == '__main__':
    sys.exit(main())
