"""Tests for src/crawler/url_tools.py — pure functions, no I/O, no config."""

from crawler.url_tools import in_scope, normalize


class TestNormalize:
    def test_default_http_port_dropped(self):
        assert normalize("http://Example.com:80/path") == "http://example.com/path"

    def test_default_https_port_dropped(self):
        assert normalize("https://Example.com:443/path") == "https://example.com/path"

    def test_non_default_port_kept(self):
        assert normalize("http://example.com:8080/path") == "http://example.com:8080/path"

    def test_scheme_and_host_lowercased_path_case_kept(self):
        assert normalize("HTTP://EXAMPLE.COM/Path") == "http://example.com/Path"

    def test_fragment_dropped(self):
        assert normalize("http://example.com/path#section") == "http://example.com/path"

    def test_dot_segments_resolved(self):
        assert normalize("http://example.com/a/b/../c/./d") == "http://example.com/a/c/d"

    def test_dot_segments_above_root_are_dropped(self):
        assert normalize("http://example.com/../c") == "http://example.com/c"

    def test_trailing_slash_preserved_through_dot_segment_removal(self):
        assert normalize("http://example.com/a/b/../") == "http://example.com/a/"

    def test_protocol_relative_resolved_against_base(self):
        result = normalize("//CDN.example.com/Img.png?X=1", base="http://seed.com/page")
        assert result == "http://cdn.example.com/Img.png?X=1"

    def test_relative_ref_resolved_against_base_href(self):
        # a <base href> overrides the page's own URL upstream; normalize itself has
        # no notion of "the page URL", it just honors whatever base it's handed
        result = normalize("img/x.png", base="http://cdn.example.com/other/")
        assert result == "http://cdn.example.com/other/img/x.png"

    def test_absolute_url_ignores_base(self):
        result = normalize("http://elsewhere.com/x", base="http://seed.com/page")
        assert result == "http://elsewhere.com/x"

    def test_relative_ref_with_no_base_stays_relative(self):
        assert normalize("../images/x.png") == "../images/x.png"

    def test_relative_ref_with_no_base_still_drops_fragment(self):
        assert normalize("page?x=1#frag") == "page?x=1"

    def test_percent_encoded_slash_in_query_is_never_decoded(self):
        result = normalize("http://example.com/search?q=a%2fb")
        assert result == "http://example.com/search?q=a%2Fb"

    def test_percent_encoded_unreserved_char_is_decoded(self):
        assert normalize("http://example.com/a%7Eb") == "http://example.com/a~b"

    def test_percent_encoded_reserved_char_hex_uppercased_but_kept(self):
        assert normalize("http://example.com/a%3ab") == "http://example.com/a%3Ab"

    def test_query_params_not_reordered(self):
        assert normalize("http://example.com/p?b=2&a=1") == "http://example.com/p?b=2&a=1"

    def test_mailto_scheme_lowercased_rest_untouched(self):
        result = normalize("MAILTO:User@Example.COM?subject=Hi")
        assert result == "mailto:User@Example.COM?subject=Hi"

    def test_non_http_scheme_fragment_not_stripped(self):
        assert normalize("MAILTO:a@b.com#x") == "mailto:a@b.com#x"


class TestInScope:
    def test_same_host_in_scope(self):
        assert in_scope("http://seed.com/page", "seed.com", allow_subdomains=False) is True

    def test_different_host_out_of_scope(self):
        assert in_scope("http://other.com/page", "seed.com", allow_subdomains=False) is False

    def test_subdomain_out_of_scope_by_default(self):
        assert in_scope("http://cdn.seed.com/x", "seed.com", allow_subdomains=False) is False

    def test_subdomain_in_scope_when_allowed(self):
        assert in_scope("http://cdn.seed.com/x", "seed.com", allow_subdomains=True) is True

    def test_seed_host_in_scope_regardless_of_flag(self):
        assert in_scope("http://seed.com/x", "seed.com", allow_subdomains=True) is True

    def test_lookalike_host_not_matched_as_subdomain(self):
        assert in_scope("http://evilseed.com/x", "seed.com", allow_subdomains=True) is False

    def test_host_comparison_is_case_insensitive(self):
        assert in_scope("http://SEED.com/x", "seed.com", allow_subdomains=False) is True

    def test_mailto_has_no_host_so_out_of_scope(self):
        assert in_scope("mailto:a@seed.com", "seed.com", allow_subdomains=True) is False
