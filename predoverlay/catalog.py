"""Pred.gg's hero catalog over an injected ``Client.query``: the one catalog query the overlay uses.

It returns provider data without pagination, authentication, storage or derived
statistics. Nullable version data is preserved; the version is passed as a variable.
"""

from .errors import PredError
from .validation import array, obj

_HERO_DATA = """
id displayName name icon roles classes version { id name }
"""


def _field(record, name):
    if name not in record:
        raise PredError("Response is missing required field: " + name)
    return record[name]


def _object(record, name, *, nullable=False):
    value = _field(record, name)
    return None if value is None and nullable else obj(value, name)


def _objects(record, name):
    rows = array(_field(record, name), name)
    for row in rows:
        obj(row, name + " entry")
    return rows


def _id(value, context):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise PredError(context + " must be a non-empty ID.")
    value = str(value).strip()
    if not value:
        raise PredError(context + " must be a non-empty ID.")
    return value


def _validate_hero(record):
    data = _object(record, "data", nullable=True)
    if data is None:
        return
    _object(data, "version")
    for name in ("roles", "classes"):
        array(_field(data, name), name)


class CatalogClient:
    """A single-query adapter; ``client`` must provide ``query``."""

    def __init__(self, client):
        self.client = client

    def heroes(self, version=None):
        """Return hero summaries, preserving null data for unavailable versions."""
        variables = {"version": None if version is None else _id(version, "Version")}
        data = obj(
            self.client.query(
                """query Catalog($version: ID) {
          heroes { id name slug data(version: $version) { %s } }
        }"""
                % _HERO_DATA,
                variables,
            ),
            "GraphQL data",
        )
        rows = _objects(data, "heroes")
        for row in rows:
            _validate_hero(row)
        return rows
