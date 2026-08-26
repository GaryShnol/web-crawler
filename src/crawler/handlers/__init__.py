"""Registers every concrete handler by importing it — see base.py's
registry and its `@register` decorator. A handler module that isn't
imported here never registers itself, and silently matches nothing; this
is the one place a fifth handler's file has to be named, alongside its new
file and its one `@register(...)` decorator.
"""

from . import html as _html  # noqa: F401 — imported for its @register side effect
