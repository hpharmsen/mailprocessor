import base64
import json
from unittest.mock import MagicMock

import pytest

from conftest import FIXTURES

import gmail_client
from gmail_client import GmailClient, MailContext, InvalidSenderError, DONE_LABEL, FAILED_LABEL, NOTIFY_TO


@pytest.fixture
def svc():
    '''A MagicMock that mimics the gmail service builder shape.'''
    return MagicMock()


@pytest.fixture
def client(svc, monkeypatch):
    monkeypatch.setattr(gmail_client, 'build', lambda *a, **kw: svc)
    return GmailClient(creds=MagicMock())


def _set_list_response(svc, messages):
    svc.users().messages().list().execute.return_value = {'messages': messages}


def test_query_pending_builds_q_with_inbox_senders_and_label_exclusions(client, svc):
    _set_list_response(svc, [{'id': 'm1'}, {'id': 'm2'}])
    svc.users.reset_mock()
    ids = client.query_pending({'a@b.com', 'c@d.com'})
    assert ids == ['m1', 'm2']
    call = svc.users().messages().list.call_args
    q = call.kwargs['q']
    assert 'in:inbox' in q
    assert 'from:(a@b.com OR c@d.com)' in q or 'from:(c@d.com OR a@b.com)' in q
    assert f'-label:{DONE_LABEL}' in q
    assert f'-label:{FAILED_LABEL}' in q


def test_query_pending_does_not_filter_on_read_state(client, svc):
    _set_list_response(svc, [])
    client.query_pending({'a@b.com'})
    q = client.users.list().execute.call_args if False else svc.users().messages().list.call_args.kwargs['q']
    assert 'is:unread' not in q
    assert 'is:read' not in q


def test_query_pending_empty_senders_makes_no_api_call(client, svc):
    assert client.query_pending(set()) == []
    svc.users().messages().list.assert_not_called()


def test_get_message_extracts_headers_and_attachments(client, svc):
    fixture = json.loads((FIXTURES / 'leap_message.json').read_text())
    svc.users().messages().get().execute.return_value = fixture
    ctx = client.get_message('leap-msg-1')
    assert ctx.sender_address == 'pelle.schlichting@leap24.eu'
    assert ctx.sender_name == 'Pelle Schlichting'
    assert ctx.attachment_filenames == ['Monthly Report May 2026.pdf']
    assert ctx.attachment_ids == {'Monthly Report May 2026.pdf': 'att-leap-1'}
    assert ctx.thread_id == 'leap-thread-1'
    assert ctx.subject == 'Monthly Report May 2026'


def test_get_message_rejects_crlf_in_sender(client, svc):
    fixture = json.loads((FIXTURES / 'malicious_message.json').read_text())
    svc.users().messages().get().execute.return_value = fixture
    with pytest.raises(InvalidSenderError):
        client.get_message('evil-msg-1')


def test_ensure_labels_creates_only_missing(client, svc):
    svc.users().labels().list().execute.return_value = {
        'labels': [
            {'name': 'INBOX', 'id': 'L_inbox'},
            {'name': DONE_LABEL, 'id': 'L_done_existing'},
        ]
    }
    create_mock = MagicMock()
    create_mock.execute.return_value = {'id': 'L_failed_new', 'name': FAILED_LABEL}
    svc.users().labels().create.return_value = create_mock

    client.ensure_labels()

    create_calls = svc.users().labels().create.call_args_list
    created_names = [c.kwargs['body']['name'] for c in create_calls]
    assert created_names == [FAILED_LABEL]
    assert client._label_ids[DONE_LABEL] == 'L_done_existing'
    assert client._label_ids[FAILED_LABEL] == 'L_failed_new'


def test_send_reply_to_is_notify_to_not_sender(client, svc):
    ctx = MailContext(
        id='m1', thread_id='t1', message_id_header='<orig@x.com>',
        sender_address='someone@external.com', sender_name='Someone',
        subject='Hello', date_iso='', attachment_filenames=[], attachment_ids={},
    )
    client.send_reply(ctx, 'body text')
    send_call = svc.users().messages().send.call_args
    raw_b64 = send_call.kwargs['body']['raw']
    raw = base64.urlsafe_b64decode(raw_b64).decode()
    headers_section = raw.split('\n\n', 1)[0]
    assert f'To: {NOTIFY_TO}' in headers_section
    assert 'someone@external.com' not in headers_section


