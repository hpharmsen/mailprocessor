'''Per-mail justai agent: chooses one of two tools, performs the action.'''
import io
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from justai import Agent
from justlog import lg
from pypdf import PdfReader

import disk
from browser_tools import (
    BrowserSession,
    make_browser_open, make_browser_snapshot, make_browser_click,
    make_browser_fill, make_browser_fill_credential, make_browser_fill_otp,
    make_browser_download, make_browser_download_url,
)
from gmail_client import (
    GmailClient, MailContext, DONE_LABEL, FAILED_LABEL, NOTIFY_TO,
)


PROMPT_TEMPLATE = '''Je bent een mail-rule-matcher. Hieronder staan de verwerkings-
regels (tasks.md) en daaronder de mail om te verwerken.

Beschikbare tools:

1. read_pdf_excerpt(filename: str, max_chars: int = 2000) -> str
   - Lees tekst uit een PDF-attachment van deze mail.
   - Gebruik dit ALS de placeholder (jaar/maand) niet uit de filename of mail-headers
     af te leiden is. Zoek dan in de PDF naar 'invoice date', 'service period',
     'factuurdatum', 'periode', etc.

2. read_pdf_file(path: str, max_chars: int = 2000) -> str
   - Zelfde als read_pdf_excerpt maar leest een absolute file-pad (bv. een net
     gedownloade PDF). Gebruik dit voor downloads, NIET voor attachments.

3. apply_rule(attachments, reply_body, rule_name) -> str
   - Voor mail-ATTACHMENT flows. attachments: lijst met
     [{{"filename": "<exacte attachment-naam>", "target_path": "<absoluut pad>"}}, ...]
   - target_path moet binnen ALLOWED_ROOTS vallen (Harmsen.nl of Harmsen AI Consultancy).

4. apply_downloaded(source_path, target_path, reply_body, rule_name) -> str
   - Voor BROWSER-DOWNLOAD flows. Verplaatst het bestand op `source_path`
     (uit browser_download) naar `target_path`, stuurt de reply, mark-read, done-label.
   - target_path moet binnen ALLOWED_ROOTS vallen.

5. report_no_action(reason: str) -> str
   - Voor: geen regel match, of een fout waardoor geen actie mogelijk is.

6. browser_open(url) -> snapshot
   - Open een URL in een headless browser. Retourneert de accessibility-tree
     (zichtbare knoppen, links, inputs, headings) als tekst.
7. browser_snapshot() -> snapshot
   - Vraag opnieuw de huidige page-state op (na bv. een navigatie).
8. browser_click(target) -> snapshot
   - Klik op een knop of link met label/zichtbare tekst `target`. Gebruik de
     EXACTE zichtbare tekst (bv. "Inloggen", "Accepteren", "Factuur").
9. browser_fill(target, value)
   - Vul een input geidentificeerd door label/placeholder met `value`.
     Gebruik dit voor NIET-geheime waardes (bv. een 6-cijferige code).
10. browser_fill_credential(target, env_var_name)
    - Vul een input met een geheime waarde uit de genoemde environment variable.
      Gebruik dit voor wachtwoorden of gebruikersnamen — NOOIT een wachtwoord
      letterlijk in een tool-call zetten.
11. browser_fill_otp(code)
    - Voor verificatie/OTP-velden die UIT MEER INVOERVELDEN bestaan (bv. 4 of 6
      losse digit-boxes). Geef de hele code als één string; de tool verdeelt
      de cijfers over de inputs.
12. browser_download(target) -> temp_path
    - Klik `target` en wacht op de resulterende download. Retourneert een
      absoluut temp-pad. Geef dat pad als `source_path` aan apply_downloaded.
13. browser_download_url(url) -> temp_path
    - Direct ophalen van een DOWNLOAD-URL via de browser-sessie (cookies blijven
      behouden). Gebruik dit als `browser_download` faalt met een timeout omdat
      de link een navigatie/redirect naar een PDF-URL is i.p.v. een
      download-event. Lees de `href` uit de snapshot en geef die als `url`.

14. wait_for_email(sender, subject_contains, timeout_seconds) -> body
    - Wacht (poll) op een NIEUWE mail van `sender` met `subject_contains` in
      het onderwerp. Retourneert de body. Gebruik dit voor 2FA-codes — extract
      de code zelf uit de body met regex/inspectie en geef die door aan
      browser_fill.

Workflow:
- Bepaal eerst welke regel matcht (kijk naar sender + onderwerp + attachment-naam).
- Voor attachment-regels: roep apply_rule aan met de juiste target_path.
- Voor browser-regels (Odido): doorloop browser_open -> fill creds -> click ->
  wait_for_email -> fill code -> click -> browser_download -> apply_downloaded.
- Vul placeholders. Volgorde van voorkeur:
    a. Uit attachment-naam of download-filename.
    b. Uit PDF-content via read_pdf_excerpt of read_pdf_file.
    c. NIET uit ontvangstdatum van de mail -- die kan jaren afwijken bij backlog.
- Als iets misgaat (geen knop gevonden, timeout, etc.) roep dan report_no_action
  met een duidelijke uitleg.
- Sluit af met een korte final_answer.

=== tasks.md ===
{tasks_md}

=== mail ===
from: {sender}
date: {date_iso}
subject: {subject}
attachments: {attachments}

=== body (truncated) ===
{body}
'''

