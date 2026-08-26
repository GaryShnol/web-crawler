"""The fetch API's response shape, and what we track ourselves about a fetch attempt."""

import enum
from dataclasses import dataclass


class Outcome(enum.Enum):
    """Closed set of fetch outcomes. errors.py maps status codes onto these; nothing else should."""

    SUCCESS = "success"
    PERMANENT_FAILURE = "permanent_failure"
    TEMPORARY_FAILURE = "temporary_failure"


@dataclass(frozen=True, slots=True)
class FetchResponse:
    """Mirrors the fetch API's response body exactly: three fields, nothing else.

    The API's own field is `statusCode`; the camelCase-to-snake_case translation
    happens in fetch/client.py when this is built, not here.
    """

    status_code: int
    headers: dict[str, str]
    body: bytes | None

    def header(self, name: str) -> str | None:
        """Case-insensitive header lookup — the API doesn't guarantee casing."""
        target = name.lower()
        for key, value in self.headers.items():
            if key.lower() == target:
                return value
        return None


@dataclass(frozen=True, slots=True)
class FetchResult:
    """What we measure ourselves about one fetch attempt, kept off FetchResponse."""

    outcome: Outcome
    elapsed: float
    attempt: int
    redirect_chain: list[str]
    response: FetchResponse | None
