"""Diagnose tool for the Gmail MCP server: prints raw tool responses as JSON.

uv run scripts/mcp_probe.py tools
uv run scripts/mcp_probe.py call <tool> '<json arguments>'
uv run scripts/mcp_probe.py raw <message_id>      # attachments from RAW MIME
"""

import asyncio
import base64
import email
import hashlib
import json
import sys
from email import policy
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

ROOT = Path(__file__).resolve().parent.parent
URL = 'https://gmailmcp.googleapis.com/mcp/v1self'
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']


def token() -> str:
    """Access token from token_mcp.json; runs the browser flow when missing."""
    path = ROOT / 'token_mcp.json'
    creds = Credentials.from_authorized_user_file(str(path), SCOPES) if path.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(ROOT / 'credentials_mcp.json'), SCOPES)
        creds = flow.run_local_server(port=0)
    path.write_text(creds.to_json())
    path.chmod(0o600)
    return creds.token


def payload(result) -> object:
    """structured_content when present, else the text content parsed as JSON."""
    if result.structured_content is not None:
        return result.structured_content
    texts = [c.text for c in result.content if c.type == 'text']
    try:
        return json.loads(''.join(texts))
    except json.JSONDecodeError:
        return texts


def attachments_from_raw(raw: str) -> list[dict]:
    """Walk the MIME tree with Gmail partId numbering and describe each attachment."""
    try:
        data = base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))
        if b'\n' not in data[:2000]:
            raise ValueError
    except ValueError:
        data = raw.encode()
    msg = email.message_from_bytes(data, policy=policy.default)
    out = [{'raw_bytes': len(data), 'message_id': msg['Message-ID']}]

    def walk(part, pid):
        if part.is_multipart():
            for i, sub in enumerate(part.iter_parts()):
                walk(sub, f'{pid}.{i}' if pid else str(i))
        elif part.get_filename():
            body = part.get_payload(decode=True) or b''
            out.append(
                {
                    'partId': pid,
                    'filename': part.get_filename(),
                    'bytes': len(body),
                    'sha256': hashlib.sha256(body).hexdigest(),
                }
            )

    walk(msg, '')
    return out


async def main(argv: list[str]) -> None:
    http = create_mcp_http_client(headers={'Authorization': f'Bearer {token()}'})
    async with http, Client(streamable_http_client(URL, http_client=http)) as client:
        cmd = argv[0] if argv else 'tools'
        if cmd == 'tools':
            tools = (await client.list_tools()).tools
            out = [{'name': t.name, 'input': t.input_schema} for t in tools]
        elif cmd == 'call':
            res = await client.call_tool(argv[1], json.loads(argv[2]) if len(argv) > 2 else {})
            out = {'is_error': res.is_error, 'payload': payload(res)}
        elif cmd == 'raw':
            res = await client.call_tool('get_message', {'messageId': argv[1], 'messageFormat': 'RAW'})
            out = attachments_from_raw(payload(res)['raw'])
        else:
            sys.exit(__doc__)
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1:]))