BODY_PROMPT_LIMIT = 4000


def build_prompt(tasks_md: str, mail_ctx: MailContext, body: str) -> str:
    excerpt = body.strip()
    if len(excerpt) > BODY_PROMPT_LIMIT:
        excerpt = excerpt[:BODY_PROMPT_LIMIT] + '\n... (truncated)'
    return PROMPT_TEMPLATE.format(
        tasks_md=tasks_md,
        sender=mail_ctx.sender_address,
        date_iso=mail_ctx.date_iso,
        subject=mail_ctx.subject,
        attachments=mail_ctx.attachment_filenames,
        body=excerpt or '(no body)',
    )


def _format_failure_email(mail_ctx: MailContext, rule_name: str, step: str, exc: Exception) -> tuple[str, str]:
    subject = f'[mailprocessor FAIL] {step} -- {rule_name or mail_ctx.sender_address}'
    deeplink = f'https://mail.google.com/mail/u/0/#inbox/{mail_ctx.id}'
    body = (
        f'Mail:    {mail_ctx.subject}\n'
        f'Van:     {mail_ctx.sender_address}\n'
        f'Regel:   {rule_name or "unmatched"}\n'
        f'Stap:    {step}\n'
        f'Fout:    {type(exc).__name__}: {exc}\n\n'
        f'Actie:   verwijder failed-label en run kickstart\n'
        f'Gmail:   {deeplink}\n\n'
        f'-- mailprocessor\n'
    )
    return subject, body


def _format_reply_body(rule_name: str, saved_paths: list[str], date_iso: str) -> str:
    paths_block = '\n'.join(f'  - {p}' for p in saved_paths)
    return (
        f'Verwerkt door regel: {rule_name}\n'
        f'Bestand(en) opgeslagen:\n{paths_block}\n'
        f'Tijdstip: {date_iso}\n\n'
        f'-- mailprocessor\n'
    )


def make_apply_rule(
    gmail: GmailClient,
    mail_ctx: MailContext,
    notifier,
) -> Callable[[list, str, str], str]:
    '''Build the apply_rule tool closed over gmail + mail_ctx + notifier.

    The returned callable is what justai introspects — its name (apply_rule),
    docstring, and type hints become the tool schema. The list[dict] shape for
    `attachments` is described in the prompt because justai's schema-builder
    cannot infer item shape from a generic list annotation.
    '''
    def apply_rule(attachments: list, reply_body: str, rule_name: str) -> str:
        '''Save each attachment to its target_path, send one reply to hp@harmsen.nl,
        mark the mail as read, add the mailprocessor/done label.

        attachments item-shape: {"filename": "<exact attachment filename>",
                                  "target_path": "<absolute path within ALLOWED_ROOTS>"}

        On failure at any step: adds mailprocessor/failed label, sends notification email,
        returns 'failed: <reason>'. On success: returns 'done: <rule_name>'.
        '''
        saved_paths: list[str] = []
        step = 'save'
        try:
            for item in attachments:
                filename = item['filename']
                target = Path(item['target_path'])
                att_id = mail_ctx.attachment_ids.get(filename)
                if not att_id:
                    raise KeyError(f'attachment "{filename}" not on mail')
                data = gmail.download_attachment(mail_ctx.id, att_id)
                status = disk.save_attachment_bytes(target, data)
                saved_paths.append(str(target.resolve()))
                lg.info('attachment saved', filename=filename,
                        target=str(target.resolve()), status=status, rule=rule_name)

            step = 'reply'
            full_reply = reply_body.rstrip() + '\n\n' + _format_reply_body(
                rule_name, saved_paths, mail_ctx.date_iso,
            )
            gmail.send_reply(mail_ctx, full_reply)

            step = 'mark_read'
            gmail.mark_read(mail_ctx.id)

            step = 'add_done'
            gmail.add_label(mail_ctx.id, DONE_LABEL)

            lg.info('rule applied', rule=rule_name, mail_id=mail_ctx.id)
            return f'done: {rule_name}'

        except Exception as exc:
            lg.error(
                f'apply_rule failed at {step}',
                rule=rule_name, mail_id=mail_ctx.id, exc_info=True,
            )
            _fail(gmail, mail_ctx, notifier, rule_name, step, exc)
            return f'failed: {step}: {type(exc).__name__}: {exc}'

    return apply_rule


