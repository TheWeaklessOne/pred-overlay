"""Pred.gg catalog queries (the overlay uses the hero list) over an injected ``Client.query``.

Each method issues one query and returns provider data, without pagination,
authentication, storage, or derived statistics. Nullable version data and
inventory positions are preserved. IDs and filters are passed as variables.
"""

from datetime import datetime, timedelta

from .errors import PredError
from .validation import array, obj, uid

_HERO_DATA = """
id displayName name icon roles classes version { id name }
"""
_ITEM_DATA = """
id displayName gameId price totalPrice slotType rarity isHidden isEvolved
version { id name }
"""
_HERO_DETAIL = (
    _HERO_DATA
    + """
description mainAttributes { abilityPower attackPower durability mobility }
attributes { stat values }
abilities {
  id key name displayName type cooldown cost
  menuDescription gameDescription simplifiedDescription icon
}
recommendedSkills
perks { id displayName slot description unlockLevel }
"""
)
_ITEM_DETAIL = (
    _ITEM_DATA
    + """
name icon smallIcon class aggressionType
stats { stat value showPercent }
effects { id name active condition cooldown text }
buildsFrom { id displayName item { id slug } price totalPrice }
buildsInto { id displayName item { id slug } totalPrice }
blocks { id displayName item { id slug } }
blockedBy { id displayName item { id slug } }
"""
)
_MATCH_EVENTS = """query MatchEvents($id: ID!) {
  match(by: {id: $id}) {
    id uuid startTime endTime duration gameMode region winningTeam endReason
    spoilerBlockedUntil version { id name }
    heroBans { team hero { id slug } }
    matchPlayers {
      id name team role kills deaths assists gold goldSpent
      player { id uuid name isNameConsole } hero { id slug }
      inventoryItemData {
        id displayName gameId price totalPrice item { id slug } version { id }
      }
      transactions {
        gameTime transactionType itemData { id displayName item { id slug } }
      }
      abilityOrder { ability gameTime }
      goldEarnedAtInterval perks { id name }
      perkData { id displayName slot version { id } }
      wardPlacements { gameTime type location { x y z } }
      wardDestructions { gameTime type location { x y z } }
    }
    heroKills {
      gameTime isFirstBlood killerEntityType killerTeam killedTeam
      killerPlayer { id } killedPlayer { id }
      killerHero { id slug } killedHero { id slug } location { x y z }
    }
    objectiveKills {
      gameTime killedEntityType killerTeam killerPlayer { id }
      killerHero { id slug } location { x y z }
    }
    structureDestructions {
      gameTime structureEntityType structureTeam destructionTeam
      destructionPlayer { id } destructionHero { id slug } location { x y z }
    }
  }
}"""
_BREAKDOWNS = {
    "hero": (
        "heroStatistics",
        "PlayerHeroStatisticFilterInput",
        "hero { id slug } matchesPlayed matchesWon totalKills "
        "totalDeaths totalAssists totalTime totalGold",
    ),
    "role": (
        "roleStatistics",
        "PlayerRoleStatisticFilterInput",
        "role matchesPlayed matchesWon totalKills totalDeaths totalAssists",
    ),
    "gamemode": (
        "gamemodeStatistics",
        "PlayerGameModeStatisticFilterInput",
        "gameMode matchesPlayed matchesWon totalKills totalDeaths totalAssists",
    ),
    "interval": (
        "intervalStatistics",
        "PlayerIntervalStatisticFilterInput",
        "intervalStart matchesPlayed matchesWon ratingData { rating { id name } maxPoints }",
    ),
}


def _field(record, name):
    if name not in record:
        raise PredError("Response is missing required field: " + name)
    return record[name]


def _object(record, name, *, nullable=False):
    value = _field(record, name)
    return None if value is None and nullable else obj(value, name)


def _objects(record, name, *, nullable=False, nullable_entries=False):
    value = _field(record, name)
    if value is None and nullable:
        return None
    rows = array(value, name)
    for row in rows:
        if row is not None or not nullable_entries:
            obj(row, name + " entry")
    return rows


def _id(value, context):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise PredError(context + " must be a non-empty ID.")
    value = str(value).strip()
    if not value:
        raise PredError(context + " must be a non-empty ID.")
    return value


def _key(ref):
    ref = _id(ref, "Catalog reference")
    return {"id" if ref.isdecimal() else "slug": ref}


def _integer(value, context, minimum=0, maximum=None):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        bounds = str(minimum) + (".." + str(maximum) if maximum is not None else " or greater")
        raise PredError(context + " must be an integer " + bounds + ".")
    return value


def _filters(filters, allowed):
    if filters is None:
        return None
    result = dict(obj(filters, "filters"))
    if result.keys() - allowed:
        raise PredError("Unsupported filter for this query.")
    return result


