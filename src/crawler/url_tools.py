"""Pure URL normalization and scope checks. No I/O, no logging, nothing from config."""

from urllib.parse import urljoin, urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}
_UNRESERVED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _normalize_pct(s: str) -> str:
    """Uppercase every %XX escape's hex digits; decode it only if it's unreserved."""
    out: list[str] = []
    i = 0
    while i < len(s):
        if s[i] == "%" and i + 2 < len(s) and s[i + 1 : i + 3].isalnum():
            hex_pair = s[i + 1 : i + 3]
            try:
                decoded = chr(int(hex_pair, 16))
            except ValueError:
                out.append(s[i])
                i += 1
                continue
            out.append(decoded if decoded in _UNRESERVED else f"%{hex_pair.upper()}")
            i += 3
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 5.2.4, minus the base case: only ever called on an absolute path."""
    out: list[str] = []
    for segment in path.split("/"):
        if segment == "..":
            if out and out[-1] != "":
                out.pop()
        elif segment != ".":
            out.append(segment)
    return "/".join(out)


def normalize(url: str, base: str | None = None) -> str:
    """Normalize scheme/host casing, default ports, ../, and percent-encoding.

    Query parameter order is left as-is: reordering could merge two URLs that
    serve different content, and that loss is silent and unrecoverable, while
    leaving them apart costs at most one redundant fetch (see CLAUDE.md).
    """
    parsed = urlsplit(url)

    if parsed.scheme and parsed.scheme not in ("http", "https"):
        # mailto:, tel:, etc. have no authority or path to normalize — touch
        # only the scheme and leave the rest exactly as given.
        return f"{parsed.scheme.lower()}:{url[len(parsed.scheme) + 1:]}"

    if not parsed.scheme and base is None:
        # A relative or protocol-relative ref with nothing to resolve it
        # against stays a relative ref — only fragment/percent-encoding apply.
        return _normalize_pct(url.split("#", 1)[0])

    resolved = urljoin(base, url) if base else url
    parsed = urlsplit(resolved)

    scheme = parsed.scheme.lower()
    port = parsed.port
    if port == _DEFAULT_PORTS.get(scheme):
        port = None
    netloc = parsed.hostname or ""
    if parsed.username:
        userinfo = parsed.username + (f":{parsed.password}" if parsed.password else "")
        netloc = f"{userinfo}@{netloc}"
    if port is not None:
        netloc = f"{netloc}:{port}"

    path = _normalize_pct(_remove_dot_segments(parsed.path))
    query = _normalize_pct(parsed.query)

    return urlunsplit((scheme, netloc, path, query, ""))


def in_scope(url: str, seed_host: str, allow_subdomains: bool) -> bool:
    """True if url's host is the seed host, or a subdomain of it when allowed."""
    host = urlsplit(url).hostname
    if not host:
        return False
    host = host.lower()
    seed_host = seed_host.lower()
    return host == seed_host or (allow_subdomains and host.endswith(f".{seed_host}"))
