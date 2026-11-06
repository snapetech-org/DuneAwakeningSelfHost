"""Validate HTTP transport URLs before passing operator input to urllib."""
import urllib.parse


def require_http_url(url):
    if not isinstance(url, str) or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise ValueError("HTTP URL must not contain whitespace or control characters")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must use HTTP or HTTPS with a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("HTTP credentials must be passed in headers, not in the URL")
    # Accessing port also rejects malformed and out-of-range values.
    if parsed.port == 0:
        raise ValueError("HTTP port must be positive")
    return url
