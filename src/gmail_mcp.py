"""Gmail access via Google's Gmail MCP server, with a synchronous facade.

All Gmail traffic goes through gmailmcp.googleapis.com (Workspace AI Tools
Addendum). Messages are fetched once in RAW format and parsed locally, so
headers, body and attachment bytes come from one call and never pass the LLM.
"""

import asyncio
import base64
import concurrent.futures
import contextlib
import email
import json
import os
import queue
import re
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path

import httpx2
from google.auth.exceptions import TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from justlog import lg
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

MCP_URL = 'https://gmailmcp.googleapis.com/mcp/v1self'
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
DONE_LABEL = 'mailprocessor/done'
FAILED_LABEL = 'mailprocessor/failed'
SKIPPED_LABEL = 'mailprocessor/skipped'
OUR_LABELS = (DONE_LABEL, FAILED_LABEL, SKIPPED_LABEL)
GMAIL_DEEPLINK = 'https://mail.google.com/mail/u/0/#inbox/'
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0
CALL_TIMEOUT_SECONDS = 330  # just above the SDK's 300 s read timeout
WAIT_POLL_SECONDS = 10  # search_threads costs 10 quota units per call
INTERNAL_ERROR = -32603  # the SDK maps every HTTP 4xx/5xx to this code


class NoValidTokenError(Exception):
    """No valid OAuth token; user must run setup-auth."""


class InvalidSenderError(Exception):
    """Sender header malformed or contains CRLF."""


class McpToolError(Exception):
    """The MCP server ran the tool and reported an error."""


@dataclass
class MailContext:
    id: str
    thread_id: str
    message_id_header: str | None
    sender_address: str
    sender_name: str
    subject: str
    date_iso: str
    attachment_filenames: list[str]
    attachment_ids: dict[str, str]  # filename -> Gmail partId


def load_credentials(token_path: Path) -> Credentials:
    """Load + refresh OAuth credentials. Raises NoValidTokenError if unusable."""
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES) if token_path.exists() else None
    if creds and creds.expired and creds.refresh_token:
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                creds.refresh(Request())
                break
            except TransportError as e:
                if attempt == RETRY_ATTEMPTS:
                    lg.error('oauth token refresh failed after retries', attempts=attempt, exc_info=e)
                    raise
                lg.warning('oauth token refresh failed, retrying', error=str(e), attempt=attempt)
                time.sleep(RETRY_BACKOFF_SECONDS)
        _write_token(token_path, creds)
    if not creds or not creds.valid:
        raise NoValidTokenError(f'Run `uv run src/main.py setup-auth` first (token at {token_path}).')
    return creds


def run_oauth_flow(credentials_path: Path, token_path: Path) -> Credentials:
    """Interactive OAuth: opens browser, writes the token with chmod 600."""
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(port=0)
    _write_token(token_path, creds)
    return creds


def _write_token(token_path: Path, creds: Credentials) -> None:
    token_path.write_text(creds.to_json())
    os.chmod(token_path, 0o600)


def _is_transient(exc: Exception) -> bool:
    """Network blips and HTTP errors (the SDK hides the status code); tool errors are permanent."""
    if isinstance(exc, MCPError):
        return exc.error.code == INTERNAL_ERROR
    return isinstance(exc, (OSError, httpx2.TransportError))


# --- MIME parsing --------------------------------------------------------------


def _parse_raw(raw: str) -> EmailMessage:
    """RAW is base64url MIME; plain MIME text is accepted too (it always has a ':' in its headers)."""
    data = raw.encode() if ':' in raw[:1000] else base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))
    return email.message_from_bytes(data, policy=policy.default)


def _leaf_parts(msg: EmailMessage) -> Iterator[tuple[str, EmailMessage]]:
    """Yield (partId, part) for every non-multipart part, numbered like Gmail: 0, 1, 1.0, ..."""

    def walk(part, pid):
        if part.is_multipart():
            for i, sub in enumerate(part.iter_parts()):
                yield from walk(sub, f'{pid}.{i}' if pid else str(i))
        else:
            yield pid, part

    return walk(msg, '')


def _attachments(msg: EmailMessage) -> dict[str, str]:
    return {part.get_filename(): pid for pid, part in _leaf_parts(msg) if part.get_filename()}


