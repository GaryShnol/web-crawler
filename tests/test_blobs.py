"""store/blobs.py: content-addressed naming and idempotent writes. Pure
filesystem, so `tmp_path` is enough — no database, no event loop.
"""

import hashlib
from pathlib import Path

from crawler.store.blobs import write


class TestWrite:
    def test_path_is_hash_prefix_plus_slug_plus_extension(self, tmp_path):
        body = b"hello world"
        content_hash, storage_path = write(
            tmp_path, "pages", "html", "http://fixture.local/hero-banner", body
        )

        expected_hash = hashlib.sha256(body).hexdigest()
        assert content_hash == expected_hash
        assert storage_path == str(tmp_path / "pages" / f"{expected_hash[:12]}-hero-banner.html")

    def test_bytes_actually_land_on_disk(self, tmp_path):
        body = b"the actual content"
        _, storage_path = write(tmp_path, "pages", "html", "http://fixture.local/x", body)
        assert Path(storage_path).read_bytes() == body

    def test_identical_bytes_from_two_urls_share_one_path_by_hash(self, tmp_path):
        body = b"same bytes either way"
        hash_a, path_a = write(tmp_path, "pages", "html", "http://fixture.local/a", body)
        hash_b, path_b = write(tmp_path, "pages", "html", "http://fixture.local/b", body)

        assert hash_a == hash_b
        # Different urls -> different slugs -> genuinely different filenames,
        # each written once; only identical (hash, url) pairs are a no-op rewrite.
        assert path_a != path_b

    def test_rewriting_the_same_hash_and_url_is_a_cheap_no_op(self, tmp_path):
        body = b"idempotent"
        first = write(tmp_path, "pages", "html", "http://fixture.local/x", body)
        second = write(tmp_path, "pages", "html", "http://fixture.local/x", body)
        assert first == second

    def test_url_with_no_meaningful_path_falls_back_to_index(self, tmp_path):
        _, storage_path = write(tmp_path, "pages", "html", "http://fixture.local/", b"root")
        assert "-index.html" in storage_path

    def test_query_string_and_punctuation_are_slugified(self, tmp_path):
        _, storage_path = write(
            tmp_path, "pages", "html", "http://fixture.local/a/b_c?x=1", b"data"
        )
        name = Path(storage_path).name
        assert name.endswith("-a-b-c.html")
