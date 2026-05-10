"""Settings package: switches between dev and prod via DJANGO_ENV.

DJANGO_ENV=prod → config.settings.prod
otherwise → config.settings.dev
"""
import os

_env = os.environ.get("DJANGO_ENV", "dev").lower()
if _env == "prod":
    from .prod import *  # noqa: F401,F403
else:
    from .dev import *  # noqa: F401,F403
