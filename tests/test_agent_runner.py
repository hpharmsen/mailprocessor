from pathlib import Path
from unittest.mock import MagicMock

import pytest

import agent_runner
import disk
from gmail_client import MailContext, DONE_LABEL, FAILED_LABEL


def _ctx(tmp_path, filename='Monthly Report May 2026.pdf'):
    return MailContext(
        id='mid-1', thread_id='tid-1', message_id_header='<x@y>',
        sender_address='pelle.schlichting@leap24.eu', sender_name='Pelle',
        subject='Monthly Report May 2026', date_iso='Fri, 12 Jun 2026 09:00:00 +0200',
        attachment_filenames=[filename],
        attachment_ids={filename: 'att-1'},
    )


@pytest.fixture
def mock_gmail():
    g = MagicMock()
    g.download_attachment.return_value = b'PDF-BYTES'
    return g


@pytest.fixture
def mock_notifier():
    return MagicMock()


@pytest.fixture
def relax_allowed_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(disk, 'ALLOWED_ROOTS', (tmp_path.resolve(),))


def test_apply_rule_calls_steps_in_order(
    mock_gmail, mock_notifier, relax_allowed_roots, tmp_path,
):
    ctx = _ctx(tmp_path)
    calls = []
    mock_gmail.send_reply.side_effect = lambda *a, **kw: calls.append('reply')
    mock_gmail.mark_read.side_effect = lambda *a: calls.append('mark_read')

    def add_label_side(_mid, label):
        calls.append(('add_label', label))
    mock_gmail.add_label.side_effect = add_label_side

    def download_side(*a, **kw):
        calls.append('download')
        return b'PDF-BYTES'
    mock_gmail.download_attachment.side_effect = download_side

    target = tmp_path / 'out.pdf'
    apply_rule = agent_runner.make_apply_rule(mock_gmail, ctx, mock_notifier)
    result = apply_rule(
        [{'filename': ctx.attachment_filenames[0], 'target_path': str(target)}],
        'body', 'leap',
    )
    assert calls == [
        'download', 'reply', 'mark_read', ('add_label', DONE_LABEL),
    ]
    assert result == 'done: leap'
    assert target.read_bytes() == b'PDF-BYTES'


def test_apply_rule_failure_at_save_adds_failed_label_emails_and_returns_failed(
    mock_gmail, mock_notifier, tmp_path, monkeypatch,
):
    # ALLOWED_ROOTS unchanged — write to /tmp will trigger PathNotAllowedError
    ctx = _ctx(tmp_path)
    apply_rule = agent_runner.make_apply_rule(mock_gmail, ctx, mock_notifier)
    result = apply_rule(
        [{'filename': ctx.attachment_filenames[0],
          'target_path': '/tmp/not-in-allowed-roots.pdf'}],
        'body', 'leap',
    )
    mock_gmail.add_label.assert_called_once_with(ctx.id, FAILED_LABEL)
    mock_notifier.email_error.assert_called_once()
    mock_gmail.send_reply.assert_not_called()
    mock_gmail.mark_read.assert_not_called()
    assert result.startswith('failed: save:')


def test_apply_rule_failure_at_reply_does_not_mark_read(
    mock_gmail, mock_notifier, relax_allowed_roots, tmp_path,
):
    ctx = _ctx(tmp_path)
    mock_gmail.send_reply.side_effect = RuntimeError('smtp down')
    apply_rule = agent_runner.make_apply_rule(mock_gmail, ctx, mock_notifier)
    result = apply_rule(
        [{'filename': ctx.attachment_filenames[0],
          'target_path': str(tmp_path / 'x.pdf')}],
        'body', 'leap',
    )
    mock_gmail.mark_read.assert_not_called()
    mock_gmail.add_label.assert_called_once_with(ctx.id, FAILED_LABEL)
    mock_notifier.email_error.assert_called_once()
    assert result.startswith('failed: reply:')


def test_apply_rule_multi_attachment_one_reply(
    mock_gmail, mock_notifier, relax_allowed_roots, tmp_path,
):
    ctx = MailContext(
        id='m1', thread_id='t1', message_id_header='<x@y>',
        sender_address='a@b.com', sender_name='', subject='Two', date_iso='now',
        attachment_filenames=['a.pdf', 'b.pdf'],
        attachment_ids={'a.pdf': 'att-a', 'b.pdf': 'att-b'},
    )
    apply_rule = agent_runner.make_apply_rule(mock_gmail, ctx, mock_notifier)
    result = apply_rule(
        [
            {'filename': 'a.pdf', 'target_path': str(tmp_path / 'a.pdf')},
            {'filename': 'b.pdf', 'target_path': str(tmp_path / 'b.pdf')},
        ],
        'body', 'r',
    )
    assert mock_gmail.download_attachment.call_count == 2
    assert mock_gmail.send_reply.call_count == 1
    assert result == 'done: r'


def test_apply_rule_unknown_attachment_filename_fails(
    mock_gmail, mock_notifier, relax_allowed_roots, tmp_path,
):
    ctx = _ctx(tmp_path)
    apply_rule = agent_runner.make_apply_rule(mock_gmail, ctx, mock_notifier)
    result = apply_rule(
        [{'filename': 'unknown.pdf', 'target_path': str(tmp_path / 'unknown.pdf')}],
        'body', 'leap',
    )
    assert result.startswith('failed: save:')
    mock_gmail.add_label.assert_called_once_with(ctx.id, FAILED_LABEL)


