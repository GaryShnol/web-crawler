"""logging.py: bound context and a call site's own `extra={"context": {...}}`
have to both land in the JSON output — a real bug (the context filter
overwrote instead of merging) meant the latter never did.
"""

import json
import logging

from crawler.logging import _ContextFilter, _JsonFormatter, bind

LOGGER_NAME = "test.logging"


def _emit(record_kwargs: dict) -> dict:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture()
    handler.addFilter(_ContextFilter())
    logger.addHandler(handler)

    logger.info("message", **record_kwargs)

    formatter = _JsonFormatter()
    return json.loads(formatter.format(captured[0]))


class TestContextFilter:
    def test_bound_context_lands_in_the_payload(self):
        with bind(url="http://fixture.local/"):
            payload = _emit({})
        assert payload["url"] == "http://fixture.local/"

    def test_no_bound_context_is_not_an_error(self):
        payload = _emit({})
        assert "url" not in payload

    def test_call_site_extra_context_lands_in_the_payload(self):
        payload = _emit({"extra": {"context": {"kind": "page", "links": 3}}})
        assert payload["kind"] == "page"
        assert payload["links"] == 3

    def test_bound_context_and_call_site_extra_both_land_together(self):
        with bind(url="http://fixture.local/", worker_id=1):
            payload = _emit({"extra": {"context": {"kind": "page"}}})
        assert payload["url"] == "http://fixture.local/"
        assert payload["worker_id"] == 1
        assert payload["kind"] == "page"

    def test_call_site_extra_wins_on_a_key_collision(self):
        with bind(kind="from-bind"):
            payload = _emit({"extra": {"context": {"kind": "from-call"}}})
        assert payload["kind"] == "from-call"