def _interval_window(filters):
    window = obj((filters or {}).get("timeframe"), "interval timeframe")
    dates = []
    for key in ("startTime", "endTime"):
        value = window.get(key)
        if not isinstance(value, str):
            raise PredError("Daily breakdown requires startTime and endTime.")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise PredError("Invalid interval timeframe timestamp.") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise PredError("Interval timeframe timestamps require a timezone.")
        dates.append(parsed)
    if not timedelta(0) < dates[1] - dates[0] <= timedelta(days=366):
        raise PredError("Daily breakdown timeframe must be positive and at most 366 days.")


def _validate_catalog(record, kind, *, detail=False):
    data = _object(record, "data", nullable=True)
    if data is None:
        return
    _object(data, "version")
    if kind == "hero":
        for name in ("roles", "classes"):
            array(_field(data, name), name)
        if detail:
            _object(data, "mainAttributes")
            for attribute in _objects(data, "attributes"):
                array(_field(attribute, "values"), "attribute values")
            for ability in _objects(data, "abilities"):
                array(_field(ability, "cooldown"), "ability cooldown")
                array(_field(ability, "cost"), "ability cost")
            array(_field(data, "recommendedSkills"), "recommendedSkills")
            _objects(data, "perks", nullable=True)
    elif detail:
        _objects(data, "stats")
        _objects(data, "effects")
        for name in ("buildsFrom", "buildsInto", "blocks", "blockedBy"):
            for relation in _objects(data, name):
                _object(relation, "item")


