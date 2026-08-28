"""Registers every concrete handler by importing it — see base.py's
registry and its `@register` decorator. A handler module that isn't
imported here never registers itself, and silently matches nothing; this
is the one place a fifth handler's file has to be named, alongside its new
file and its own bare `@register`.
"""

from . import gif as _gif  # noqa: F401 — imported for its @register side effect
from . import html as _html  # noqa: F401 — imported for its @register side effect
from . import image as _image  # noqa: F401 — imported for its @register side effect
from . import jpeg as _jpeg  # noqa: F401 — imported for its @register side effect
from . import pdf as _pdf  # noqa: F401 — imported for its @register side effect
from . import video as _video  # noqa: F401 — imported for its @register side effect
from . import webp as _webp  # noqa: F401 — imported for its @register side effect
