"""Pred.gg's observed GraphQL interface. No third-party dependencies."""

import math
import os
import re
import time
import urllib.error
import urllib.request

from .errors import PayloadError, PredError
from .transport import JsonTransport, NoRedirect, redact, validate_token
from .validation import array, obj, uid

BASE = "https://pred.gg"


class Client:
    def __init__(self, *, token=None, timeout=20, delay=0.5, retries=2, anonymous=False):
        # anonymous: send no Authorization at all, for data Pred.gg serves publicly
        # (the hero catalog, ratings) now that its guest token no longer opens player stats.
        self.anonymous = anonymous
        self.token = None if anonymous else token
        if self.token is None and not anonymous:
            self.token = os.environ.get("PRED_ACCESS_TOKEN")
        if self.token is not None:
            validate_token(self.token)
        self.supplied_token = bool(self.token)
        self.version = None
        self.expires_at = 0
        self.timeout = timeout
        self.delay = delay
        self.last_request = 0
        self.opener = urllib.request.build_opener(NoRedirect())
        self.request_count = 0
        self.transport = JsonTransport(
            BASE, timeout=timeout, delay=delay, retries=retries, opener=self.opener
        )

    def _http(self, path, payload=None, authenticated=False):
        headers = {}
        if self.version:
            headers["X-Frontend-Version"] = self.version
        if authenticated:
            headers["Authorization"] = "Bearer " + self.token
        self.transport.opener = self.opener
        try:
            return self.transport.request(path, payload, headers=headers, secrets=(self.token,))
        finally:
            self.request_count = self.transport.request_count
            self.last_request = self.transport.last_request

    def authenticate(self, refresh=False):
        if self.version is None or refresh:
            data = obj(self._http("/_app/version.json"), "frontend version")
            if not isinstance(data.get("version"), (str, int)) or isinstance(
                data.get("version"), bool
            ):
                raise PayloadError("Frontend version is missing or invalid.")
            self.version = str(data["version"])
        if self.supplied_token or self.anonymous:
            return
        if not self.token or refresh or time.monotonic() >= self.expires_at:
            data = obj(self._http("/auth/token"), "guest authentication")
            candidate = data.get("access_token")
            try:
                validate_token(candidate)
                raw_expiry = data.get("expires_in", 300)
                if isinstance(raw_expiry, bool):
                    raise ValueError
                expiry = float(raw_expiry)
                if not math.isfinite(expiry) or expiry <= 0:
                    raise ValueError
            except (PredError, TypeError, ValueError, OverflowError):
                raise PayloadError(
                    "Guest authentication returned an invalid token or expiry."
                ) from None
            self.token = candidate
            self.expires_at = time.monotonic() + max(0, expiry - 30)

    def query(self, query, variables=None):
        # This client accepts query operations only. No operationName is sent, so
        # multi-operation documents cannot silently select a different operation.
        without_leading_comments = re.sub(r"\A(?:\s|\#[^\r\n]*(?:\r?\n|$))*", "", query)
        if not re.match(r"(?:query\b|\{)", without_leading_comments):
            raise PredError("Only GraphQL query operations are supported; mutations are disabled.")
        self.authenticate()
        for attempt in range(2):
            try:
                result = self._http(
                    "/gql", {"query": query, "variables": variables or {}}, bool(self.token)
                )
            except PredError as exc:
                guest = not self.supplied_token and not self.anonymous
                if "HTTP 401" in str(exc) and guest and attempt == 0:
                    self.authenticate(refresh=True)
                    continue
                raise
            result = obj(result, "GraphQL response")
            errors = result.get("errors")
            if errors:
                # Never silently accept partial GraphQL responses.
                messages = [
                    redact(obj(e, "GraphQL error").get("message", "GraphQL error"), (self.token,))
                    for e in array(errors, "GraphQL errors")
                ]
                if (
                    any("authentication" in m.lower() for m in messages)
                    and not self.supplied_token
                    and not self.anonymous
                    and attempt == 0
                ):
                    self.authenticate(refresh=True)
                    continue
                message = "; ".join(messages)
                if self.token:
                    message = message.replace(self.token, "[redacted]")
                raise PredError("GraphQL: " + message)
            if result.get("data") is None:
                raise PredError("Pred.gg returned no GraphQL data.")
            return obj(result["data"], "GraphQL data")
        raise PredError("Authentication failed after refresh.")

    def stats(self, player_id):
        data = self.query(
            """query($id:ID!){player(by:{id:$id}){id name generalStatistic{
          result{matchesPlayed matchesWon totalKills totalDeaths totalAssists totalTime
          totalHeroDamage totalGold mostPlayedHero{id name slug} mostPlayedRole mostPlayedRegion}
        }}}""",
            {"id": player_id},
        )["player"]
        if data is None:
            raise PredError("Player not found: " + player_id)
        data = obj(data, "player statistics")
        if uid(data.get("id")) != uid(player_id):
            raise PayloadError("Provider returned statistics for a different player.")
        return data

    def schema(self, name):
        type_ref = "kind name"
        for _ in range(5):
            type_ref = "kind name ofType{%s}" % type_ref
        return self.query(
            """query($name:String!){__type(name:$name){name kind description
          fields{name description args{name defaultValue type{%s}} type{%s}}
          inputFields{name defaultValue type{%s}} enumValues{name description}
        }}"""
            % (type_ref, type_ref, type_ref),
            {"name": name},
        )["__type"]
