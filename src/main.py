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

from justlog import lg, setup_logging  # noqa: E402
from justlog.handlers import JanitorWebhookHandler  # noqa: E402

# OAuth client with the "AI-powered agent" flag; the Gmail MCP server rejects others.
TOKEN_PATH = ROOT / 'token_mcp.json'
CREDENTIALS_PATH = ROOT / 'credentials_mcp.json'
# Plain OAuth client for gmail_rest (attachment bytes + Message-ID only).
REST_TOKEN_PATH = ROOT / 'token.json'
REST_CREDENTIALS_PATH = ROOT / 'credentials.json'
TASKS_MD_PATH = ROOT / 'tasks.md'
LOG_PATH = Path('/Users/hp/Library/Logs/mailprocessor/app.log')

# Logging staat vóór de projectimports: een kapotte dependency (bv. justai
# als editable path-dep die een nieuwe package nodig heeft) crasht anders al
# bij het importeren, buiten het logbestand en Janitor om.
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
    from google.auth.exceptions import RefreshError, TransportError

    import agent_runner
    import gmail_mcp
    import task_parser
    from gmail_mcp import GMAIL_DEEPLINK, GmailMcp, InvalidSenderError
    from gmail_rest import GmailRest
except Exception as e:
    lg.error(
        'import failed; venv out of sync?\n'
        f'Actie: cd {ROOT} && uv sync\n',
        exc_info=e,
    )
    sys.exit(1)


async def run() -> int:
    try:
        creds = gmail_mcp.load_credentials(TOKEN_PATH)
        rest_creds = gmail_mcp.load_credentials(REST_TOKEN_PATH)
    except TransportError:
        # load_credentials already alerted Janitor after its final retry.
        lg.warning('run aborted: oauth refresh unavailable')
        return 1
    except (gmail_mcp.NoValidTokenError, RefreshError) as e:
        lg.error(
            'oauth bootstrap required\n'
            f'Actie: cd {ROOT} && uv run src/main.py setup-auth\n',
            exc_info=e,
        )
        return 1

    tasks_md = TASKS_MD_PATH.read_text()
    senders = task_parser.extract_senders(tasks_md)
    if not senders:
        lg.warning('no senders in tasks.md; nothing to do')
        return 0

    gmail = GmailMcp(creds, rest=GmailRest(rest_creds))
    try:
        return await _process_pending(gmail, senders, tasks_md)
    finally:
        gmail.close()


async def _process_pending(gmail: GmailMcp, senders: set[str], tasks_md: str) -> int:
    try:
        gmail.ensure_labels()
        mids = gmail.query_pending(senders)
    except Exception as e:
        # The first MCP call also opens the session, so a rejected token lands here.
        lg.error(
            'gmail mcp query failed\n'
            f'Actie bij invalid_token: cd {ROOT} && uv run src/main.py setup-auth\n',
            exc_info=e,
        )
        return 1

    if not mids:
        lg.info('no new mails', senders=len(senders))
        return 0

    lg.info('processing mails', count=len(mids), senders=len(senders))

    for mid in mids:
        try:
            mail_ctx = gmail.get_message(mid)
        except InvalidSenderError as e:
            # Verwachte client-side uitkomst, geen bug: een malformed From-header
            # komt van de afzender. WARNING, dus geen Janitor-alert.
            lg.warning('skipping mail with malformed sender', mail_id=mid, sender=str(e))
            continue
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
    """OAuth for both clients: the agent client (MCP) and the plain client (gmail_rest)."""
    for credentials, token, kind in (
        (CREDENTIALS_PATH, TOKEN_PATH, 'with "Use this client for an AI-powered agent"'),
        (REST_CREDENTIALS_PATH, REST_TOKEN_PATH, 'without the agent flag'),
    ):
        if not credentials.exists():
            print(f'ERROR: {credentials} missing.')
            print(f'Create a Desktop OAuth client {kind} in Google Cloud Console and save its JSON')
            print('there (docs/solutions/integrations/gmail-mcp-setup.md).')
            return 1
        print(f'Opening browser for OAuth consent ({credentials.name})...')
        gmail_mcp.run_oauth_flow(credentials, token)
        print(f'Token written to {token} (chmod 600).')
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == 'setup-auth':
        return setup_auth()
    return asyncio.run(run())


if __name__ == '__main__':
    sys.exit(main())
