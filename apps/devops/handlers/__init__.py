"""Регистрация handlers (импорт триггерит @register_handler)."""
from . import backup  # noqa: F401
from . import deploy  # noqa: F401
from . import disk_usage  # noqa: F401
from . import git_log  # noqa: F401
from . import noop  # noqa: F401
from . import pull_db  # noqa: F401
from . import rebuild  # noqa: F401
from . import restore_db  # noqa: F401
from . import rollback  # noqa: F401
from . import s3_stats  # noqa: F401
from . import status  # noqa: F401
