# Gmail MCP-server: setup en gedrag

Vastgesteld op 27 november 2026 met `scripts/mcp_probe.py` tegen
`https://gmailmcp.googleapis.com/mcp/v1self`, account hp@harmsen.nl, project
`mailprocessor-499408`.

## Setup

1. APIs aanzetten:

   ```bash
   gcloud services enable gmail.googleapis.com gmailmcp.googleapis.com --project=mailprocessor-499408
   ```

2. Twee Desktop OAuth-clients via
   https://console.cloud.google.com/apis/credentials?project=mailprocessor-499408,
   beide met scope `gmail.modify`:
   - `credentials_mcp.json`: vink **"Use this client for an AI-powered agent"**
     aan. Zonder die vlag geeft elke `tools/call` HTTP 401 `invalid_token`,
     terwijl `tools/list` wel werkt.
   - `credentials.json`: zonder agent-vlag, voor `src/gmail_rest.py`.

3. Admin console (https://admin.google.com) > Agents > Toegangsbeheer voor agents
   > Instellingen > Gebruikersbeoordeling voor externe agents:
   **Toegang tot externe gegevens** op **Toestaan**. De wijziging werkte na
   ongeveer 3 minuten. Staat het op "Goedkeuring van gebruiker vereisen", dan:
   - laat `search_threads` elke thread van een externe afzender weg, met
     `omittedContent.omittedReasonCounts[].reason = DATA_PROTECTION_POLICY`.
     Alleen zelf verstuurde mail komt terug; `THREAD_VIEW_IDS_ONLY` geeft wel
     de id's;
   - geeft `get_message` (elk formaat, ook `METADATA_ONLY`): "This action
     requires user confirmation, which this client does not support."

   De samenwerkingsinstellingen (mail sturen, delen) mogen op goedkeuring
   blijven: mailprocessor gebruikt die acties niet.

4. `uv run src/main.py setup-auth` doet beide OAuth-flows.

## Wat de server wel en niet kan

| Vraag | Uitkomst |
| --- | --- |
| Download-tool voor bijlagen | Bestaat niet, ook niet op `/mcp/v1` |
| `get_message` met `RAW` | Geweigerd voor mails met bijlagen: "RAW format is not supported for messages with attachments or non-body parts" |
| Bijlage-metadata | `FULL_CONTENT` en `PLAIN_TEXT` geven per bijlage `filename`, `id`, `mimeType`, `partId` |
| Bijlage-`id` | Is de REST-`attachmentId`; werkt in `messages.attachments.get`. Verschilt per call, dus direct gebruiken |
| Bytes via REST | Identiek aan de eerder opgeslagen PDF (Anthropic, 33.760 bytes, sha256 `b3ac5c15...`) |
| `Message-ID`-header | Niet in `FULL_CONTENT`; via REST `messages.get?format=metadata&metadataHeaders=Message-ID` |
| `structured_content` | Gevuld bij succes; bij `is_error` staat de melding als tekst in `content` |
| `search_threads` standaardview | `THREAD_VIEW_MINIMAL`: per bericht `id`, `sender` (kaal adres), `labelIds` (id's zoals `Label_53`), `date` (ISO, `Z`), plus `messageCount` op de thread |
| `label:mailprocessor/done` in de query | Werkt zoals geschreven, net als `-label:...` |
| `after:<epoch>` in de query | Werkt (op seconden) |
| `list_labels` | `{'labels': [{'labelId', 'name', ...}]}` |
| `create_label` | Geeft `labelId` terug; `mailprocessor/skipped` werd `Label_67` |
| `update_message_labels` | `addLabelIds` / `removeLabelIds`, ook `UNREAD`; antwoord bevat de nieuwe `labelIds` |

## Quota

Standard-tier: 5.000 requests per dag, `search_threads` telt als 10. Een run
zonder nieuwe mail kost één `list_labels` plus één `search_threads`.
