from pathlib import Path

import pytest

import disk


@pytest.fixture
def relax_allowed_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(disk, 'ALLOWED_ROOTS', (tmp_path.resolve(),))


def test_save_creates_parent_dirs(relax_allowed_roots, tmp_path):
    target = tmp_path / 'a' / 'b' / 'file.pdf'
    assert disk.save_attachment_bytes(target, b'data') == 'saved'
    assert target.read_bytes() == b'data'


def test_save_identical_bytes_is_noop(relax_allowed_roots, tmp_path):
    target = tmp_path / 'x.pdf'
    target.write_bytes(b'same')
    assert disk.save_attachment_bytes(target, b'same') == 'identical-already-present'


def test_save_different_bytes_refuses_overwrite(relax_allowed_roots, tmp_path):
    target = tmp_path / 'x.pdf'
    target.write_bytes(b'old')
    with pytest.raises(disk.TargetExistsError):
        disk.save_attachment_bytes(target, b'new')


def test_save_rejects_relative_path(relax_allowed_roots):
    with pytest.raises(ValueError):
        disk.save_attachment_bytes(Path('relative.pdf'), b'data')


def test_save_rejects_path_outside_allowed_roots(tmp_path):
    with pytest.raises(disk.PathNotAllowedError):
        disk.save_attachment_bytes(Path('/etc/passwd-test'), b'x')


def test_save_rejects_traversal_attempt(relax_allowed_roots, tmp_path):
    target = tmp_path / 'a' / '..' / '..' / 'etc' / 'evil'
    with pytest.raises(disk.PathNotAllowedError):
        disk.save_attachment_bytes(target, b'x')


@pytest.mark.skipif(not disk._CASE_INSENSITIVE, reason='darwin-only')
def test_save_accepts_different_casing_on_macos(tmp_path, monkeypatch):
    # ALLOWED_ROOTS = /Foo/Bar; target uses /foo/bar — must be accepted on macOS.
    monkeypatch.setattr(disk, 'ALLOWED_ROOTS', (tmp_path / 'Mixed-Case-Root',))
    (tmp_path / 'Mixed-Case-Root').mkdir()
    target = tmp_path / 'mixed-case-root' / 'sub' / 'x.pdf'
    assert disk.save_attachment_bytes(target, b'data') == 'saved'
