"""Omeda game backend: player login and read-only queries about the player's own game.

Login follows the game client's path: a login-queue pass from the game backend,
then ``authenticateorcreatev2`` on the social backend with a Steam credential
(an auth session ticket from the local Steam client, or a browser Steam OpenID
assertion). It returns a social and a game token. Verified on 2026-10-30: this
login did not disconnect a running game client of the same account.

The client calls only audited read-only RPCs. The current game instance gives
the match ID and the ten player UUIDs while the match runs; its game-server
connection details are never returned or stored.
"""

import time
import urllib.parse

from .errors import AuthenticationError, PayloadError, PredError
from .official import GAME_SHARD, ORIGIN
from .transport import JsonTransport, redact, validate_token
from .validation import array, obj, uid

GAME_ORIGIN = "https://game.live.predecessor.omeda.pragmaengine.com"
ALLOWED = {
    "MultiplayerRpc.SyncGameInstanceV1Request",
    "MatchHistoryRpc.GetMatchDetailsV1Request",
    "MatchHistoryRpc.GetPlayerMatchListV1Request",
    "PlayerPublicProfileRpc.GetV1Request",
    "MatchmakingRpc.GetMatchmakingInfoV2Request",
}
TEAMS = {0: "DAWN", 1: "DUSK"}  # observed: team 0 was Dawn on the loading screen
_QUEUE_LIMIT = 120  # seconds to wait for the login queue


def login(provider_token, *, social=None, game=None, sleep=time.sleep):
    """Exchange a Steam credential for {"social_token", "game_token"}.

    ``provider_token`` is a hex Steam auth session ticket or the JSON string of a
    Steam OpenID assertion. Uses an existing Omeda account; Pragma's endpoint
    would create one for a Steam account that never played, so only use accounts
    that already have a Predecessor profile.
    """
    social = social or JsonTransport(ORIGIN, timeout=20, delay=0.5, same_origin_redirects=True)
    game = game or JsonTransport(GAME_ORIGIN, timeout=20, delay=0.5, same_origin_redirects=True)
    query = "/v1/loginqueue/getinqueuev1?" + urllib.parse.urlencode({"gameShardId": GAME_SHARD})
    waited = 0.0
    while True:
        ticket = obj(game.request(query), "login queue")
        if ticket.get("isAllowedIn"):
            break
        pause = min(max(float(ticket.get("pollNextDuration") or 1000) / 1000, 0.5), 10)
        waited += pause
        if waited > _QUEUE_LIMIT:
            raise PredError("Omeda login queue did not admit the session in time.")
        sleep(pause)
    if not isinstance(ticket.get("token"), str) or not ticket["token"]:
        raise PayloadError("Login queue returned no pass.")
    payload = {
        "providerId": "STEAM",
        "providerToken": provider_token,
        "gameShardId": GAME_SHARD,
        "loginQueuePassToken": ticket["token"],
    }
    auth = obj(
        social.request(
            "/v1/account/authenticateorcreatev2",
            payload,
            secrets=(provider_token, ticket["token"]),
        ),
        "authentication response",
    )
    tokens = obj(auth.get("pragmaTokens"), "authentication tokens")
    social_token, game_token = tokens.get("pragmaSocialToken"), tokens.get("pragmaGameToken")
    if not isinstance(social_token, str) or not isinstance(game_token, str):
        raise AuthenticationError("Omeda did not issue game and social tokens.")
    validate_token(social_token)
    validate_token(game_token)
    return {"social_token": social_token, "game_token": game_token}


