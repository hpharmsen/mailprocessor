import base64
import contextlib
from datetime import UTC
from email.message import EmailMessage
from types import SimpleNamespace

import gmail_mcp
import pytest
from gmail_mcp import (
    DONE_LABEL,
    FAILED_LABEL,
    SKIPPED_LABEL,
    GmailMcp,
    InvalidSenderError,
)
from mcp import MCPError

PDF = b'%PDF-1.4 invoice bytes \x00\x01\x02'
CSV = b'a,b\n1,2\n'


def raw_mail(
    sender='Leap <billing@leap.eu>',
    subject='Factuur 2026-10',
    message_id='<abc@leap.eu>',
    attachments=(('invoice.pdf', PDF),),
    html=None,
    plain='Hallo, bijgaand de factuur.',
) -> str:
    """Build a MIME message and return it base64url-encoded, as the RAW format does."""
    msg = EmailMessage()
    msg['From'] = sender
    msg['To'] = 'hp@harmsen.nl'
    msg['Subject'] = subject
    msg['Date'] = 'Mon, 02 Nov 2026 10:00:00 +0100'
    if message_id:
        msg['Message-ID'] = message_id
    if plain is not None:
        msg.set_content(plain)
    if html is not None:
        if plain is None:
            msg.set_content(html, subtype='html')
        else:
            msg.add_alternative(html, subtype='html')
    for name, data in attachments:
        msg.add_attachment(data, maintype='application', subtype='octet-stream', filename=name)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def ok(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(is_error=False, structured_content=payload, content=[])


class FakeClient:
    """Stands in for mcp.Client: scripted responses per tool, records every call."""

    def __init__(self, handlers: dict):
        self.handlers = handlers
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        h = self.handlers[name]
        result = h(arguments) if callable(h) else h
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(gmail_mcp.time, 'sleep', lambda s: None)


def make(handlers: dict) -> tuple[GmailMcp, FakeClient]:
    fake = FakeClient(handlers)

    @contextlib.asynccontextmanager
    async def connect():
        yield fake

    gmail = GmailMcp(connect=connect)
    gmail._label_ids = {DONE_LABEL: 'L_done', FAILED_LABEL: 'L_failed', SKIPPED_LABEL: 'L_skip'}
    return gmail, fake


def msg(mid, sender='billing@leap.eu', labels=('INBOX',), date='2026-11-02T09:00:00Z'):
    return {'id': mid, 'sender': sender, 'labelIds': list(labels), 'date': date}


# --- query_pending -----------------------------------------------------------


def test_query_pending_query_excludes_done_failed_skipped():
    gmail, fake = make({'search_threads': ok({'threads': [{'id': 't1', 'messages': [msg('m1')]}]})})
    ids = gmail.query_pending({'billing@leap.eu', 'x@y.nl', 'a@b.com'})
    gmail.close()
    q = fake.calls[0][1]['query']
    assert 'in:inbox' in q and 'from:(a@b.com OR billing@leap.eu OR x@y.nl)' in q
    for label in (DONE_LABEL, FAILED_LABEL, SKIPPED_LABEL):
        assert f'-label:{label}' in q
    assert ids == ['m1']


def test_query_pending_filters_messages_within_thread():
    thread = {
        'id': 't1',
        'messages': [
            msg('old', labels=('INBOX', 'L_done')),
            msg('new'),
            msg('archived', labels=()),
            msg('other', sender='someone@else.com'),
            msg('skipped', labels=('INBOX', 'L_skip')),
        ],
    }
    gmail, _ = make({'search_threads': ok({'threads': [thread]})})
    assert gmail.query_pending({'billing@leap.eu'}) == ['new']
    gmail.close()


def test_query_pending_sender_match_is_case_insensitive():
    gmail, _ = make(
        {'search_threads': ok({'threads': [{'id': 't', 'messages': [msg('m', sender='Billing@Leap.eu')]}]})}
    )
    assert gmail.query_pending({'billing@leap.eu'}) == ['m']
    gmail.close()


def test_query_pending_fetches_full_thread_when_truncated():
    search = {'threads': [{'id': 't1', 'messageCount': 7, 'messages': [msg('m6')]}]}
    full = {'id': 't1', 'messages': [msg('m0'), msg('m6')]}
    gmail, fake = make({'search_threads': ok(search), 'get_thread': ok(full)})
    assert gmail.query_pending({'billing@leap.eu'}) == ['m0', 'm6']
    gmail.close()
    assert fake.calls[1] == ('get_thread', {'threadId': 't1', 'messageFormat': 'MINIMAL'})


def test_query_pending_follows_pagination():
    pages = iter(
        [
            ok({'threads': [], 'nextPageToken': 'p2'}),
            ok({'threads': [{'id': 't', 'messages': [msg('m')]}]}),
        ]
    )
    gmail, fake = make({'search_threads': lambda a: next(pages)})
    assert gmail.query_pending({'billing@leap.eu'}) == ['m']
    gmail.close()
    assert fake.calls[1][1]['pageToken'] == 'p2'


def test_query_pending_empty_senders_makes_no_call():
    gmail, fake = make({})
    assert gmail.query_pending(set()) == []
    gmail.close()
    assert fake.calls == []


# --- get_message / body / attachments ----------------------------------------


def test_get_message_builds_mail_context_from_raw():
    raw = raw_mail(attachments=(('invoice.pdf', PDF), ('data.csv', CSV)))
    gmail, fake = make({'get_message': ok({'id': 'm1', 'threadId': 't1', 'raw': raw})})
    ctx = gmail.get_message('m1')
    gmail.close()
    assert fake.calls == [('get_message', {'messageId': 'm1', 'messageFormat': 'RAW'})]
    assert (ctx.id, ctx.thread_id) == ('m1', 't1')
    assert (ctx.sender_name, ctx.sender_address) == ('Leap', 'billing@leap.eu')
    assert ctx.subject == 'Factuur 2026-10'
    assert ctx.date_iso == 'Mon, 02 Nov 2026 10:00:00 +0100'
    assert ctx.message_id_header == '<abc@leap.eu>'
    assert ctx.attachment_filenames == ['invoice.pdf', 'data.csv']
    assert set(ctx.attachment_ids) == {'invoice.pdf', 'data.csv'}


def test_get_message_without_message_id_gives_none():
    gmail, _ = make({'get_message': ok({'id': 'm1', 'threadId': 't1', 'raw': raw_mail(message_id=None)})})
    assert gmail.get_message('m1').message_id_header is None
    gmail.close()


def test_get_message_rejects_crlf_in_sender():
    # A folded header is unfolded by the parser; the real vector is CRLF inside an encoded-word.
    name = base64.b64encode(b'Evil\r\nBcc: leak@attacker.example').decode()
    raw = base64.urlsafe_b64encode(
        f'From: =?utf-8?b?{name}?= <evil@x.com>\r\nSubject: hi\r\n\r\nbody\r\n'.encode()
    ).decode()
    gmail, _ = make({'get_message': ok({'id': 'm1', 'threadId': 't1', 'raw': raw})})
    with pytest.raises(InvalidSenderError):
        gmail.get_message('m1')
    gmail.close()


def test_get_message_strips_crlf_from_subject():
    subj = base64.b64encode(b'Factuur\r\nBcc: x@y.z').decode()
    raw = base64.urlsafe_b64encode(
        f'From: a@b.nl\r\nSubject: =?utf-8?b?{subj}?=\r\nMessage-ID: <q@b.nl>\r\n\r\nbody\r\n'.encode()
    ).decode()
    gmail, _ = make({'get_message': ok({'id': 'm1', 'threadId': 't1', 'raw': raw})})
    assert gmail.get_message('m1').subject == 'Factuur Bcc: x@y.z'
    gmail.close()


def test_get_message_accepts_unencoded_raw():
    raw = base64.urlsafe_b64decode(raw_mail()).decode()
    gmail, _ = make({'get_message': ok({'id': 'm1', 'threadId': 't1', 'raw': raw})})
    assert gmail.get_message('m1').sender_address == 'billing@leap.eu'
    gmail.close()


def test_download_attachment_returns_exact_bytes_per_part():
    raw = raw_mail(attachments=(('invoice.pdf', PDF), ('data.csv', CSV)))
    gmail, fake = make({'get_message': ok({'id': 'm1', 'threadId': 't1', 'raw': raw})})
    ctx = gmail.get_message('m1')
    assert gmail.download_attachment('m1', ctx.attachment_ids['invoice.pdf']) == PDF
    assert gmail.download_attachment('m1', ctx.attachment_ids['data.csv']) == CSV
    gmail.close()
    assert len(fake.calls) == 1, 'RAW is fetched once per message'


def test_download_attachment_unknown_part_raises():
    gmail, _ = make({'get_message': ok({'id': 'm1', 'threadId': 't1', 'raw': raw_mail()})})
    with pytest.raises(KeyError):
        gmail.download_attachment('m1', '9.9')
    gmail.close()


def test_attachment_part_ids_follow_gmail_numbering():
    # multipart/mixed: [0] = text/plain, [1] = invoice.pdf
    gmail, _ = make({'get_message': ok({'id': 'm1', 'threadId': 't1', 'raw': raw_mail()})})
    assert gmail.get_message('m1').attachment_ids == {'invoice.pdf': '1'}
    gmail.close()


def test_get_message_body_prefers_plain_text():
    gmail, _ = make({'get_message': ok({'id': 'm1', 'threadId': 't1', 'raw': raw_mail(html='<p>html</p>')})})
    assert gmail.get_message_body('m1').strip() == 'Hallo, bijgaand de factuur.'
    gmail.close()


def test_get_message_body_converts_html_and_lists_links():
    html = '<p>Bekijk je factuur</p><a href="https://x.ai/invoice/1">online</a>'
    gmail, _ = make({'get_message': ok({'id': 'm1', 'threadId': 't1', 'raw': raw_mail(plain=None, html=html)})})
    body = gmail.get_message_body('m1')
    gmail.close()
    assert 'Bekijk je factuur' in body
    assert '- online: https://x.ai/invoice/1' in body


# --- labels ------------------------------------------------------------------


def test_ensure_labels_creates_only_missing():
    existing = {'labels': [{'labelId': 'L1', 'name': DONE_LABEL}, {'labelId': 'X', 'name': 'other'}]}
    created = iter(['L2', 'L3'])
    gmail, fake = make(
        {
            'list_labels': ok(existing),
            'create_label': lambda a: ok({'labelId': next(created), 'name': a['displayName']}),
        }
    )
    gmail._label_ids = {}
    gmail.ensure_labels()
    gmail.close()
    assert [a['displayName'] for n, a in fake.calls if n == 'create_label'] == [FAILED_LABEL, SKIPPED_LABEL]
    assert gmail._label_ids == {DONE_LABEL: 'L1', FAILED_LABEL: 'L2', SKIPPED_LABEL: 'L3'}


def test_add_label_uses_label_id():
    gmail, fake = make({'update_message_labels': ok({'messageId': 'm1', 'labelIds': []})})
    gmail.add_label('m1', SKIPPED_LABEL)
    gmail.close()
    assert fake.calls == [('update_message_labels', {'messageId': 'm1', 'addLabelIds': ['L_skip']})]


def test_mark_read_only_removes_unread():
    gmail, fake = make({'update_message_labels': ok({'messageId': 'm1', 'labelIds': []})})
    gmail.mark_read('m1')
    gmail.close()
    assert fake.calls == [('update_message_labels', {'messageId': 'm1', 'removeLabelIds': ['UNREAD']})]


# --- wait_for_email ----------------------------------------------------------

AFTER = 1_793_000_000  # epoch seconds; 2026-10-26T...


def iso(epoch: int) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(epoch, UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def test_wait_for_email_returns_body_of_newest_after_start(no_sleep):
    thread = {
        'id': 't',
        'messages': [
            msg('old', sender='noreply@odido.nl', date=iso(AFTER - 3600)),
            msg('new', sender='noreply@odido.nl', date=iso(AFTER + 30)),
        ],
    }
    raws = {'new': raw_mail(sender='noreply@odido.nl', plain='Je code is 123456', attachments=())}
    gmail, fake = make(
        {
            'search_threads': ok({'threads': [thread]}),
            'get_message': lambda a: ok({'id': a['messageId'], 'threadId': 't', 'raw': raws[a['messageId']]}),
        }
    )
    body = gmail.wait_for_email('noreply@odido.nl', 'code', 120, AFTER)
    gmail.close()
    assert '123456' in body
    assert f'after:{AFTER}' in fake.calls[0][1]['query']


def test_wait_for_email_hits_on_second_poll(no_sleep):
    polls = iter(
        [
            ok({}),
            ok({'threads': [{'id': 't', 'messages': [msg('new', sender='noreply@odido.nl', date=iso(AFTER + 5))]}]}),
        ]
    )
    raw = raw_mail(sender='noreply@odido.nl', plain='code 999', attachments=())
    gmail, fake = make(
        {
            'search_threads': lambda a: next(polls),
            'get_message': ok({'id': 'new', 'threadId': 't', 'raw': raw}),
        }
    )
    assert 'code 999' in gmail.wait_for_email('noreply@odido.nl', 'code', 120, AFTER)
    gmail.close()
    assert [n for n, _ in fake.calls].count('search_threads') == 2


def test_wait_for_email_times_out(monkeypatch):
    clock = {'t': 0.0}
    monkeypatch.setattr(gmail_mcp.time, 'monotonic', lambda: clock['t'])
    monkeypatch.setattr(gmail_mcp.time, 'sleep', lambda s: clock.__setitem__('t', clock['t'] + s))
    gmail, fake = make({'search_threads': ok({})})
    with pytest.raises(TimeoutError):
        gmail.wait_for_email('noreply@odido.nl', 'code', 120, AFTER)
    gmail.close()
    assert len(fake.calls) == 12, 'polls every 10 seconds'


# --- retry -------------------------------------------------------------------


def transient():
    return MCPError(code=-32603, message='Server returned an error response')


def test_transient_error_then_success_logs_warning_not_error(no_sleep, monkeypatch):
    attempts = iter([transient(), ok({'labels': []})])
    logged = []
    monkeypatch.setattr(gmail_mcp.lg, 'warning', lambda *a, **k: logged.append('W'))
    monkeypatch.setattr(gmail_mcp.lg, 'error', lambda *a, **k: logged.append('E'))
    gmail, fake = make({'list_labels': lambda a: next(attempts)})
    assert gmail.call('list_labels', {}) == {'labels': []}
    gmail.close()
    assert logged == ['W']


def test_persistent_error_raises_after_last_attempt_without_logging_error(no_sleep, monkeypatch):
    logged = []
    monkeypatch.setattr(gmail_mcp.lg, 'warning', lambda *a, **k: logged.append('W'))
    monkeypatch.setattr(gmail_mcp.lg, 'error', lambda *a, **k: logged.append('E'))
    gmail, fake = make({'list_labels': lambda a: transient()})
    with pytest.raises(MCPError):
        gmail.call('list_labels', {})
    gmail.close()
    assert len(fake.calls) == gmail_mcp.RETRY_ATTEMPTS
    # The caller logs the single ERROR; the client only warns per retry.
    assert logged == ['W'] * (gmail_mcp.RETRY_ATTEMPTS - 1)


def test_tool_error_result_is_not_retried(no_sleep):
    err = SimpleNamespace(
        is_error=True, structured_content=None, content=[SimpleNamespace(type='text', text='label not found')]
    )
    gmail, fake = make({'update_message_labels': err})
    with pytest.raises(gmail_mcp.McpToolError, match='label not found'):
        gmail.call('update_message_labels', {'messageId': 'm'})
    gmail.close()
    assert len(fake.calls) == 1


def test_text_content_is_parsed_as_json_when_no_structured_content():
    res = SimpleNamespace(
        is_error=False, structured_content=None, content=[SimpleNamespace(type='text', text='{"labels": []}')]
    )
    gmail, _ = make({'list_labels': res})
    assert gmail.call('list_labels', {}) == {'labels': []}
    gmail.close()


# --- credentials -------------------------------------------------------------


def test_load_credentials_raises_when_no_token(tmp_path):
    with pytest.raises(gmail_mcp.NoValidTokenError):
        gmail_mcp.load_credentials(tmp_path / 'token_mcp.json')


def test_load_credentials_refreshes_expired_token_and_chmods(tmp_path, monkeypatch):
    token_path = tmp_path / 'token_mcp.json'
    token_path.write_text('{}')

    class Creds:
        expired, refresh_token, valid = True, 'r', False

        def refresh(self, request):
            self.expired, self.valid = False, True

        def to_json(self):
            return '{"refreshed": true}'

    monkeypatch.setattr(gmail_mcp.Credentials, 'from_authorized_user_file', lambda *a: Creds())
    creds = gmail_mcp.load_credentials(token_path)
    assert creds.valid
    assert token_path.read_text() == '{"refreshed": true}'
    assert token_path.stat().st_mode & 0o777 == 0o600


def test_module_does_not_use_rest_client():
    assert 'googleapiclient' not in open(gmail_mcp.__file__).read()
