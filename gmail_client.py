'''Gmail client: OAuth + read + write operations.'''
import base64
import os
import re
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
NOTIFY_TO = 'hp@harmsen.nl'
DONE_LABEL = 'mailprocessor/done'
FAILED_LABEL = 'mailprocessor/failed'


class NoValidTokenError(Exception):
    '''No valid OAuth token; user must run setup-auth.'''


class InvalidSenderError(Exception):
    '''Sender header malformed or contains CRLF.'''


@dataclass
class MailContext:
    id: str
    thread_id: str
    message_id_header: str
    sender_address: str
    sender_name: str
    subject: str
    date_iso: str
    attachment_filenames: list[str]
    attachment_ids: dict[str, str]


def load_credentials(token_path: Path, credentials_path: Path) -> Credentials:
    '''Load + refresh OAuth credentials. Raises NoValidTokenError if unusable.'''
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
        os.chmod(token_path, 0o600)
    if not creds or not creds.valid:
        raise NoValidTokenError(f'Run `uv run main.py setup-auth` first (token at {token_path}).')
    return creds


def run_oauth_flow(credentials_path: Path, token_path: Path) -> Credentials:
    '''Interactive OAuth — opens browser, writes token.json with chmod 600.'''
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(port=0)
    token_path.write_text(creds.to_json())
    os.chmod(token_path, 0o600)
    return creds


def _header(headers: list[dict], name: str) -> str:
    name_lower = name.lower()
    for h in headers:
        if h['name'].lower() == name_lower:
            return h['value']
    return ''


def _validate_address(raw: str) -> tuple[str, str]:
    '''Return (name, addr); raise InvalidSenderError on CRLF.'''
    if '\r' in raw or '\n' in raw:
        raise InvalidSenderError(raw)
    name, addr = parseaddr(raw)
    if '\r' in addr or '\n' in addr or '\r' in name or '\n' in name:
        raise InvalidSenderError(raw)
    if not addr or '@' not in addr:
        raise InvalidSenderError(raw)
    return name, addr


def _walk_parts(payload: dict):
    '''Yield every part in a payload tree (including the root if it has data).'''
    yield payload
    for sub in payload.get('parts') or []:
        yield from _walk_parts(sub)


class _HTMLToText(HTMLParser):
    '''Minimal HTML -> text + anchor-list. Drops <style>/<script>.'''
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
            href = dict(attrs).get('href') or ''
            self._link_href = href
            self._link_text_start = len(self._buf)
        if tag in ('p', 'br', 'div', 'li', 'tr', 'h1', 'h2', 'h3'):
            self._buf.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self._skip:
            self._skip -= 1
        if tag == 'a' and self._link_href is not None:
            text = ''.join(self._buf[self._link_text_start:]).strip()
            self.links.append((text, self._link_href))
            self._link_href = None
            self._link_text_start = None

    def handle_data(self, data):
        if self._skip:
            return
        self._buf.append(data)

    def text(self) -> str:
        raw = ''.join(self._buf)
        # collapse whitespace
        return re.sub(r'\n\s*\n+', '\n\n', re.sub(r'[ \t]+', ' ', raw)).strip()


def html_to_text_and_links(html: str) -> tuple[str, list[tuple[str, str]]]:
    '''Strip HTML to text + return list of (anchor_text, href).'''
    parser = _HTMLToText()
    try:
        parser.feed(html)
    except Exception:
        pass
    return parser.text(), parser.links


