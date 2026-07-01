import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
SRC = ROOT / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# gmail_client reads NOTIFY_TO from the environment at import time; ensure it
# has a value under pytest so the import does not raise KeyError.
os.environ.setdefault('NOTIFY_TO', 'test@example.com')

FIXTURES = Path(__file__).parent / 'fixtures'

# Initialize justlog once for the test session so lg.info(**kwargs) works.
from justlog import setup_logging  # noqa: E402
_LOG_TMP = Path(tempfile.gettempdir()) / 'mailprocessor-tests.log'
setup_logging(str(_LOG_TMP))
