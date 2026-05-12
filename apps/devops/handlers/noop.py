"""Заглушка для этапа 3.2: возвращает что приняли params. Реальные handlers — этап 3.3+."""
from apps.devops.tasks import register_handler


@register_handler("noop")
def run_noop(params: dict) -> dict:
    return {
        "output": f"noop handler executed with params={params}\n",
        "result": {"echo": params},
    }