class GmailClient:
    '''Thin wrapper around Gmail API with per-operation methods.'''

    def __init__(self, creds: Credentials):
        self._svc = build('gmail', 'v1', credentials=creds, cache_discovery=False)
        self._label_ids: dict[str, str] = {}

    # --- queries ---------------------------------------------------------

    def query_pending(self, senders: set[str]) -> list[str]:
        '''Return message-ids of pending inbox-mails from any sender.

        "Pending" = in inbox, from a watched sender, without our done/failed labels.
        Read-state is intentionally ignored: opening a mail manually should not
        prevent the mailprocessor from handling it. The done/failed labels are
        the single source of truth for "already processed".

        `in:inbox` keeps archived mails out of scope (an old unprocessed receipt
        that you archived months ago must not get picked up).
        '''
        if not senders:
            return []
        from_clause = ' OR '.join(sorted(senders))
        q = (
            f'in:inbox from:({from_clause}) '
            f'-label:{DONE_LABEL} -label:{FAILED_LABEL}'
        )
        res = self._svc.users().messages().list(userId='me', q=q).execute()
        return [m['id'] for m in res.get('messages', [])]

    def get_message(self, message_id: str) -> MailContext:
        '''Fetch full message and build MailContext. Raises InvalidSenderError on CRLF.'''
        msg = self._svc.users().messages().get(
            userId='me', id=message_id, format='full'
        ).execute()
        payload = msg['payload']
        headers = payload.get('headers', [])

        raw_from = _header(headers, 'From')
        name, addr = _validate_address(raw_from)

        attachment_filenames: list[str] = []
        attachment_ids: dict[str, str] = {}
        for part in _walk_parts(payload):
            filename = part.get('filename') or ''
            body = part.get('body') or {}
            att_id = body.get('attachmentId')
            if filename and att_id:
                attachment_filenames.append(filename)
                attachment_ids[filename] = att_id

        return MailContext(
            id=msg['id'],
            thread_id=msg['threadId'],
            message_id_header=_header(headers, 'Message-Id') or _header(headers, 'Message-ID'),
            sender_address=addr,
            sender_name=name,
            subject=_header(headers, 'Subject'),
            date_iso=_header(headers, 'Date'),
            attachment_filenames=attachment_filenames,
            attachment_ids=attachment_ids,
        )

    def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        '''Fetch attachment bytes by id.'''
        res = self._svc.users().messages().attachments().get(
            userId='me', messageId=message_id, id=attachment_id
        ).execute()
        return base64.urlsafe_b64decode(res['data'])

    def get_message_body(self, message_id: str) -> str:
        '''Return body as plain text. If no text/plain part exists, the text/html
        part is converted to text and a list of links is appended.'''
        msg = self._svc.users().messages().get(
            userId='me', id=message_id, format='full'
        ).execute()
        plain_chunks: list[str] = []
        html_chunks: list[str] = []
        for part in _walk_parts(msg['payload']):
            mime = part.get('mimeType') or ''
            data = (part.get('body') or {}).get('data')
            if not data:
                continue
            text = base64.urlsafe_b64decode(data + '==').decode('utf-8', errors='replace')
            if mime == 'text/plain':
                plain_chunks.append(text)
            elif mime == 'text/html':
                html_chunks.append(text)
        if plain_chunks:
            return '\n'.join(plain_chunks)
        html_joined = '\n'.join(html_chunks)
        if not html_joined:
            return ''
        text, links = html_to_text_and_links(html_joined)
        if links:
            seen: set[str] = set()
            link_lines = []
            for label, href in links:
                if href in seen or not href.startswith(('http://', 'https://')):
                    continue
                seen.add(href)
                link_lines.append(f'- {label or "(no text)"}: {href}')
            text = text + '\n\n=== Links ===\n' + '\n'.join(link_lines)
        return text

    def wait_for_email(
        self, sender: str, subject_contains: str, timeout_seconds: int,
        after_epoch: int, poll_interval: float = 5.0,
    ) -> str:
        '''Poll until a matching new mail arrives; return its body. Raises TimeoutError.

        Only mails received after `after_epoch` (unix seconds) qualify, so a previous
        verification mail from yesterday will not be picked up.
        '''
        q = f'from:{sender} subject:({subject_contains}) after:{after_epoch}'
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            res = self._svc.users().messages().list(userId='me', q=q).execute()
            msgs = res.get('messages', [])
            if msgs:
                return self.get_message_body(msgs[0]['id'])
            time.sleep(poll_interval)
        raise TimeoutError(
            f'no mail from {sender} with subject ~ "{subject_contains}" '
            f'within {timeout_seconds}s'
        )

    # --- labels ----------------------------------------------------------

    def ensure_labels(self) -> None:
        '''Create mailprocessor/done + mailprocessor/failed if missing. Cache ids.'''
        existing = self._svc.users().labels().list(userId='me').execute()
        by_name = {lab['name']: lab['id'] for lab in existing.get('labels', [])}
        for needed in (DONE_LABEL, FAILED_LABEL):
            if needed in by_name:
                self._label_ids[needed] = by_name[needed]
            else:
                created = self._svc.users().labels().create(
                    userId='me',
                    body={
                        'name': needed,
                        'labelListVisibility': 'labelShow',
                        'messageListVisibility': 'show',
                    },
                ).execute()
                self._label_ids[needed] = created['id']

    def add_label(self, message_id: str, label_name: str) -> None:
        '''Add label (by name) to message.'''
        label_id = self._label_ids.get(label_name)
        if not label_id:
            self.ensure_labels()
            label_id = self._label_ids[label_name]
        self._svc.users().messages().modify(
            userId='me', id=message_id,
            body={'addLabelIds': [label_id]},
        ).execute()

    def mark_read(self, message_id: str) -> None:
        self._svc.users().messages().modify(
            userId='me', id=message_id,
            body={'removeLabelIds': ['UNREAD']},
        ).execute()

    # --- sending ---------------------------------------------------------

    def send_reply(self, mail_ctx: MailContext, body: str) -> None:
        '''Send a reply within mail_ctx's thread to NOTIFY_TO (hardcoded).'''
        msg = EmailMessage()
        msg['To'] = NOTIFY_TO
        msg['From'] = NOTIFY_TO
        subj = mail_ctx.subject or '(no subject)'
        msg['Subject'] = subj if subj.lower().startswith('re:') else f'Re: {subj}'
        if mail_ctx.message_id_header:
            msg['In-Reply-To'] = mail_ctx.message_id_header
            msg['References'] = mail_ctx.message_id_header
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        self._svc.users().messages().send(
            userId='me',
            body={'raw': raw, 'threadId': mail_ctx.thread_id},
        ).execute()