def _text(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except (LookupError, ValueError):  # unknown charset or broken encoding
        return (part.get_payload(decode=True) or b'').decode('utf-8', errors='replace')


def body_text(msg: EmailMessage) -> str:
    """Body as plain text; without a text/plain part the HTML is converted and its links appended."""
    texts = [
        (p.get_content_type(), _text(p))
        for _, p in _leaf_parts(msg)
        if not p.get_filename() and p.get_content_type() in ('text/plain', 'text/html')
    ]
    plain = [t for kind, t in texts if kind == 'text/plain']
    if plain:
        return '\n'.join(plain)
    html = '\n'.join(t for kind, t in texts if kind == 'text/html')
    if not html:
        return ''
    text, links = html_to_text_and_links(html)
    first_label: dict[str, str] = {}
    for label, href in links:
        if href.startswith(('http://', 'https://')):
            first_label.setdefault(href, label)
    lines = [f'- {label or "(no text)"}: {href}' for href, label in first_label.items()]
    return f'{text}\n\n=== Links ===\n' + '\n'.join(lines) if lines else text


def _validate_address(raw: str) -> tuple[str, str]:
    """Return (name, addr); raise InvalidSenderError on CRLF or a missing address."""
    name, addr = parseaddr(raw)
    if any(c in raw for c in '\r\n') or not addr or '@' not in addr:
        raise InvalidSenderError(raw)
    return name, addr


def _message_id(msg: EmailMessage) -> str | None:
    """The Message-ID if it is a single well-formed <...> token; it goes into outgoing headers."""
    value = str(msg.get('Message-ID') or '').strip()
    return value if re.fullmatch(r'<[^<>\s]+>', value) else None


class _HTMLToText(HTMLParser):
    """Minimal HTML -> text + anchor-list. Drops <style>/<script>."""

    def __init__(self):
        super().__init__()
        self._buf: list[str] = []
        self._skip = 0
        self.links: list[tuple[str, str]] = []
        self._link_href: str | None = None
        self._link_text_start: int | None = None

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self._skip += 1
        if tag == 'a':
            self._link_href = dict(attrs).get('href') or ''
            self._link_text_start = len(self._buf)
        if tag in ('p', 'br', 'div', 'li', 'tr', 'h1', 'h2', 'h3'):
            self._buf.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self._skip:
            self._skip -= 1
        if tag == 'a' and self._link_href is not None:
            self.links.append((''.join(self._buf[self._link_text_start :]).strip(), self._link_href))
            self._link_href = None

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def text(self) -> str:
        raw = ''.join(self._buf)
        return re.sub(r'\n\s*\n+', '\n\n', re.sub(r'[ \t]+', ' ', raw)).strip()


def html_to_text_and_links(html: str) -> tuple[str, list[tuple[str, str]]]:
    """Strip HTML to text + return list of (anchor_text, href)."""
    parser = _HTMLToText()
    with contextlib.suppress(Exception):  # malformed HTML: keep what was parsed so far
        parser.feed(html)
    return parser.text(), parser.links


# --- client --------------------------------------------------------------------


def _server_connector(token: str) -> Callable[[], contextlib.AbstractAsyncContextManager]:
    @contextlib.asynccontextmanager
    async def connect():
        http = create_mcp_http_client(headers={'Authorization': f'Bearer {token}'})
        # max_sse_event_size=None: a RAW message with a PDF easily exceeds the 1 MB default.
        transport = streamable_http_client(MCP_URL, http_client=http, max_sse_event_size=None)
        async with http, Client(transport) as client:
            yield client

    return connect


class GmailMcp:
    """Synchronous Gmail operations over one MCP session that lives on its own thread.

    justai runs tools synchronously inside a running event loop, and the MCP SDK is
    async, so the session runs in a worker thread with its own loop (like BrowserSession).
    """

    def __init__(
        self,
        creds: Credentials | None = None,
        connect: Callable[[], contextlib.AbstractAsyncContextManager] | None = None,
    ):
        assert creds or connect
        self._connect = connect or _server_connector(creds.token)
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._label_ids: dict[str, str] = {}
        self._messages: dict[str, tuple[dict, EmailMessage]] = {}

    # --- session plumbing ----------------------------------------------------

    def _start(self) -> None:
        ready: concurrent.futures.Future = concurrent.futures.Future()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._serve(ready)), daemon=True)
        self._thread.start()
        ready.result(timeout=CALL_TIMEOUT_SECONDS)

    async def _serve(self, ready: concurrent.futures.Future) -> None:
        try:
            async with self._connect() as client:
                ready.set_result(None)
                while (item := await asyncio.to_thread(self._queue.get)) is not None:
                    name, args, fut = item
                    try:
                        fut.set_result(await client.call_tool(name, args))
                    except Exception as e:
                        fut.set_exception(e)
        except Exception as e:
            if not ready.done():
                ready.set_exception(e)
            else:  # session died: later calls time out and surface as an error there
                lg.warning('mcp session ended with error', error=str(e))

    def close(self) -> None:
        if self._thread:
            self._queue.put(None)
            self._thread.join(timeout=30)
            self._thread = None

    def _call_once(self, name: str, args: dict) -> dict:
        if not self._thread:
            self._start()
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._queue.put((name, args, fut))
        result = fut.result(timeout=CALL_TIMEOUT_SECONDS)
        if result.is_error:
            raise McpToolError(f'{name}: ' + ' '.join(c.text for c in result.content if c.type == 'text'))
        if result.structured_content is not None:
            return result.structured_content
        return json.loads(''.join(c.text for c in result.content if c.type == 'text') or '{}')

    def call(self, name: str, args: dict) -> dict:
        """Call an MCP tool; retry transient failures with a WARNING each, re-raise the last one."""
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                return self._call_once(name, args)
            except Exception as e:
                if attempt == RETRY_ATTEMPTS or not _is_transient(e):
                    raise
                lg.warning(f'mcp {name} failed, retrying', attempt=attempt, error=str(e))
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    def _search(self, query: str) -> Iterator[dict]:
        """Yield every message of every matching thread (search_threads returns at most 5 per thread)."""
        args = {'query': query, 'pageSize': 50}
        while True:
            res = self.call('search_threads', args)
            for thread in res.get('threads', []):
                msgs = thread.get('messages', [])
                if thread.get('messageCount', len(msgs)) > len(msgs):
                    msgs = self.call('get_thread', {'threadId': thread['id'], 'messageFormat': 'MINIMAL'})['messages']
                yield from msgs
            if not res.get('nextPageToken'):
                return
            args = {**args, 'pageToken': res['nextPageToken']}

    def _message(self, message_id: str) -> tuple[dict, EmailMessage]:
        if message_id not in self._messages:
            res = self.call('get_message', {'messageId': message_id, 'messageFormat': 'RAW'})
            self._messages[message_id] = res, _parse_raw(res['raw'])
        return self._messages[message_id]

    # --- queries ---------------------------------------------------------------

    def query_pending(self, senders: set[str]) -> list[str]:
        """Message-ids of inbox mails from a watched sender without done/failed/skipped label.

        Read-state is ignored on purpose: opening a mail by hand must not stop processing.
        search_threads returns whole threads, so each message is filtered again here.
        """
        if not senders:
            return []
        wanted = {s.lower() for s in senders}
        ours = {self._label_ids.get(name, name) for name in OUR_LABELS}
        q = f'in:inbox from:({" OR ".join(sorted(senders))}) ' + ' '.join(f'-label:{n}' for n in OUR_LABELS)
        return [
            m['id']
            for m in self._search(q)
            if 'INBOX' in m.get('labelIds', [])
            and not ours & set(m.get('labelIds', []))
            and m.get('sender', '').lower() in wanted
        ]

    def get_message(self, message_id: str) -> MailContext:
        """Fetch the RAW message and build a MailContext. Raises InvalidSenderError on CRLF."""
        res, msg = self._message(message_id)
        try:
            raw_from = str(msg.get('From') or '')
        except ValueError as e:  # the stdlib refuses CR/LF in address parts
            raise InvalidSenderError(str(e)) from e
        name, addr = _validate_address(raw_from)
        attachments = _attachments(msg)
        return MailContext(
            id=message_id,
            thread_id=res.get('threadId', ''),
            message_id_header=_message_id(msg),
            sender_address=addr,
            sender_name=name,
            subject=' '.join(str(msg.get('Subject') or '').split()),
            date_iso=str(msg.get('Date') or ''),
            attachment_filenames=list(attachments),
            attachment_ids=attachments,
        )

    def get_message_body(self, message_id: str) -> str:
        return body_text(self._message(message_id)[1])

    def download_attachment(self, message_id: str, part_id: str) -> bytes:
        """Decoded bytes of the MIME part with this Gmail partId. Raises KeyError if absent."""
        for pid, part in _leaf_parts(self._message(message_id)[1]):
            if pid == part_id:
                return part.get_payload(decode=True) or b''
        raise KeyError(f'part {part_id} not in message {message_id}')

    def wait_for_email(
        self,
        sender: str,
        subject_contains: str,
        timeout_seconds: int,
        after_epoch: int,
        poll_interval: float = WAIT_POLL_SECONDS,
    ) -> str:
        """Poll until a mail from sender arrives after after_epoch; return the newest one's body."""
        q = f'from:{sender} subject:({subject_contains}) after:{after_epoch}'
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            hits = [
                m
                for m in self._search(q)
                if m.get('sender', '').lower() == sender.lower()
                and datetime.fromisoformat(m['date']).timestamp() >= after_epoch
            ]
            if hits:
                return self.get_message_body(max(hits, key=lambda m: m['date'])['id'])
            time.sleep(poll_interval)
        raise TimeoutError(f'no mail from {sender} with subject ~ "{subject_contains}" within {timeout_seconds}s')

    # --- labels ----------------------------------------------------------------

    def ensure_labels(self) -> None:
        """Create our labels if missing and cache name -> id."""
        by_name = {lab['name']: lab['labelId'] for lab in self.call('list_labels', {}).get('labels', [])}
        for name in OUR_LABELS:
            self._label_ids[name] = by_name.get(name) or self.call('create_label', {'displayName': name})['labelId']

    def add_label(self, message_id: str, label_name: str) -> None:
        if label_name not in self._label_ids:
            self.ensure_labels()
        self.call('update_message_labels', {'messageId': message_id, 'addLabelIds': [self._label_ids[label_name]]})

    def mark_read(self, message_id: str) -> None:
        self.call('update_message_labels', {'messageId': message_id, 'removeLabelIds': ['UNREAD']})
