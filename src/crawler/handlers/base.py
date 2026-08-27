"""What a handler is: the registry, the shape every handler implements, and
the boundary between routed content and what a handler extracts from it.
worker.py depends on this module, not on any concrete handler — routing is
this registry's decision, not worker.py's, so a fifth type is one new file
and one `@register` decorator here, with no edit to worker.py or to any
other handler.
"""

from dataclasses import dataclass
from typing import Protocol

from ..store.frontier import DiscoveredLink


@dataclass(frozen=True, slots=True)
class HandlerResult:
    """What handling one body produced. `metadata` is `None` when a handler
    has nothing yet worth a `content_metadata` row for — not every field a
    handler could someday extract, just what it actually extracts today;
    `None` here means "not yet implemented", not "legitimately absent"
    (that case is a real payload with a null field and a reason — see
    CLAUDE.md's unknowable-field decision, which doesn't apply until a
    handler has fields to be missing from).
    """

    metadata: dict[str, object] | None
    links: list[DiscoveredLink]


class Handler(Protocol):
    """One instance per content family, held in the registry below.

    `kind` names the family for `content_metadata.kind` (e.g. "page").
    `directory`/`extension` name where a matched body's blob lands under
    `output_dir` — see CLAUDE.md's blob-naming decision. `content_type` is
    the one canonical string for the family (e.g. "text/html", never a
    variant carrying `; charset=...`) — what a matched body's `contents` row
    stores, regardless of whatever the response's own Content-Type header
    said. All four come from the handler that matched, never from the URL
    or the header, because the handler is what verified the content
    actually is what it claims to be.
    """

    kind: str
    directory: str
    extension: str
    content_type: str

    def sniff(self, body: bytes) -> bool:
        """True if `body`'s own bytes are this handler's type — magic bytes,
        never the declared Content-Type and never the URL. The one check
        that can't be spoofed by a wrong header (CLAUDE.md: "headers that
        lie").
        """
        ...

    def handle(self, body: bytes, url: str) -> HandlerResult: ...


_REGISTRY: dict[str, Handler] = {}


def register(cls: type[Handler]) -> type[Handler]:
    """Decorator: `@register` on a handler class files one instance of it
    under its own declared `content_type` — one string, declared once, in
    the class body next to `kind`/`directory`/`extension`. That filing is
    only a hint for `resolve` below to try first — the real routing
    decision is always `sniff`, since a declared Content-Type is exactly
    the thing that can lie.
    """
    _REGISTRY[cls.content_type] = cls()
    return cls


def resolve(content_type: str | None, body: bytes) -> Handler | None:
    """The routed handler for `body`, or `None` if nothing registered
    matches — the "skipped" case (DESIGN.md). Trusts `content_type` only as
    a hint about which handler to try first; every registered handler's
    `sniff` gets a chance regardless, so a mislabeled body still lands on
    the handler its bytes actually match. The `.split(";", 1)` below only
    steers that hint lookup — what a matched handler reports back through
    its own `.content_type` is always the bare literal declared on the
    class, so a header carrying `; charset=...` (or no header at all) never
    reaches `contents.content_type` either way.
    """
    hint = _REGISTRY.get(content_type.split(";", 1)[0].strip().lower()) if content_type else None
    if hint is not None and hint.sniff(body):
        return hint
    for handler in _REGISTRY.values():
        if handler is not hint and handler.sniff(body):
            return handler
    return None
