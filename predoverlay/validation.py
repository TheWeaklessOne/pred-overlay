"""Small validators at external data boundaries, without leaking payload values."""

import uuid

from .errors import PayloadError


def obj(value, context="response"):
    if not isinstance(value, dict):
        raise PayloadError(context + " must be an object.")
    return value


def array(value, context="results"):
    if not isinstance(value, list):
        raise PayloadError(context + " must be an array.")
    return value


def nonnegative(value, context):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PayloadError(context + " must be a non-negative integer.")
    return value


def uid(value, context="UUID"):
    if not isinstance(value, str):
        raise PayloadError(context + " must be a UUID string.")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise PayloadError(context + " is not a valid UUID.") from None