def make_apply_downloaded(
    gmail: GmailClient,
    mail_ctx: MailContext,
    notifier,
) -> Callable[[str, str, str, str], str]:
    '''apply_downloaded for browser-flow rules. Moves a local file (from
    browser_download) to its final ALLOWED_ROOTS path, then mirrors the
    apply_rule side-effects: reply to NOTIFY_TO, mark read, add done-label.'''
    def apply_downloaded(
        source_path: str, target_path: str, reply_body: str, rule_name: str,
    ) -> str:
        step = 'read_source'
        try:
            src = Path(source_path)
            if not src.is_absolute() or not src.exists():
                raise FileNotFoundError(f'source not found: {source_path}')
            data = src.read_bytes()

            step = 'save'
            target = Path(target_path)
            status = disk.save_attachment_bytes(target, data)
            saved_paths = [str(target.resolve())]
            lg.info('downloaded saved', filename=src.name,
                    target=str(target.resolve()), status=status, rule=rule_name)

            step = 'reply'
            full_reply = reply_body.rstrip() + '\n\n' + _format_reply_body(
                rule_name, saved_paths, mail_ctx.date_iso,
            )
            gmail.send_reply(mail_ctx, full_reply)

            step = 'mark_read'
            gmail.mark_read(mail_ctx.id)

            step = 'add_done'
            gmail.add_label(mail_ctx.id, DONE_LABEL)

            # Cleanup temp file (best-effort)
            try:
                src.unlink()
            except Exception:
                pass

            lg.info('rule applied (downloaded)', rule=rule_name, mail_id=mail_ctx.id)
            return f'done: {rule_name}'

        except Exception as exc:
            lg.error(
                f'apply_downloaded failed at {step}',
                rule=rule_name, mail_id=mail_ctx.id, exc_info=True,
            )
            _fail(gmail, mail_ctx, notifier, rule_name, step, exc)
            return f'failed: {step}: {type(exc).__name__}: {exc}'

    return apply_downloaded


def make_wait_for_email(gmail: GmailClient, after_epoch: int) -> Callable:
    '''Bind wait_for_email to a starting timestamp so old mails are ignored.'''
    def wait_for_email(
        sender: str, subject_contains: str, timeout_seconds: int = 120,
    ) -> str:
        '''Poll Gmail until a new mail from `sender` with `subject_contains` in
        the subject arrives. Returns the body text. Raises after timeout.'''
        try:
            body = gmail.wait_for_email(
                sender=sender,
                subject_contains=subject_contains,
                timeout_seconds=timeout_seconds,
                after_epoch=after_epoch,
            )
            lg.info('wait_for_email hit', sender=sender,
                    subject_contains=subject_contains, body_chars=len(body))
            return body
        except TimeoutError as exc:
            lg.warning('wait_for_email timeout', sender=sender,
                       subject_contains=subject_contains)
            return f'timeout: {exc}'
        except Exception as exc:
            lg.error('wait_for_email failed', error=str(exc), exc_info=True)
            return f'error: {type(exc).__name__}: {exc}'
    return wait_for_email


def read_pdf_file(path: str, max_chars: int = 2000) -> str:
    '''Read text from a PDF at an absolute local path. Returns the first
    max_chars characters of concatenated page text.'''
    try:
        p = Path(path)
        if not p.is_absolute() or not p.exists():
            return f'error: file not found: {path}'
        reader = PdfReader(str(p))
        chunks = []
        running = 0
        for page in reader.pages:
            txt = page.extract_text() or ''
            chunks.append(txt)
            running += len(txt)
            if running >= max_chars:
                break
        full = '\n'.join(chunks)
        excerpt = full[:max_chars]
        lg.info('pdf file excerpt read', path=path,
                chars=len(excerpt), pages=len(reader.pages))
        return excerpt or '(no extractable text)'
    except Exception as exc:
        lg.error('pdf file read failed', path=path, exc_info=True)
        return f'error: {type(exc).__name__}: {exc}'


