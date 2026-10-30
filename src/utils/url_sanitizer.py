import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE_PARAMETER = re.compile(
    r"(?:token|sign|signature|auth|authorization|key|secret|expire|session|cookie)",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def redact_url(url: str) -> str:
    """Remove authentication-like query parameter values from a URL."""
    try:
        parts = urlsplit(url)
        if not parts.query:
            return url
        query = [
            (key, "[REDACTED]" if _SENSITIVE_PARAMETER.search(key) else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )
    except (TypeError, ValueError):
        return "<redacted URL>"


def redact_urls_in_text(text: str) -> str:
    """Sanitize URLs embedded in diagnostic text."""
    return _URL.sub(lambda match: redact_url(match.group(0)), text)
