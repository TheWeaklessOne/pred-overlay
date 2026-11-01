"""Bounded HTTPS JSON transport; credentials never follow cross-origin redirects."""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .errors import AuthenticationError, PayloadError, RateLimitError, TransportError


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def redact(value, secrets=()):
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "[redacted]")
    text = re.sub(r'(?i)Bearer\s+[^\s"\',}]+', "Bearer [redacted]", text)
    return re.sub(r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[redacted JWT]", text)


def validate_token(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 32768
        or any(ord(c) <= 32 or ord(c) > 126 for c in value)
    ):
        raise AuthenticationError(
            "Credential must be a non-empty printable token without whitespace."
        )
    return value


def validate_headers(headers):
    for name, value in headers.items():
        if not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise TransportError("Invalid HTTP header name.")
        if not isinstance(value, str) or any(
            ord(c) < 32 or ord(c) > 255 or ord(c) == 127 for c in value
        ):
            raise TransportError("Invalid HTTP header value; request was not sent.")


def require_redirect_blocker(opener):
    # Concrete urllib openers must block automatic redirects BEFORE sending.
    # Test doubles implement this transport contract without network handlers.
    handlers = getattr(opener, "handlers", None)
    if isinstance(opener, urllib.request.OpenerDirector) and isinstance(handlers, list):
        if any(
            isinstance(h, urllib.request.HTTPRedirectHandler) and type(h) is not NoRedirect
            for h in handlers
        ):
            raise TransportError(
                "Injected urllib opener must use NoRedirect; request was not sent."
            )


def origin(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise TransportError("API targets must be HTTPS origins without embedded credentials.")
    return parsed.scheme, parsed.hostname.lower(), parsed.port or 443


def retry_seconds(value):
    if not value:
        return None
    if str(value).isdigit():
        return float(value)
    try:
        at = parsedate_to_datetime(value)
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return max(0.0, (at - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


class JsonTransport:
    def __init__(
        self,
        base,
        *,
        timeout=20,
        delay=0.5,
        retries=2,
        max_retry_delay=30,
        same_origin_redirects=False,
        opener=None,
    ):
        self.base = base.rstrip("/")
        self.origin = origin(base)
        self.timeout, self.delay = timeout, delay
        self.retries, self.max_retry_delay = retries, max_retry_delay
        self.same_origin_redirects = same_origin_redirects
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.last_request = 0.0
        self.request_count = 0
        self.redirect_count = 0

    def request(self, path, payload=None, *, headers=None, secrets=()):
        if not path.startswith("/") or path.startswith("//"):
            raise TransportError("API request must use a provider-relative path.")
        url = self.base + path
        if origin(url) != self.origin:
            raise TransportError("Unexpected API origin.")
        request_headers = {"Accept": "application/json", "User-Agent": "pred-overlay/0.1"}
        request_headers.update(headers or {})
        validate_headers(request_headers)
        require_redirect_blocker(self.opener)
        data = None
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        attempt = redirects = 0
        while True:
            pause = self.delay - (time.monotonic() - self.last_request)
            if pause > 0:
                time.sleep(pause)
            self.last_request = time.monotonic()
            self.request_count += 1
            req = urllib.request.Request(url, data=data, headers=request_headers)
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    # Also defend against a caller-supplied opener that follows redirects.
                    if hasattr(response, "geturl") and origin(response.geturl()) != self.origin:
                        raise TransportError("API response changed origin unexpectedly.")
                    raw = response.read(32 * 1024 * 1024 + 1)
                if len(raw) > 32 * 1024 * 1024:
                    raise PayloadError("API response exceeds the 32 MiB limit.")
                try:
                    return json.loads(raw)
                except (ValueError, UnicodeError):
                    raise PayloadError("API returned invalid JSON.") from None
            except urllib.error.HTTPError as exc:
                location = exc.headers.get("Location") if exc.headers else None
                if exc.code in (307, 308) and location and self.same_origin_redirects:
                    target = urllib.parse.urljoin(url, location)
                    if origin(target) != self.origin:
                        raise TransportError(
                            "Cross-origin API redirect refused; credentials were not forwarded.",
                            status=exc.code,
                        ) from None
                    if (
                        urllib.parse.urlsplit(target).path
                        != urllib.parse.urlsplit(self.base + path).path
                    ):
                        raise TransportError(
                            "API redirect changed the operation path; credentials were not forwarded.",
                            status=exc.code,
                        ) from None
                    if redirects >= 3:
                        raise TransportError(
                            "API redirect limit exceeded.", status=exc.code
                        ) from None
                    redirects += 1
                    self.redirect_count += 1
                    url = target
                    continue
                if exc.code in (401, 403):
                    raise AuthenticationError(
                        "HTTP %d: authentication missing, expired or not permitted." % exc.code,
                        status=exc.code,
                    ) from None
                if exc.code in (429, 502, 503, 504):
                    pause = retry_seconds(exc.headers.get("Retry-After")) if exc.headers else None
                    pause = max(2**attempt, pause or 0)
                    if attempt < self.retries and pause <= self.max_retry_delay:
                        attempt += 1
                        time.sleep(pause)
                        continue
                    cls = RateLimitError if exc.code == 429 else TransportError
                    raise cls(
                        "HTTP %d: temporary API failure; retry later." % exc.code,
                        status=exc.code,
                        retry_after=pause,
                    ) from None
                raise TransportError("API returned HTTP %d." % exc.code, status=exc.code) from None
            except (ValueError, UnicodeError):
                raise TransportError(
                    "Request construction or header validation failed; no credential details are reported."
                ) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < self.retries:
                    time.sleep(2**attempt)
                    attempt += 1
                    continue
                raise TransportError("Cannot reach API: " + redact(exc, secrets)) from None
