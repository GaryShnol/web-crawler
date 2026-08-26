"""Writes a fetched body to disk, named by content — see CLAUDE.md's blob
naming decision. Pure filesystem and hashing, no database access: the
per-url transaction in worker.py never waits on a syscall to open it, and
this module has no idea `contents` exists.
"""

import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit

_HASH_PREFIX_LEN = 12
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(url: str) -> str:
    path = urlsplit(url).path
    slug = _SLUG_RE.sub("-", path.lower()).strip("-")
    return slug or "index"


def write(output_dir: Path, directory: str, extension: str, url: str, body: bytes) -> tuple[str, str]:
    """Writes `body` under `output_dir/directory/`, named by the first
    twelve characters of its sha256 plus a slug from `url`'s path. Returns
    `(content_hash, storage_path)` — the caller inserts both into
    `contents` inside its own transaction; this function never touches the
    database.

    Idempotent: identical bytes for a different url land on the same path,
    a no-op if it's already there rather than a second write.
    """
    content_hash = hashlib.sha256(body).hexdigest()
    target_dir = output_dir / directory
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{content_hash[:_HASH_PREFIX_LEN]}-{_slug(url)}.{extension}"
    if not path.exists():
        path.write_bytes(body)
    return content_hash, str(path)