class GameClient:
    """Read-only game-backend RPCs with a player game token."""

    def __init__(self, token, *, timeout=20, delay=0.5, retries=2):
        validate_token(token)
        self.token = token
        self.transport = JsonTransport(
            GAME_ORIGIN, timeout=timeout, delay=delay, retries=retries, same_origin_redirects=True
        )

    def rpc(self, kind, payload):
        if kind not in ALLOWED:
            raise PredError("Unsupported game RPC; only audited read-only methods are allowed.")
        body = obj(
            self.transport.request(
                "/v1/rpc",
                {"requestId": 1, "type": kind, "payload": payload},
                headers={"Authorization": "Bearer " + self.token},
                secrets=(self.token,),
            ),
            "game RPC",
        )
        if body.get("serviceError"):
            error = obj(body["serviceError"], "game service error")
            raise PredError(
                "Game RPC: "
                + redact(
                    error.get("error") or error.get("status") or "service error", (self.token,)
                )
            )
        return obj(obj(body.get("response"), "game response").get("payload"), "game payload")

    def current_game(self):
        """The session's current match: ID and player UUIDs (no connection details)."""
        data = self.rpc("MultiplayerRpc.SyncGameInstanceV1Request", {})
        if not data.get("isInGameInstance"):
            return {"in_game": False, "match_id": None, "player_ids": []}
        instance = obj(data.get("gameInstance"), "game instance")
        players = [
            uid(obj(p, "game instance player").get("playerId"))
            for p in array(instance.get("players", []), "game instance players")
        ]
        return {
            "in_game": True,
            "match_id": uid(instance.get("gameInstanceId")),
            "player_ids": list(dict.fromkeys(players)),
        }

    def match_list(self, limit=5):
        """The session's own recent matches, newest first (sorted here to be safe)."""
        data = self.rpc("MatchHistoryRpc.GetPlayerMatchListV1Request", {"limit": int(limit)})
        rows = []
        for row in array(data.get("matchSummaries", []), "match summaries"):
            row = obj(row, "match summary")
            rows.append(
                {
                    "match_id": uid(row.get("matchId")),
                    "ended_at": int(row.get("gameEndedEpochSeconds") or 0),
                    "game_mode": row.get("gameMode"),
                    "hero": row.get("heroName"),
                    "role": (row.get("roleName") or "").upper() or None,
                    "won": bool(row.get("win")),
                }
            )
        return sorted(rows, key=lambda r: r["ended_at"], reverse=True)

    def match_details(self, match_id):
        """A finished match's roster and stats, or None while it is not recorded yet."""
        data = self.rpc("MatchHistoryRpc.GetMatchDetailsV1Request", {"matchId": uid(match_id)})
        details = data.get("matchDetails")
        if not details:
            return None
        details = obj(details, "match details")
        players = []
        for row in array(details.get("players"), "match players"):
            row = obj(row, "match player")
            team = row.get("teamId")
            name = row.get("playerName")
            players.append(
                {
                    "player_id": uid(row.get("playerId")),
                    "name": None if name in (None, "", "HIDDEN") else name,
                    "platform": row.get("playerPlatform"),
                    "team": TEAMS.get(int(team)) if isinstance(team, (int, float)) else None,
                    "role": (row.get("roleName") or "").upper() or None,
                    "hero": row.get("heroName"),
                    "kda": [row.get("kills"), row.get("deaths"), row.get("assists")],
                }
            )
        winner = details.get("winningTeamId")
        return {
            "match_id": uid(details.get("matchId")),
            "game_mode": details.get("gameMode"),
            "winning_team": TEAMS.get(int(winner)) if isinstance(winner, (int, float)) else None,
            "ended_at": details.get("gameEndedEpochSeconds"),
            "players": players,
        }

    def queues(self):
        """Players searching for a match right now, per region and queue (see queue_counts)."""
        return queue_counts(self.rpc("MatchmakingRpc.GetMatchmakingInfoV2Request", {}))

    def public_profiles(self, player_ids):
        """Current rank, level and account age of any players, by UUID.

        Only these fields are kept, plus the name on the platform the player
        last signed in from: the game shows that name (a Steam name can differ
        from the official one), so the overlay and the scoreboard match it.
        Other linked accounts are dropped.
        """

        def number(value):
            try:
                return int(float(value))
            except (TypeError, ValueError, OverflowError):
                return None  # one odd field must not fail the whole roster

        ids = [uid(p) for p in player_ids]
        data = self.rpc(
            "PlayerPublicProfileRpc.GetV1Request", {"byPlayerIdRequest": {"playerIds": ids}}
        )
        profiles = {}
        for pid, row in obj(data.get("profiles") or {}, "public profiles").items():
            row = obj(row, "public profile")
            rank = row.get("rank") or {}
            created = number(row.get("createdTimestampMillis"))
            platform = row.get("lastLoggedInProviderAccount")
            shown = next(
                (
                    (account.get("providerDisplayName") or {}).get("displayName")
                    for account in row.get("providerAccounts") or []
                    if account.get("idProviderType") == platform
                ),
                None,
            )
            profiles[uid(pid)] = {
                "placed": bool(rank.get("isPlaced")),
                "tier": number(rank.get("tier")),
                "division": number(rank.get("division")),
                "vp": number(rank.get("vp")),
                "level": number(row.get("level")),
                "created": created // 1000 if created else None,
                "shown_name": shown or None,  # as the game displays them
            }
        return profiles


# Omeda game modes in Pred.gg's words where they are known to match, so
# matches stored from either source filter the same way.
PREDGG_MODES = {"TEAM_VS_TEAM": "STANDARD", "RANKED": "RANKED"}


