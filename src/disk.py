'''Safe attachment storage with ALLOWED_ROOTS guard and content-hash idempotency.'''
import sys
from pathlib import Path

ALLOWED_ROOTS: tuple[Path, ...] = (
    Path('/Users/hp/Harmsen.nl').resolve(),
    Path('/Users/hp/Harmsen AI Consultancy').resolve(),
)

# macOS APFS is case-insensitive; matching the filesystem behaviour avoids
# spurious PathNotAllowedError when tasks.md uses different casing than the on-disk root.
_CASE_INSENSITIVE = sys.platform == 'darwin'


class PathNotAllowedError(Exception):
    '''Target path is outside ALLOWED_ROOTS.'''


class TargetExistsError(Exception):
    '''Target exists with different content; refuse to overwrite.'''


def _is_under(target: Path, root: Path) -> bool:
    t, r = str(target), str(root)
    if _CASE_INSENSITIVE:
        t, r = t.lower(), r.lower()
    return t == r or t.startswith(r.rstrip('/') + '/')


def save_attachment_bytes(target: Path, data: bytes) -> str:
    '''Save data to target. Returns status string.

    - 'saved'                       — new file written
    - 'identical-already-present'   — file exists with identical bytes (no-op)
    Raises PathNotAllowedError, TargetExistsError, ValueError.
    '''
    if not target.is_absolute():
        raise ValueError('target must be absolute')
    resolved = target.resolve()
    if not any(_is_under(resolved, root) for root in ALLOWED_ROOTS):
        raise PathNotAllowedError(f'{resolved} outside ALLOWED_ROOTS')
    if resolved.exists():
        if resolved.read_bytes() == data:
            return 'identical-already-present'
        raise TargetExistsError(str(resolved))
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_bytes(data)
    return 'saved'
