import base64
import io
import json
import urllib.error

import gmail_mcp
import gmail_rest
import pytest
from gmail_rest import REST_URL, GmailRest

PDF = b'%PDF-1.4 \x00\xff\xfe bytes'


class Creds:
    def __init__(self, valid=True):
        self.valid, self.token, self.refreshed = valid, 'tok', False

    def refresh(self, request):
        self.valid, self.token, self.refreshed = True, 'fresh', True


@pytest.fixture
def http(monkeypatch):
    """Scripted urlopen: each entry is a dict (JSON response) or an exception."""
    monkeypatch.setattr(gmail_mcp.time, 'sleep', lambda s: None)
    state = {'responses': [], 'requests': []}

    def urlopen(req, timeout):
        state['requests'].append(req)
        r = state['responses'].pop(0)
        if isinstance(r, BaseException):
            raise r
        return io.BytesIO(json.dumps(r).encode())

    monkeypatch.setattr(gmail_rest.urllib.request, 'urlopen', urlopen)
    return state


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError('u', code, 'err', {}, io.BytesIO(b''))


def test_attachment_decodes_unpadded_base64url(http):
    http['responses'] = [{'data': base64.urlsafe_b64encode(PDF).decode().rstrip('='), 'size': len(PDF)}]
    assert GmailRest(Creds()).attachment('m1', 'A1') == PDF
    req = http['requests'][0]
    assert req.full_url == f'{REST_URL}m1/attachments/A1'
    assert req.get_header('Authorization') == 'Bearer tok'


def test_message_id_reads_only_that_header(http):
    http['responses'] = [{'payload': {'headers': [{'name': 'Message-Id', 'value': '<x@y.nl>'}]}}]
    assert GmailRest(Creds()).message_id('m1') == '<x@y.nl>'
    assert http['requests'][0].full_url == f'{REST_URL}m1?format=metadata&metadataHeaders=Message-ID'


def test_message_id_missing_gives_none(http):
    http['responses'] = [{'payload': {}}]
    assert GmailRest(Creds()).message_id('m1') is None


def test_expired_token_is_refreshed_before_call(http):
    http['responses'] = [{'payload': {}}]
    creds = Creds(valid=False)
    GmailRest(creds).message_id('m1')
    assert creds.refreshed
    assert http['requests'][0].get_header('Authorization') == 'Bearer fresh'


def test_server_error_is_retried_with_warning(http, monkeypatch):
    logged = []
    monkeypatch.setattr(gmail_mcp.lg, 'warning', lambda *a, **k: logged.append('W'))
    monkeypatch.setattr(gmail_mcp.lg, 'error', lambda *a, **k: logged.append('E'))
    http['responses'] = [http_error(503), urllib.error.URLError('reset'), {'payload': {}}]
    assert GmailRest(Creds()).message_id('m1') is None
    assert logged == ['W', 'W']


def test_client_error_is_not_retried(http):
    http['responses'] = [http_error(404)]
    with pytest.raises(urllib.error.HTTPError):
        GmailRest(Creds()).attachment('m1', 'gone')
    assert len(http['requests']) == 1
