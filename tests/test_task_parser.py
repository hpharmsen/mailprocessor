from conftest import FIXTURES

import task_parser


def test_extract_senders_from_tasks_md():
    content = (FIXTURES / 'tasks_sample.md').read_text()
    assert task_parser.extract_senders(content) == {
        'pelle.schlichting@leap24.eu',
        'invoice+statements@mail.anthropic.com',
    }


def test_extract_senders_empty_file():
    assert task_parser.extract_senders('') == set()


def test_extract_senders_ignores_non_email_text():
    assert task_parser.extract_senders('LEAP regel zonder adres') == set()


def test_extract_senders_dedupes():
    content = 'a@b.com\n... a@b.com komt weer ...\n'
    assert task_parser.extract_senders(content) == {'a@b.com'}


def test_extract_senders_handles_plus_and_dots():
    content = 'foo.bar+baz@example.co.uk'
    assert task_parser.extract_senders(content) == {'foo.bar+baz@example.co.uk'}


def test_extract_senders_excludes_reply_target():
    content = 'Stuur hp@harmsen.nl een reply.\nVan: a@b.com\n'
    assert task_parser.extract_senders(content) == {'a@b.com'}


def test_extract_senders_on_real_tasks_md_excludes_self():
    content = (FIXTURES.parent.parent / 'tasks.md').read_text()
    senders = task_parser.extract_senders(content)
    assert 'hp@harmsen.nl' not in senders
    assert 'pelle.schlichting@leap24.eu' in senders
    assert 'invoice+statements@mail.anthropic.com' in senders