class CatalogClient:
    """A single-query adapter; ``client`` must provide ``query``."""

    def __init__(self, client):
        self.client = client

    def _query(self, query, variables=None):
        return obj(self.client.query(query, variables or {}), "GraphQL data")

    def _catalog_list(self, root, kind, selection, version):
        variables = {"version": None if version is None else _id(version, "Version")}
        data = self._query(
            """query Catalog($version: ID) {
          %s { id name slug data(version: $version) { %s } }
        }"""
            % (root, selection),
            variables,
        )
        rows = _objects(data, root)
        for row in rows:
            _validate_catalog(row, kind)
        return rows

    def heroes(self, version=None):
        """Return hero summaries, preserving null data for unavailable versions."""
        return self._catalog_list("heroes", "hero", _HERO_DATA, version)

    def items(self, version=None):
        """Return item summaries; hidden/historical entries remain labeled."""
        return self._catalog_list("items", "item", _ITEM_DATA, version)

    def versions(self):
        """Return version metadata in provider order, without inferring latest."""
        data = self._query("query Versions { versions { id name releaseDate patchType } }")
        return _objects(data, "versions")

    def _catalog_detail(self, kind, ref, version, selection):
        data = self._query(
            """query CatalogDetail($key: %sKey!, $version: ID) {
          %s(by: $key) { id name slug data(version: $version) { %s } }
        }"""
            % (kind.title(), kind, selection),
            {
                "key": _key(ref),
                "version": None if version is None else _id(version, "Version"),
            },
        )
        record = _object(data, kind, nullable=True)
        if record is None:
            raise PredError(kind.title() + " not found.")
        _validate_catalog(record, kind, detail=True)
        return record

    def hero(self, ref, version=None):
        """Return one hero by numeric ID or slug, including versioned abilities."""
        return self._catalog_detail("hero", ref, version, _HERO_DETAIL)

    def item(self, ref, version=None):
        """Return one item by numeric ID or slug, including stats and recipe edges."""
        return self._catalog_detail("item", ref, version, _ITEM_DETAIL)

    def match_events(self, match_id):
        """Return one known UUID match with event, purchase and upgrade arrays.

        Gold samples retain their indices; no sampling cadence is assumed.
        Arrays retain provider order and null inventory/perk positions.
        """
        match_id = uid(match_id, "Match ID")
        data = self._query(_MATCH_EVENTS, {"id": match_id})
        match = _object(data, "match", nullable=True)
        if match is None:
            raise PredError("Match not found.")
        if uid(match.get("id"), "Response match ID") != match_id:
            raise PredError("Response contains a different match.")
        _object(match, "version")
        for ban in _objects(match, "heroBans", nullable=True) or []:
            _object(ban, "hero", nullable=True)
        for member in _objects(match, "matchPlayers"):
            _object(member, "player")
            _object(member, "hero", nullable=True)
            for item in (
                _objects(member, "inventoryItemData", nullable=True, nullable_entries=True) or []
            ):
                if item is not None:
                    _object(item, "item")
                    _object(item, "version")
            for transaction in _objects(member, "transactions"):
                item = _object(transaction, "itemData", nullable=True)
                if item is not None:
                    _object(item, "item")
            _objects(member, "abilityOrder")
            array(_field(member, "goldEarnedAtInterval"), "goldEarnedAtInterval")
            _objects(member, "perks", nullable=True, nullable_entries=True)
            for perk in _objects(member, "perkData", nullable=True, nullable_entries=True) or []:
                if perk is not None:
                    _object(perk, "version")
            for name in ("wardPlacements", "wardDestructions"):
                for event in _objects(member, name):
                    _object(event, "location")
        for name, nullable_fields in (
            ("heroKills", ("killerPlayer", "killedPlayer", "killerHero", "killedHero")),
            ("objectiveKills", ("killerPlayer", "killerHero")),
            ("structureDestructions", ("destructionPlayer", "destructionHero")),
        ):
            for event in _objects(match, name):
                _object(event, "location")
                for field in nullable_fields:
                    _object(event, field, nullable=True)
        return match

    def player_breakdown(self, player_id, kind, filters=None):
        """Return a results wrapper for hero, role, gamemode, or interval.

        Interval is daily and requires timezone-aware timeframe.startTime and
        endTime at most 366 days apart. Other kinds have finite catalog/enum rows.
        """
        player_id = uid(player_id, "Player ID")
        if not isinstance(kind, str) or kind not in _BREAKDOWNS:
            raise PredError("Breakdown kind must be hero, role, gamemode, or interval.")
        field, input_type, selection = _BREAKDOWNS[kind]
        allowed = {"gameModes", "regions", "roles", "timeframe", "matchup"}
        allowed.add("ratings" if kind == "interval" else "rating")
        if kind in ("hero", "interval"):
            allowed.add("heroes")
        filters = _filters(filters, allowed)
        extra_argument = extra_field = ""
        if kind == "interval":
            _interval_window(filters)
            extra_argument, extra_field = ", interval: DAY", "interval"
        data = self._query(
            """query PlayerBreakdown($id: ID!, $filter: %s) {
          player(by: {id: $id}) {
            %s(filter: $filter%s) { %s results { %s } }
          }
        }"""
            % (input_type, field, extra_argument, extra_field, selection),
            {
                "id": player_id,
                "filter": filters,
            },
        )
        player = _object(data, "player", nullable=True)
        if player is None:
            raise PredError("Player not found.")
        result = _object(player, field, nullable=True)
        if result is None:
            raise PredError("Player breakdown is unavailable.")
        for row in _objects(result, "results"):
            if kind == "hero":
                _object(row, "hero")
            elif kind == "interval":
                for rating in _objects(row, "ratingData"):
                    _object(rating, "rating")
        return result

    def leaderboard(self, rating_id, limit=20, offset=0):
        """Return one CURRENT leaderboard page; local request cap is 50 rows."""
        rating_id = _id(rating_id, "Rating ID")
        _integer(limit, "Leaderboard limit", 1, 50)
        _integer(offset, "Leaderboard offset")
        data = self._query(
            """query Leaderboard($rating: ID!, $limit: Int!, $offset: Int!) {
          leaderboardPaginated(ratingId: $rating, ratingType: CURRENT,
                               limit: $limit, offset: $offset) {
            ratingId ratingType limit offset totalCount results {
              player { id name } points ranking percentile unranked
              rank { id name } peakPoints peakRanking
            }
          }
        }""",
            {"rating": rating_id, "limit": limit, "offset": offset},
        )
        page = _object(data, "leaderboardPaginated")
        rows = _objects(page, "results")
        returned_limit = _integer(page.get("limit"), "Response limit", 0, limit)
        returned_offset = _integer(page.get("offset"), "Response offset")
        total = _integer(page.get("totalCount"), "Response totalCount")
        if (
            returned_offset != offset
            or len(rows) > returned_limit
            or (rows and offset + len(rows) > total)
        ):
            raise PredError("Inconsistent leaderboard pagination response.")
        if page.get("ratingId") != rating_id or page.get("ratingType") != "CURRENT":
            raise PredError("Response contains a different leaderboard.")
        for row in rows:
            _object(row, "player")
            _object(row, "rank", nullable=True)
        return page

    def matchup(self, hero_id, filters=None):
        """Return enemy/same-role WINRATE DESC statistics, including sample sizes.

        The API has no limit/offset argument. Its finite hero matchup table is
        returned intact; no user list or additional pages are requested.
        """
        hero_id = _id(hero_id, "Hero ID")
        filters = _filters(filters, {"gameModes", "roles", "ranks", "versions", "matchupRoles"})
        data = self._query(
            """query Matchup($id: ID!, $filter: HeroMatchupStatisticFilterInput) {
          hero(by: {id: $id}) {
            matchupStatistic(filter: $filter, metric: WINRATE, order: DESC,
                             isAlly: false, sameRole: true) {
              filter { gameModes roles ranks versions matchupRoles }
              metric order isAlly sameRole results {
                matchupHero { id slug } matchesPlayed winrate loserate
                firstTowerTime firstTowerTimeDiff
              }
            }
          }
        }""",
            {"id": hero_id, "filter": filters},
        )
        hero = _object(data, "hero", nullable=True)
        if hero is None:
            raise PredError("Hero not found.")
        result = _object(hero, "matchupStatistic", nullable=True)
        if result is None:
            raise PredError("Matchup statistics are unavailable.")
        echoed = _object(result, "filter")
        for field in ("gameModes", "roles", "ranks", "versions", "matchupRoles"):
            value = _field(echoed, field)
            if value is not None:
                array(value, "matchup filter " + field)
        for row in _objects(result, "results"):
            _object(row, "matchupHero")
        return result
