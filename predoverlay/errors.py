"""Safe, structured errors shared by providers, services and presentation."""


class PredError(Exception):
    code = "operation_failed"

    def __init__(self, message, *, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    def as_dict(self):
        data = {"error": str(self), "code": self.code}
        if self.status is not None:
            data["status"] = self.status
        if self.retry_after is not None:
            data["retry_after"] = self.retry_after
        return data


class AuthenticationError(PredError):
    code = "authentication_required"


class TransportError(PredError):
    code = "transport_error"


class RateLimitError(TransportError):
    code = "rate_limited"


class PayloadError(PredError):
    code = "invalid_response"


class AmbiguousPlayerError(PredError):
    code = "ambiguous_player"


class CapabilityError(PredError):
    code = "unsupported_capability"
