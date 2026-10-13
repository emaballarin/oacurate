"""Exception hierarchy and its mapping to process exit codes."""

from datetime import datetime

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_PARTIAL = 2
EXIT_AUTH = 3
EXIT_BUDGET = 4


class OacurateError(Exception):
    """Base class for every error the tool raises on purpose."""

    exit_code = EXIT_USAGE


class UsageError(OacurateError):
    """Invalid command-line usage or configuration."""


class ValidationError(OacurateError):
    """Input or plan content failed validation."""


class InvalidIdentifier(ValidationError):
    """A DOI, ORCID, OpenAlex ID or arXiv ID is malformed."""


class PlanError(ValidationError):
    """A plan file is malformed or does not belong to the configured author."""


class ApiError(OacurateError):
    """An HTTP request failed in a way that is not handled more specifically."""

    def __init__(self, message: str, *, status: int | None = None, body: object = None) -> None:
        """Keep the HTTP status and the decoded body next to the message."""
        super().__init__(message)
        self.status = status
        self.body = body


class BadRequest(ApiError):
    """The server answered ``400``."""


class NotFound(ApiError):
    """The server answered ``404``."""


class TransportError(ApiError):
    """Network failure or ``5xx`` that persisted through every retry."""


class AuthError(ApiError):
    """Authentication or authorisation failure (``401`` / ``403``)."""

    exit_code = EXIT_AUTH


class Unauthorized(AuthError):
    """The server answered ``401``: missing or invalid API key."""


class Forbidden(AuthError):
    """The server answered ``403``: the key's user may not perform this curation."""


class BudgetError(OacurateError):
    """A credit or rate budget stopped the run."""

    exit_code = EXIT_BUDGET


class BudgetExceeded(BudgetError):
    """The next billed request would exceed ``--max_credits``; it was not sent."""


class BudgetExhausted(BudgetError):
    """The server reports the daily credit budget as exhausted."""

    def __init__(self, message: str, *, reset_at: datetime | None = None) -> None:
        """Keep the local time at which the budget resets, when known."""
        super().__init__(message)
        self.reset_at = reset_at


class RateLimited(BudgetError):
    """``429`` persisted after honouring ``Retry-After`` the maximum number of times."""
