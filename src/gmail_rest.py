"""The one Gmail REST exception: attachment bytes and the Message-ID header.

The Gmail MCP server has no attachment download and refuses RAW for mails with
attachments. This module is plain deterministic code with its own OAuth client
(without the agent flag); the agent never calls it and never sees its output.
"""

import base64
import json
import urllib.error
import urllib.request

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from gmail_mcp import retry

REST_URL = 'https://gmail.googleapis.com/gmail/v1/users/me/messages/'
TIMEOUT_SECONDS = 60


def _is_transient(exc: Exception) -> bool:
    """429, 5xx and network errors; other HTTP errors are permanent."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, OSError)


class GmailRest:
    def __init__(self, creds: Credentials):
        self.creds = creds

    def _get(self, path: str) -> dict:
        def once() -> dict:
            if not self.creds.valid:
                self.creds.refresh(Request())
            req = urllib.request.Request(REST_URL + path, headers={'Authorization': f'Bearer {self.creds.token}'})
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                return json.load(resp)

        return retry(f'gmail rest {path.split("?")[0]}', once, _is_transient)

    def attachment(self, message_id: str, attachment_id: str) -> bytes:
        data = self._get(f'{message_id}/attachments/{attachment_id}')['data']
        return base64.urlsafe_b64decode(data + '=' * (-len(data) % 4))

    def message_id(self, message_id: str) -> str | None:
        """The raw Message-ID header value, or None."""
        headers = self._get(f'{message_id}?format=metadata&metadataHeaders=Message-ID')['payload'].get('headers', [])
        return next((h['value'] for h in headers if h['name'].lower() == 'message-id'), None)