def test_report_no_action_returns_message_does_nothing():
    result = agent_runner.report_no_action('no match')
    assert result == 'no-action: no match'


def _make_pdf_bytes(text: str) -> bytes:
    '''Generate a minimal one-page PDF containing `text` via pypdf.'''
    from pypdf import PdfWriter
    from pypdf.generic import RectangleObject
    w = PdfWriter()
    w.add_blank_page(width=612, height=792)
    # Inject a small content stream so extract_text finds something. Easiest:
    # use pypdf's add_page from a pre-made minimal PDF. Instead, just craft
    # a content stream by hand on the blank page.
    from pypdf.generic import ContentStream, NameObject, DecodedStreamObject
    page = w.pages[0]
    content = DecodedStreamObject()
    # BT/ET = text object; Tf = font; Tj = show string. Use built-in Helvetica.
    safe = text.replace('(', r'\(').replace(')', r'\)')
    content.set_data(
        f'BT /F1 12 Tf 72 720 Td ({safe}) Tj ET'.encode('latin-1')
    )
    # Attach font + content
    from pypdf.generic import DictionaryObject, ArrayObject, IndirectObject
    font = DictionaryObject({
        NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'),
        NameObject('/BaseFont'): NameObject('/Helvetica'),
    })
    resources = DictionaryObject({
        NameObject('/Font'): DictionaryObject({NameObject('/F1'): font}),
    })
    page[NameObject('/Resources')] = resources
    page[NameObject('/Contents')] = content
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


import io  # for the helper above


def test_read_pdf_excerpt_extracts_text(mock_gmail, tmp_path):
    pdf_bytes = _make_pdf_bytes('Invoice date: 2026-04-15 -- amount EUR 99')
    mock_gmail.download_attachment.return_value = pdf_bytes
    ctx = MailContext(
        id='m1', thread_id='t', message_id_header='<x@y>',
        sender_address='a@b.com', sender_name='', subject='inv',
        date_iso='', attachment_filenames=['Invoice-X.pdf'],
        attachment_ids={'Invoice-X.pdf': 'att-1'},
    )
    tool = agent_runner.make_read_pdf_excerpt(mock_gmail, ctx)
    result = tool('Invoice-X.pdf')
    assert '2026' in result
    assert 'Invoice date' in result


def test_read_pdf_excerpt_unknown_filename_returns_error(mock_gmail, tmp_path):
    ctx = MailContext(
        id='m1', thread_id='t', message_id_header='<x@y>',
        sender_address='a@b.com', sender_name='', subject='inv',
        date_iso='', attachment_filenames=['only.pdf'],
        attachment_ids={'only.pdf': 'att-1'},
    )
    tool = agent_runner.make_read_pdf_excerpt(mock_gmail, ctx)
    result = tool('missing.pdf')
    assert result.startswith('error:')
    assert 'only.pdf' in result
    mock_gmail.download_attachment.assert_not_called()


def test_read_pdf_excerpt_respects_max_chars(mock_gmail, tmp_path):
    pdf_bytes = _make_pdf_bytes('A' * 100)
    mock_gmail.download_attachment.return_value = pdf_bytes
    ctx = MailContext(
        id='m1', thread_id='t', message_id_header='<x@y>',
        sender_address='a@b.com', sender_name='', subject='inv',
        date_iso='', attachment_filenames=['x.pdf'],
        attachment_ids={'x.pdf': 'att-1'},
    )
    tool = agent_runner.make_read_pdf_excerpt(mock_gmail, ctx)
    result = tool('x.pdf', max_chars=20)
    assert len(result) <= 20


def test_process_mail_cleans_tempfile_even_on_exception(monkeypatch, mock_gmail, mock_notifier, tmp_path):
    ctx = _ctx(tmp_path)
    created_paths = []

    class _BoomAgent:
        def __init__(self, *a, **kw):
            pass
        async def run_until_done(self, prompt_path):
            created_paths.append(prompt_path)
            raise RuntimeError('agent boom')

    monkeypatch.setattr(agent_runner, 'Agent', _BoomAgent)

    import asyncio
    with pytest.raises(RuntimeError):
        asyncio.run(agent_runner.process_mail(mock_gmail, ctx, 'tasks', mock_notifier))

    assert created_paths
    assert not Path(created_paths[0]).exists()


def test_process_mail_passes_tools_and_returns_answer(monkeypatch, mock_gmail, mock_notifier, tmp_path):
    ctx = _ctx(tmp_path)
    captured = {}

    class _StubResult:
        answer = 'done: leap'

    class _StubAgent:
        def __init__(self, *, model, role, goal, tools, max_iterations, verbose):
            captured['tools'] = tools
        async def run_until_done(self, prompt_path):
            assert Path(prompt_path).exists()
            return _StubResult()

    monkeypatch.setattr(agent_runner, 'Agent', _StubAgent)

    import asyncio
    answer = asyncio.run(agent_runner.process_mail(mock_gmail, ctx, 'tasks', mock_notifier))
    assert answer == 'done: leap'
    names = [t.__name__ for t in captured['tools']]
    assert names == [
        'read_pdf_excerpt', 'read_pdf_file',
        'apply_rule', 'apply_downloaded', 'report_no_action',
        'browser_open', 'browser_snapshot', 'browser_click',
        'browser_fill', 'browser_fill_credential', 'browser_fill_otp',
        'browser_download', 'browser_download_url', 'wait_for_email',
    ]
    assert agent_runner.report_no_action in captured['tools']
    assert agent_runner.read_pdf_file in captured['tools']
