"""Pred.gg's GraphQL interface, over the app's own HTTPS transport (transport.py).

Without ``token`` only data Pred.gg serves to anyone is read (the hero catalog).
With it, ``token()`` (predgg.Session.token) gives the user's own access token for
the queries that need an account, such as player search; a 401 refreshes it once.
No other token is ever sent: never one taken from the website or a browser.
"""

import re

from .errors import AuthenticationError, PredError
from .transport import JsonTransport, https_opener
from .validation import array, obj

BASE = "https://pred.gg"


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


class Client:
    def __init__(self, *, timeout=20, delay=0.5, retries=2, token=None):
        self.token = token
        self.opener = https_opener()
        self.transport = JsonTransport(
            BASE, timeout=timeout, delay=delay, retries=retries, opener=self.opener
        )

    def query(self, query, variables=None, tolerate=()):
        """The data of a query. ``tolerate``: field names under which an error is
        accepted (their value comes back null); any other error raises."""
        # This client accepts query operations only. No operationName is sent, so
        # multi-operation documents cannot silently select a different operation.
        without_leading_comments = re.sub(r"\A(?:\s|\#[^\r\n]*(?:\r?\n|$))*", "", query)
        if not re.match(r"(?:query\b|\{)", without_leading_comments):
            raise PredError("Only GraphQL query operations are supported; mutations are disabled.")
        self.transport.opener = self.opener
        body = {"query": query, "variables": variables or {}}
        if self.token is None:
            answer = self.transport.request("/gql", body)
        else:
            access = self.token()  # no session, or a refused refresh: up to the caller
            try:
                answer = self._signed(body, access)
            except AuthenticationError as exc:
                if exc.status != 401:
                    raise
                answer = self._signed(body, self.token(force=True))  # expired early
        result = obj(answer, "GraphQL response")
        errors = result.get("errors")
        self.tolerated = ()  # the top fields whose errors the last query let go
        if errors and tolerate and result.get("data") is not None:
            paths = [e.get("path") if isinstance(e, dict) else None for e in errors]
            # Only parts the caller can do without. An error there may null the whole
            # entry above it: the caller sees the null and may ask for it again.
            if all(isinstance(p, list) and set(map(str, p)) & set(tolerate) for p in paths):
                self.tolerated = tuple(dict.fromkeys(str(p[0]) for p in paths))
                errors = None
        if errors:
            # Never silently accept partial GraphQL responses.
            # The text reaches the log: player IDs that a message repeats go.
            # So do the names a search sent (an error may quote its arguments).
            sent = [v for v in (variables or {}).values() if isinstance(v, str) and v]
            messages = []
            for e in array(errors, "GraphQL errors"):
                text = str(obj(e, "GraphQL error").get("message", "GraphQL error"))
                for value in sorted(sent, key=len, reverse=True):
                    text = text.replace(value, "<value>")
                messages.append(_UUID.sub("<id>", text)[:200])
            raise PredError("GraphQL: " + "; ".join(messages))
        if result.get("data") is None:
            raise PredError("Pred.gg returned no GraphQL data.")
        return obj(result["data"], "GraphQL data")

    def _signed(self, body, access):
        return self.transport.request(
            "/gql", body, headers={"Authorization": "Bearer " + access}, secrets=(access,)
        )