def make_read_pdf_excerpt(
    gmail: GmailClient,
    mail_ctx: MailContext,
) -> Callable[[str, int], str]:
    '''Build the read_pdf_excerpt tool closed over gmail + mail_ctx.

    The tool downloads the named attachment, extracts text via pypdf,
    and returns the first max_chars characters joined across all pages.
    '''
    def read_pdf_excerpt(filename: str, max_chars: int = 2000) -> str:
        '''Read text from a PDF attachment of this mail. Returns the first
        max_chars characters of concatenated page text. Use this to find
        an invoice date or service period that is not in the filename.'''
        att_id = mail_ctx.attachment_ids.get(filename)
        if not att_id:
            available = list(mail_ctx.attachment_ids)
            return f'error: attachment "{filename}" not found. Available: {available}'
        try:
            data = gmail.download_attachment(mail_ctx.id, att_id)
            reader = PdfReader(io.BytesIO(data))
            chunks = []
            running = 0
            for page in reader.pages:
                txt = page.extract_text() or ''
                chunks.append(txt)
                running += len(txt)
                if running >= max_chars:
                    break
            full = '\n'.join(chunks)
            excerpt = full[:max_chars]
            lg.info('pdf excerpt read', filename=filename,
                    chars=len(excerpt), pages=len(reader.pages))
            return excerpt or '(no extractable text)'
        except Exception as exc:
            lg.error('pdf read failed', filename=filename, exc_info=True)
            return f'error: {type(exc).__name__}: {exc}'

    return read_pdf_excerpt


def report_no_action(reason: str) -> str:
    '''Geen actie voor deze mail (geen regel match, of placeholder onvulbaar).
    Geen labels, mail blijft ongelezen, gelogd voor review.
    Returnt 'no-action: <reason>'.
    '''
    lg.info('no action', reason=reason)
    return f'no-action: {reason}'


def _fail(
    gmail: GmailClient,
    mail_ctx: MailContext,
    notifier,
    rule_name: str,
    step: str,
    exc: Exception,
) -> None:
    '''Label + notify on failure. Swallow secondary errors so the agent can return.'''
    try:
        gmail.add_label(mail_ctx.id, FAILED_LABEL)
    except Exception:
        lg.error('failed to add failed-label', mail_id=mail_ctx.id)
    try:
        subject, body = _format_failure_email(mail_ctx, rule_name, step, exc)
        notifier.email_error(subject, body)
    except Exception:
        lg.error('failed to send error notification', mail_id=mail_ctx.id)


async def process_mail(
    gmail: GmailClient,
    mail_ctx: MailContext,
    tasks_md: str,
    notifier,
    *,
    model: str = 'claude-sonnet-4-6',
) -> str:
    '''Run the justai agent for one mail; returns the agent's answer.'''
    fd, prompt_path_str = tempfile.mkstemp(suffix='.md')
    import os as _os
    _os.close(fd)
    prompt_path = Path(prompt_path_str)
    browser = BrowserSession()
    # Anchor wait_for_email to "now" so we only see verification mails that
    # arrive after this process_mail call started.
    after_epoch = int(time.time())
    try:
        try:
            body = gmail.get_message_body(mail_ctx.id)
        except Exception as exc:
            lg.warning('mail body fetch failed', mail_id=mail_ctx.id, error=str(exc))
            body = ''
        prompt_path.write_text(build_prompt(tasks_md, mail_ctx, body))
        agent = Agent(
            model=model,
            role='Mail rule matcher',
            goal='Bepaal welke regel uit tasks.md van toepassing is en voer 1 tool uit.',
            tools=[
                make_read_pdf_excerpt(gmail, mail_ctx),
                read_pdf_file,
                make_apply_rule(gmail, mail_ctx, notifier),
                make_apply_downloaded(gmail, mail_ctx, notifier),
                report_no_action,
                make_browser_open(browser),
                make_browser_snapshot(browser),
                make_browser_click(browser),
                make_browser_fill(browser),
                make_browser_fill_credential(browser),
                make_browser_fill_otp(browser),
                make_browser_download(browser),
                make_browser_download_url(browser),
                make_wait_for_email(gmail, after_epoch),
            ],
            max_iterations=25,
            verbose=False,
        )
        result = await agent.run_until_done(str(prompt_path))
        return result.answer
    finally:
        prompt_path.unlink(missing_ok=True)
        try:
            browser.close()
        except Exception:
            lg.error('browser session close failed', exc_info=True)
