"""Official Omeda identity provider; audited RPC allowlist and explicit login."""

import os

from .errors import AuthenticationError, PayloadError, PredError
from .transport import JsonTransport, redact, validate_token
from .validation import array, obj, uid

ORIGIN = "https://social.live.predecessor.omeda.pragmaengine.com"
GAME_SHARD = "00000000-0000-0000-0000-000000000001"
ALLOWED = {
    "AccountRpc.GetAccountV1Request",
    "AccountRpc.GetPragmaPlayerIdForDisplayNameV1Request",
    "AccountRpc.GetDisplayNameForPragmaPlayerIdV1Request",
    "AccountRpc.GetPlayerIdentitiesV1Request",
    "FriendRpc.GetPresenceV1Request",
}
# Returns the friend list but also registers this session online in the Friend
# service, so callers must opt in explicitly.
SIDE_EFFECT = {"FriendRpc.GetFriendListAndRegisterOnlineV1Request"}


class OfficialClient:
    def __init__(self, token=None, *, timeout=20, delay=0.6, retries=2):
        self.token = token if token is not None else os.environ.get("OMEDA_SOCIAL_TOKEN")
        if self.token is not None:
            validate_token(self.token)
        self.transport = JsonTransport(
            ORIGIN, timeout=timeout, delay=delay, retries=retries, same_origin_redirects=True
        )
        self.opener = self.transport.opener

    def clean_error(self, message):
        return redact(message, (self.token,))

    def request(self, path, body=None):
        headers = {}
        if body is not None:
            if not self.token:
                raise AuthenticationError(
                    "Official RPC needs a signed-in session (or OMEDA_SOCIAL_TOKEN)."
                )
            headers["Authorization"] = "Bearer " + self.token
        self.transport.opener = self.opener
        return obj(
            self.transport.request(path, body, headers=headers, secrets=(self.token,)),
            "official response",
        )

    def rpc(self, kind, payload, *, side_effects=False):
        if kind not in ALLOWED and not (side_effects and kind in SIDE_EFFECT):
            raise PredError(
                "Unsupported RPC; this client permits only audited methods, side effects by opt-in."
            )
        body = obj(
            self.request("/v1/rpc", {"requestId": 1, "type": kind, "payload": payload}),
            "official RPC",
        )
        if body.get("serviceError"):
            error = obj(body["serviceError"], "official service error")
            raise PredError(
                "Official RPC: "
                + self.clean_error(error.get("error") or error.get("status") or "service error")
            )
        return obj(
            obj(body.get("response"), "official response envelope").get("payload"),
            "official payload",
        )

    def info(self):
        return self.request("/v1/info")

    def types(self, text=None):
        values = self.request("/v1/types")
        return {
            "source": "omeda_official",
            "meaning": "Registered message types, not a permission or endpoint availability guarantee.",
            "types": {
                k: v for k, v in values.items() if not text or text.casefold() in k.casefold()
            },
        }

    def me(self):
        data = self.rpc("AccountRpc.GetAccountV1Request", {})
        name = obj(data.get("displayName"), "own display name")
        if (
            not isinstance(name.get("displayName"), str)
            or not name["displayName"]
            or not isinstance(name.get("discriminator", ""), str)
        ):
            raise PayloadError("Official account has an invalid display name.")
        games = array(data.get("gameIdentities"), "own game identities")
        for game in games:
            game = obj(game, "game identity")
            uid(game.get("gameShardId"))
            uid(game.get("pragmaPlayerId"))
        return {
            "source": "omeda_official",
            "displayName": {k: name[k] for k in ("displayName", "discriminator") if k in name},
            "pragmaSocialId": data.get("pragmaSocialId"),
            "gameIdentities": [
                {k: g[k] for k in ("gameShardId", "pragmaPlayerId") if k in obj(g)} for g in games
            ],
        }

    def lookup(self, full_name):
        name, separator, discriminator = full_name.rpartition("#")
        if not separator or not name.strip() or not discriminator.strip():
            raise PredError(
                "Supply a complete in-game Name#code. Bare nickname and wildcard search are not supported online."
            )
        data = self.rpc(
            "AccountRpc.GetPragmaPlayerIdForDisplayNameV1Request",
            {"gameShardId": GAME_SHARD, "displayName": name, "discriminator": discriminator},
        )
        found = bool(data.get("pragmaPlayerId"))
        result = (
            {"pragmaPlayerId": uid(data["pragmaPlayerId"])}
            if found
            else {k: data[k] for k in ("identifier", "message") if isinstance(data.get(k), str)}
        )
        return {"source": "omeda_official", "query": full_name, "found": found, "result": result}

    def name(self, player_id):
        player_id = uid(player_id)
        data = self.rpc(
            "AccountRpc.GetDisplayNameForPragmaPlayerIdV1Request",
            {"gameShardId": GAME_SHARD, "pragmaPlayerId": player_id},
        )
        found = isinstance(data.get("displayName"), str) and bool(data["displayName"])
        code = data.get("discriminator", "")
        if not isinstance(code, str):
            raise PayloadError("Discriminator must be a string.")
        result = (
            {"displayName": data["displayName"], "discriminator": code}
            if found
            else {k: data[k] for k in ("identifier", "message") if isinstance(data.get(k), str)}
        )
        return {
            "source": "omeda_official",
            "player_id": player_id,
            "found": found,
            "full_name": data["displayName"] + "#" + code if found else None,
            "result": result,
        }

    def identities(self, player_ids):
        ids = list(dict.fromkeys(uid(value) for value in player_ids))
        if not 1 <= len(ids) <= 100:
            raise PredError("Supply 1–100 UUIDs per call (verified request-size limit).")
        data = self.rpc(
            "AccountRpc.GetPlayerIdentitiesV1Request", {"gameShardId": GAME_SHARD, "playerIds": ids}
        )
        results, returned = [], set()
        for row in array(data.get("playerIdentities"), "playerIdentities"):
            row = obj(row, "player identity")
            pid = uid(row.get("pragmaPlayerId"))
            if pid not in ids or pid in returned:
                raise PayloadError("Official API returned an unrequested or duplicate UUID.")
            name = obj(row.get("pragmaDisplayName"), "display name")
            if not isinstance(name.get("displayName"), str) or not isinstance(
                name.get("discriminator", ""), str
            ):
                raise PayloadError("Official identity contains an invalid display name.")
            item = {
                "pragmaPlayerId": pid,
                "pragmaDisplayName": {
                    k: name[k] for k in ("displayName", "discriminator") if k in name
                },
            }
            if row.get("pragmaSocialId"):
                item["pragmaSocialId"] = uid(row["pragmaSocialId"])
            results.append(item)
            returned.add(pid)
        return {
            "source": "omeda_official",
            "results": results,
            "not_returned": [pid for pid in ids if pid not in returned],
            "note": "Names are current observations. Missing identities do not establish account absence.",
        }

    def own_presences(self):
        """Raw presence records of the session's own account; a pure read."""
        data = self.rpc("FriendRpc.GetPresenceV1Request", {})
        return [
            obj(obj(row, "presence entry").get("presence"), "own presence")
            for row in array(data.get("presences"), "presences")
        ]

    def friends(self):
        """Friends with raw presences. Side effect: registers this session online."""
        data = self.rpc("FriendRpc.GetFriendListAndRegisterOnlineV1Request", {}, side_effects=True)
        results = []
        for friend in array(obj(data.get("friendList"), "friend list").get("friends"), "friends"):
            friend = obj(friend, "friend")
            name = obj(friend.get("displayName"), "friend display name")
            if not isinstance(name.get("displayName"), str) or not isinstance(
                name.get("discriminator", ""), str
            ):
                raise PayloadError("Friend has an invalid display name.")
            player_ids = []
            for game in array(friend.get("gameIdentities", []), "friend game identities"):
                game = obj(game, "friend game identity")
                if game.get("gameShardId") == GAME_SHARD:
                    player_ids.append(uid(game.get("pragmaPlayerId")))
            # Whitelisted fields only: friend records may carry social or provider IDs.
            results.append(
                {
                    "displayName": {
                        k: name[k] for k in ("displayName", "discriminator") if k in name
                    },
                    "player_ids": player_ids,
                    "presences": [
                        obj(p, "friend presence")
                        for p in array(friend.get("presences", []), "friend presences")
                    ],
                }
            )
        return results

    def exchange_steam(self, payload):
        if obj(payload).get("providerId") != "STEAM" or not isinstance(
            payload.get("providerToken"), str
        ):
            raise PredError("Expected a signed Steam OpenID response.")
        auth = obj(
            self.transport.request(
                "/v1/account/authenticatev1", payload, secrets=(payload["providerToken"],)
            ),
            "authentication response",
        )
        token = obj(auth.get("pragmaTokens"), "authentication tokens").get("pragmaSocialToken")
        if not isinstance(token, str) or not token:
            raise AuthenticationError("Omeda did not issue a player session.")
        validate_token(token)
        previous_token = self.token
        self.token = token
        try:
            profile = self.me()
        except Exception:
            self.token = previous_token
            raise
        return {"access_token": token, "profile": profile}
