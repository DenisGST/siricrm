"""HTTP-клиент для общения с агентом целевого окружения.

Берёт Environment, читает токен из env-переменной agent_token_env, шлёт запросы.
"""
import os
from typing import Any

import requests

from .models import Environment


class AgentError(RuntimeError):
    """Понятная ошибка ответа агента (с телом ответа и, если есть, кодом ошибки)."""

    def __init__(self, message: str, status: int | None = None, payload: dict | None = None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


class AgentClient:
    """Тонкая обёртка над requests с Bearer-токеном."""

    def __init__(self, env: Environment, timeout: float = 15.0):
        self.env = env
        self.timeout = timeout
        self.base_url = env.base_url.rstrip("/")
        self.token = os.environ.get(env.agent_token_env, "")
        if not self.token:
            raise RuntimeError(f"Переменная окружения с токеном ({env.agent_token_env}) пуста")

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _check(self, r: requests.Response, what: str) -> dict[str, Any]:
        if r.ok:
            try:
                return r.json()
            except ValueError:
                return {}
        # Достаём осмысленное тело ответа агента, если оно есть.
        payload = {}
        try:
            payload = r.json()
        except ValueError:
            pass
        err = payload.get("error") if isinstance(payload, dict) else None
        if err == "unauthorized":
            msg = (f"Агент {self.env.name} ({self.base_url}) ответил 401 — неверный токен "
                   f"(env {self.env.agent_token_env}).")
        elif err == "unknown_action_type":
            avail = ", ".join(payload.get("available", [])) or "?"
            msg = (f"На окружении {self.env.name} ещё нет действия — его код устарел. "
                   f"Сначала задеплой на {self.env.name}. Доступные сейчас: {avail}.")
        else:
            body = (r.text or "")[:300]
            msg = f"Агент {self.env.name} ({self.base_url}) ответил HTTP {r.status_code} на {what}: {body}"
        raise AgentError(msg, status=r.status_code, payload=payload if isinstance(payload, dict) else {})

    def ping(self) -> dict[str, Any]:
        r = requests.get(f"{self.base_url}/devops/agent/ping/",
                         headers=self._headers, timeout=self.timeout)
        return self._check(r, "ping")

    def create_job(self, action_type: str, params: dict | None = None) -> dict[str, Any]:
        r = requests.post(
            f"{self.base_url}/devops/agent/jobs/",
            json={"action_type": action_type, "params": params or {}},
            headers=self._headers,
            timeout=self.timeout,
        )
        return self._check(r, f"создание job {action_type}")

    def get_job(self, job_id: str) -> dict[str, Any]:
        r = requests.get(
            f"{self.base_url}/devops/agent/jobs/{job_id}/",
            headers=self._headers,
            timeout=self.timeout,
        )
        return self._check(r, f"опрос job {job_id}")
