"""Pred.gg with the player's own account: sign-in, token refresh and authorized queries.

The app is Pred.gg's public OAuth client "Pred Overlay (public)": no client
secret exists, so the sign-in uses PKCE. Pred.gg sends the browser back to
``http://127.0.0.1:47833/callback`` (registered for this client). The tokens are
the user's and stay in the session file (auth.SessionStore); nothing here logs
or prints them.
"""

import base64
import hashlib
import json
import math
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .auth import AuthError, SessionStore, oauth_login
from .errors import AuthenticationError, PayloadError, TransportError
from .transport import https_opener

BASE = "https://pred.gg"
CLIENT_ID = "g8ztejg6qe35qv8simfq4xizratql4z5"
AUTHORIZE = BASE + "/api/oauth2/authorize"
TOKEN = BASE + "/api/oauth2/token"
PORT = 47833
SCOPES = "offline_access profile"
REFRESH_WITHIN = 120  # seconds of validity left that already call for a refresh
TIMEOUT = 15
_LOCK = threading.Lock()  # one refresh at a time: a refresh token is good once
_OWN = {}  # session path -> mtime of this process's last write (tracker.check_session)


def _wrote(path):
    try:
        _OWN[str(path)] = os.stat(path).st_mtime_ns
    except OSError:
        pass


def own_write(path):
    """The session file's mtime after this process last wrote it, or None."""
    return _OWN.get(str(path))


def pkce():
    """(verifier, S256 challenge) for one sign-in."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(redirect_uri, state, challenge):
    return (
        AUTHORIZE
        + "?"
        + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": CLIENT_ID,
                "redirect_uri": redirect_uri,
                "scope": SCOPES,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )


def _post_form(form, *, opener=None, timeout=TIMEOUT):
    """POST to the token endpoint; the JSON answer, or a credential-free error."""
    opener = opener or https_opener()
    request = urllib.request.Request(
        TOKEN,
        data=urllib.parse.urlencode(form).encode("ascii"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": f"pred-overlay/{__version__} (+https://github.com/TheWeaklessOne/pred-overlay)",
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403):
            # invalid_grant and friends: the code or refresh token is spent or refused.
            # No status: a refusal here is about the session, whatever the code says.
            raise AuthenticationError("Pred.gg refused the sign-in (HTTP %d)." % exc.code) from None
        raise TransportError(
            "Pred.gg's token endpoint returned HTTP %d." % exc.code, status=exc.code
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Which failure, by its class alone (SSLCertVerificationError, gaierror, timeout):
        # the text may hold what must not be logged.
        reason = getattr(exc, "reason", exc)
        if not isinstance(reason, BaseException):  # an SSLError's reason is a text
            reason = exc
        error = TransportError(f"Cannot reach Pred.gg's token endpoint ({type(reason).__name__}).")
        error.cause = type(reason).__name__
        raise error from None
    if len(raw) > 1024 * 1024:
        raise PayloadError("Pred.gg's token answer is too large.")
    try:
        answer = json.loads(raw)
    except (ValueError, UnicodeError):
        raise PayloadError("Pred.gg's token answer is not JSON.") from None
    if not isinstance(answer, dict):
        raise PayloadError("Pred.gg's token answer is not an object.")
    return answer


def record(answer, now=None, previous=None):
    """Token answer -> the session record; the old refresh token stays if none is new."""
    now = time.time() if now is None else now
    access = answer.get("access_token")
    refresh = answer.get("refresh_token") or (previous or {}).get("refresh_token")
    lifetime = answer.get("expires_in")
    if (
        not isinstance(access, str)
        or not access
        or not isinstance(refresh, str)
        or not refresh
        or type(lifetime) not in (int, float)
        or not math.isfinite(lifetime)
        or lifetime <= 0
    ):
        raise PayloadError("Pred.gg's token answer lacks a token or its lifetime.")
    return {"access_token": access, "refresh_token": refresh, "expires_at": int(now + lifetime)}


def sign_in(session_path, *, open_browser=True, on_ready=None, cancel=None, port=PORT, post=None):
    """Sign in on Pred.gg's own page in the browser and store the tokens; returns a label
    for the account (the sign-in window says "Signed in as ...")."""
    post = post or _post_form
    verifier, challenge = pkce()

    def exchange(code, redirect_uri):
        return record(
            post(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": CLIENT_ID,
                    "code_verifier": verifier,
                }
            )
        )

    tokens = oauth_login(
        lambda redirect_uri, state: authorize_url(redirect_uri, state, challenge),
        exchange,
        open_browser=open_browser,
        on_ready=on_ready,
        cancel=cancel,
        port=port,
    )
    store = SessionStore(session_path)
    with _LOCK:
        store.save({"version": 1, "providers": {"predgg": tokens}})
    return "your Pred.gg account"


def stored(session_path):
    """The stored record, or None when there is none (never log the result)."""
    return SessionStore(session_path).load()["providers"].get("predgg")


def sign_out(session_path):
    """Delete the stored tokens; waits for a refresh in progress so it cannot save them back."""
    with _LOCK:
        return SessionStore(session_path).clear()


class Session:
    """The user's Pred.gg tokens: hands out a valid access token, refreshing it in time.

    ``token()`` raises AuthenticationError when there is no session or Pred.gg
    refused its refresh token: the user has to sign in again.
    """

    def __init__(self, session_path, *, post=None, clock=time.time):
        self.path = session_path
        self.post = post or _post_form
        self.clock = clock

    def token(self, force=False):
        with _LOCK:
            try:
                current = stored(self.path)
            except AuthError:
                raise AuthenticationError("The session file cannot be read.") from None
            if current is None:
                raise AuthenticationError("Not signed in to Pred.gg.")
            if not force and current["expires_at"] - self.clock() > REFRESH_WITHIN:
                return current["access_token"]
            fresh = record(
                self.post(
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": current["refresh_token"],
                        "client_id": CLIENT_ID,
                    }
                ),
                self.clock(),
                previous=current,
            )
            SessionStore(self.path).save({"version": 1, "providers": {"predgg": fresh}})
            _wrote(self.path)
            return fresh["access_token"]

    def signed_in(self):
        try:
            return stored(self.path) is not None
        except AuthError:
            return False
