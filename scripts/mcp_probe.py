"""Diagnose tool for the Gmail MCP server: prints raw tool responses as JSON.

uv run scripts/mcp_probe.py tools
uv run scripts/mcp_probe.py call <tool> '<json arguments>'
uv run scripts/mcp_probe.py attachments <message_id>  # size + sha256 via gmail_rest
"""

import asyncio
import hashlib
import json
import sys
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


def attachments(message_id: str, meta: dict) -> list[dict]:
    """Download each attachment the way mailprocessor does (gmail_rest) and describe it."""
    sys.path.insert(0, str(ROOT / 'src'))
    from gmail_mcp import load_credentials
    from gmail_rest import GmailRest

    rest = GmailRest(load_credentials(ROOT / 'token.json'))
    out = [{'message_id': rest.message_id(message_id)}]
    for a in meta.get('attachments', []):
        data = rest.attachment(message_id, a['id'])
        out.append({'filename': a['filename'], 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    return out


async def main(argv: list[str]) -> None:
    http = create_mcp_http_client(headers={'Authorization': f'Bearer {token()}'})
    async with http, Client(streamable_http_client(URL, http_client=http, max_sse_event_size=None)) as client:
        cmd = argv[0] if argv else 'tools'
        if cmd == 'tools':
            tools = (await client.list_tools()).tools
            out = [{'name': t.name, 'input': t.input_schema} for t in tools]
        elif cmd == 'call':
            res = await client.call_tool(argv[1], json.loads(argv[2]) if len(argv) > 2 else {})
            out = {'is_error': res.is_error, 'payload': payload(res)}
        elif cmd == 'attachments':
            res = await client.call_tool('get_message', {'messageId': argv[1], 'messageFormat': 'PLAIN_TEXT'})
            out = attachments(argv[1], payload(res))
        else:
            sys.exit(__doc__)
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1:]))