def test_send_reply_keeps_thread_via_threadid_and_in_reply_to(client, svc):
    ctx = MailContext(
        id='m1', thread_id='thread-xyz', message_id_header='<orig@x.com>',
        sender_address='a@b.com', sender_name='', subject='Subj',
        date_iso='', attachment_filenames=[], attachment_ids={},
    )
    client.send_reply(ctx, 'body')
    send_call = svc.users().messages().send.call_args
    assert send_call.kwargs['body']['threadId'] == 'thread-xyz'
    raw = base64.urlsafe_b64decode(send_call.kwargs['body']['raw']).decode()
    assert 'In-Reply-To: <orig@x.com>' in raw
    assert 'References: <orig@x.com>' in raw


def test_send_reply_adds_re_prefix_when_missing(client, svc):
    ctx = MailContext(
        id='m1', thread_id='t', message_id_header='<x@x>',
        sender_address='a@b.com', sender_name='', subject='Hello world',
        date_iso='', attachment_filenames=[], attachment_ids={},
    )
    client.send_reply(ctx, 'body')
    raw = base64.urlsafe_b64decode(
        svc.users().messages().send.call_args.kwargs['body']['raw']
    ).decode()
    assert 'Subject: Re: Hello world' in raw


def test_send_reply_keeps_existing_re_prefix(client, svc):
    ctx = MailContext(
        id='m1', thread_id='t', message_id_header='<x@x>',
        sender_address='a@b.com', sender_name='', subject='Re: Hello',
        date_iso='', attachment_filenames=[], attachment_ids={},
    )
    client.send_reply(ctx, 'body')
    raw = base64.urlsafe_b64decode(
        svc.users().messages().send.call_args.kwargs['body']['raw']
    ).decode()
    # No double Re:
    assert 'Subject: Re: Hello' in raw
    assert 'Re: Re: Hello' not in raw


def test_download_attachment_b64url_decodes(client, svc):
    raw_bytes = b'PDF-bytes-here-\x00\xff'
    encoded = base64.urlsafe_b64encode(raw_bytes).decode()
    svc.users().messages().attachments().get().execute.return_value = {'data': encoded}
    assert client.download_attachment('m1', 'att1') == raw_bytes


def test_load_credentials_chmod_600_after_refresh(tmp_path, monkeypatch):
    token_path = tmp_path / 'token.json'
    creds_path = tmp_path / 'credentials.json'
    token_path.write_text('{"fake": "token"}')
    token_path.chmod(0o644)

    fake_creds = MagicMock()
    fake_creds.expired = True
    fake_creds.refresh_token = 'rt'
    fake_creds.valid = True
    fake_creds.to_json.return_value = '{"refreshed": true}'

    monkeypatch.setattr(
        gmail_client.Credentials, 'from_authorized_user_file',
        lambda *a, **kw: fake_creds,
    )
    monkeypatch.setattr(gmail_client, 'Request', lambda: MagicMock())

    gmail_client.load_credentials(token_path, creds_path)
    fake_creds.refresh.assert_called_once()
    assert token_path.read_text() == '{"refreshed": true}'
    mode = token_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_load_credentials_raises_when_no_token(tmp_path):
    token_path = tmp_path / 'token.json'  # doesn't exist
    creds_path = tmp_path / 'credentials.json'
    with pytest.raises(gmail_client.NoValidTokenError):
        gmail_client.load_credentials(token_path, creds_path)


def test_mark_read_removes_unread_label(client, svc):
    client.mark_read('m1')
    call = svc.users().messages().modify.call_args
    assert call.kwargs['id'] == 'm1'
    assert call.kwargs['body'] == {'removeLabelIds': ['UNREAD']}


def test_add_label_uses_cached_id(client, svc):
    client._label_ids[DONE_LABEL] = 'L_done_cached'
    client.add_label('m1', DONE_LABEL)
    call = svc.users().messages().modify.call_args
    assert call.kwargs['body'] == {'addLabelIds': ['L_done_cached']}