def queue_counts(payload):
    """``GetMatchmakingInfoV2`` -> {region: {queue: (players, parties)}}.

    Counts are players searching for a match right now (not everyone online).
    Every queue is cross-play, so consoles are included but cannot be told apart.
    """
    counts = {}
    for row in array(payload.get("queueInfo") or [], "queue info"):
        key = obj(obj(row, "queue").get("matchmakingQueueKey") or {}, "queue key")
        ext = obj(key.get("extMatchmakingKey") or {}, "queue key")
        region, queue = ext.get("regionName"), ext.get("queueName")
        if not region or not queue:
            continue
        players, parties = counts.setdefault(region, {}).get(queue, (0, 0))
        counts[region][queue] = (
            players + int(row.get("playersInQueue") or 0),
            parties + int(row.get("partiesInQueue") or 0),
        )
    return counts


def match_document(details):
    """``GameClient.match_details`` -> the match shape the local store keeps.

    Hero names are the internal ones, as Pred.gg's ``hero.name``; the match ID
    is the game instance ID that Pred.gg uses too, so both sources merge.
    """
    from datetime import datetime, timezone

    try:
        ended = datetime.fromtimestamp(int(float(details["ended_at"])), timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise PayloadError("Match details have no end time.") from None
    players = []
    for row in details["players"]:
        kills, deaths, assists = (list(row.get("kda") or []) + [None] * 3)[:3]
        player = {"id": row["player_id"]}
        if row.get("name"):
            player["name"] = row["name"]
        players.append(
            {
                "player": player,
                "name": row.get("name"),
                "team": row.get("team"),
                "role": row.get("role"),
                "hero": {"name": row["hero"]} if row.get("hero") else None,
                "kills": kills,
                "deaths": deaths,
                "assists": assists,
                "platform": row.get("platform"),
            }
        )
    return {
        "id": details["match_id"],
        "endTime": ended.isoformat().replace("+00:00", "Z"),
        "gameMode": PREDGG_MODES.get(details.get("game_mode"), details.get("game_mode")),
        "winningTeam": details.get("winning_team"),
        "matchPlayers": players,
    }


RENEW_BEFORE = 600  # seconds: renew tokens that expire sooner than this


def steam_client_login(game_log=None):
    """Log in with a ticket from the local Steam client; no browser involved.

    Omeda's endpoint creates a profile for a Steam account that never played,
    so this refuses to run until the game has run on this PC (its log exists).
    """
    from .overlay.phases import default_log
    from .steam_ticket import steam_web_ticket

    if not (game_log or default_log()).exists():
        raise PredError("Predecessor has not run on this PC yet; play it once first.")
    with steam_web_ticket() as ticket:
        return login(ticket)


def browser_exchange(body):
    """``steam_login`` exchange: a browser Steam OpenID assertion becomes both tokens."""
    from .official import OfficialClient

    tokens = login(body["providerToken"])
    return {"tokens": tokens, "profile": OfficialClient(tokens["social_token"]).me()}


def save_tokens(store, tokens):
    values = store.load()
    values["providers"]["omeda"] = {"social_token": tokens["social_token"]}
    values["providers"]["omeda_game"] = {"game_token": tokens["game_token"]}
    store.save(values)


def _fresh(token, margin=RENEW_BEFORE):
    from .auth import _expiry_hint

    hint = _expiry_hint(token) if token else None
    return bool(hint) and hint["expires_at"] - time.time() > margin


def full_name(display):
    """``{"displayName", "discriminator"}`` → ``Name#code``."""
    code = display.get("discriminator")
    return display["displayName"] + ("#" + code if code else "")


def stored_tokens(session_path, *, renew=True, margin=RENEW_BEFORE):
    """Valid social and game tokens from the session, refreshed via Steam if possible.

    The JWT expiry is read only to decide when to renew; the server stays the
    authority on validity. With ``renew`` the running Steam client is asked for
    a web-API ticket (see ``steam_ticket``); if that fails, the caller is told
    what to fix and to sign in again.
    """
    from .auth import SessionStore
    from .config import token_for

    social = token_for("omeda", session_path)
    game = token_for("omeda_game", session_path)
    if _fresh(social, margin) and _fresh(game, margin):
        return {"social_token": social, "game_token": game}
    if not renew:
        raise AuthenticationError("Not signed in, or the session expired.")
    try:
        tokens = steam_client_login()
    except PredError as exc:
        raise AuthenticationError(
            f"Could not sign in through Steam: {exc} "
            "Start Steam with the account you play Predecessor on, then sign in again."
        ) from None
    save_tokens(SessionStore(session_path), tokens)
    return tokens
