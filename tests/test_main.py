import asyncio
import logging
import sys
from pathlib import Path

import justlog
import justlog.handlers
import pytest

SRC = Path(__file__).parent.parent / 'src'


@pytest.fixture
def main(monkeypatch, tmp_path):
    """Import main without the real log file or Janitor handler (lg.error would mail Janitor)."""
    monkeypatch.setattr(justlog, 'setup_logging', lambda *a, **k: None)
    monkeypatch.setattr(justlog.handlers, 'JanitorWebhookHandler', lambda **k: logging.NullHandler())
    sys.modules.pop('main', None)
    import main as m

    tasks = tmp_path / 'tasks.md'
    tasks.write_text('Factuur van billing@leap.eu opslaan.')
    monkeypatch.setattr(m, 'TASKS_MD_PATH', tasks)
    monkeypatch.setattr(m.gmail_mcp, 'load_credentials', lambda path: object())
    yield m
    sys.modules.pop('main', None)


class FakeGmail:
    instances: list['FakeGmail'] = []

    def __init__(self, creds, pending=('m1', 'm2')):
        self.pending = list(pending)
        self.closed = False
        FakeGmail.instances.append(self)

    def ensure_labels(self):
        pass

    def query_pending(self, senders):
        assert senders == {'billing@leap.eu'}
        return self.pending

    def get_message(self, mid):
        return type('Ctx', (), {'id': mid, 'subject': 's', 'sender_address': 'billing@leap.eu'})()

    def close(self):
        self.closed = True


@pytest.fixture
def processed(main, monkeypatch):
    seen = []

    async def process_mail(gmail, ctx, tasks_md):
        seen.append(ctx.id)
        return 'done'

    monkeypatch.setattr(main.agent_runner, 'process_mail', process_mail)
    FakeGmail.instances = []
    return seen


def test_run_processes_each_pending_mail_and_closes_session(main, processed, monkeypatch):
    monkeypatch.setattr(main, 'GmailMcp', FakeGmail)
    assert asyncio.run(main.run()) == 0
    assert processed == ['m1', 'm2']
    assert FakeGmail.instances[0].closed


def test_run_without_pending_mail_closes_session(main, processed, monkeypatch):
    monkeypatch.setattr(main, 'GmailMcp', lambda creds: FakeGmail(creds, pending=()))
    assert asyncio.run(main.run()) == 0
    assert processed == []
    assert FakeGmail.instances[0].closed


def test_run_closes_session_when_query_fails(main, processed, monkeypatch):
    class Broken(FakeGmail):
        def query_pending(self, senders):
            raise OSError('down')

    errors = []
    monkeypatch.setattr(main, 'GmailMcp', Broken)
    monkeypatch.setattr(main.lg, 'error', lambda msg, **k: errors.append(msg))
    assert asyncio.run(main.run()) == 1
    assert FakeGmail.instances[0].closed
    assert errors and 'setup-auth' in errors[0]


def test_run_token_error_logs_setup_auth_and_exits_1(main, monkeypatch):
    def no_token(path):
        raise main.gmail_mcp.NoValidTokenError('no token')

    errors = []
    monkeypatch.setattr(main.gmail_mcp, 'load_credentials', no_token)
    monkeypatch.setattr(main.lg, 'error', lambda msg, **k: errors.append(msg))
    assert asyncio.run(main.run()) == 1
    assert 'setup-auth' in errors[0]


def test_setup_auth_uses_agent_client_files(main):
    assert main.CREDENTIALS_PATH.name == 'credentials_mcp.json'
    assert main.TOKEN_PATH.name == 'token_mcp.json'


def test_no_source_module_uses_gmail_rest_api():
    for path in SRC.glob('*.py'):
        text = path.read_text()
        assert 'googleapiclient' not in text, path
        assert 'gmail.googleapis.com/gmail/v1' not in text, path
