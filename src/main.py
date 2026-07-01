'''mailprocessor entry point.

Two modes:
  uv run src/main.py             # the cron run
  uv run src/main.py setup-auth  # interactive OAuth bootstrap
'''
import asyncio
import os
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')

from google.auth.exceptions import RefreshError  # noqa: E402
from justlog import lg, setup_logging  # noqa: E402

import agent_runner  # noqa: E402
import task_parser  # noqa: E402
from gmail_client import (  # noqa: E402
    GmailClient, NoValidTokenError, load_credentials, run_oauth_flow, NOTIFY_TO,
)

TOKEN_PATH = ROOT / 'token.json'
CREDENTIALS_PATH = ROOT / 'credentials.json'
TASKS_MD_PATH = ROOT / 'tasks.md'
LOG_PATH = Path('/Users/hp/Library/Logs/mailprocessor/app.log')


def email_error(subject: str, body: str) -> None:
    '''Send a notification via Gmail SMTP (independent of the Gmail API).'''
    password = os.getenv('EMAIL_PASSWORD_HP')
    if not password:
        lg.error('EMAIL_PASSWORD_HP not set; cannot send notification', subject=subject)
        return
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = NOTIFY_TO
    msg['To'] = NOTIFY_TO
    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as srv:
            srv.starttls()
            srv.login(NOTIFY_TO, password)
            srv.send_message(msg)
    except Exception as e:
        lg.error('smtp send failed', error=str(e), subject=subject)


async def run() -> int:
    setup_logging(str(LOG_PATH), max_bytes=1_000_000, backup_count=5)

    try:
        creds = load_credentials(TOKEN_PATH, CREDENTIALS_PATH)
    except (NoValidTokenError, RefreshError) as e:
        email_error(
            'mailprocessor: OAuth needs re-bootstrap',
            f'Run: cd {ROOT} && uv run src/main.py setup-auth\n\nDetail: {e}\n',
        )
        lg.error('oauth bootstrap required', error=str(e))
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
        lg.error('gmail query failed', error=str(e), exc_info=True)
        email_error('mailprocessor: gmail query failed', f'{type(e).__name__}: {e}\n')
        return 1

    if not mids:
        lg.info('no new mails', senders=len(senders))
        return 0

    lg.info('processing mails', count=len(mids), senders=len(senders))

    notifier = SimpleNamespace(email_error=email_error)
    for mid in mids:
        try:
            mail_ctx = gmail.get_message(mid)
        except Exception as e:
            lg.error('get_message failed', mail_id=mid, error=str(e), exc_info=True)
            continue
        try:
            outcome = await agent_runner.process_mail(
                gmail, mail_ctx, tasks_md, notifier,
            )
            lg.info(
                'processed',
                sender=mail_ctx.sender_address,
                subject=mail_ctx.subject,
                outcome=outcome,
            )
        except Exception as e:
            lg.error(
                'unhandled in process_mail',
                mail_id=mid, error=str(e), exc_info=True,
            )
            email_error(
                f'mailprocessor crash on {mail_ctx.subject}',
                f'From: {mail_ctx.sender_address}\nError: {type(e).__name__}: {e}\n',
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
