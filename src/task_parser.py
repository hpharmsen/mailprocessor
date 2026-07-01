'''Extract sender e-mail addresses from tasks.md.'''
import re

EMAIL_RE = re.compile(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}')

# Addresses that appear in tasks.md as reply-targets, not as senders to monitor.
EXCLUDED = frozenset({'hp@harmsen.nl'})


def extract_senders(content: str) -> set[str]:
    '''Return all unique e-mail addresses found in content, minus reply-targets.'''
    return set(EMAIL_RE.findall(content)) - EXCLUDED
