#!/bin/sh
# A fresh named volume mounted over /app/output is owned by root -- that
# happens at container start, after the image's own build-time chown, so
# it always wins. Only root can fix that up, which is why this runs before
# USER ever drops to appuser: chown the mount, then gosu hands off to the
# real process as appuser for good, the same as if it had run there all
# along (signals, exit code, PID 1 -- gosu execs, it doesn't fork).
set -e
mkdir -p /app/output
chown -R appuser:appuser /app/output
exec gosu appuser python -m crawler.cli "$@"
