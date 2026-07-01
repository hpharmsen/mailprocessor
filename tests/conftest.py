import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).parent / 'fixtures'

# Initialize justlog once for the test session so lg.info(**kwargs) works.
from justlog import setup_logging  # noqa: E402
_LOG_TMP = Path(tempfile.gettempdir()) / 'mailprocessor-tests.log'
setup_logging(str(_LOG_TMP))
