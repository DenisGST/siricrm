"""HTTP-клиент для общения с prod-агентом.

Берёт Environment, читает токен из env-переменной agent_token_env, шлёт запросы.
"""
import os
from typing import Any

import requests

from .models import Environment


class AgentClient:
    """Тонкая обёртка над requests с Bearer-токеном."""

    def __init__(self, env: Environment, timeout: float = 15.0):
        self.env = env
        self.timeout = timeout
        self.base_url = env.base_url.rstrip("/")
        self.token = os.environ.get(env.agent_token_env, "")
        if not self.token:
            raise RuntimeError(f"Token env var {env.agent_token_env} is empty")

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def ping(self) -> dict[str, Any]:
        r = requests.get(f"{self.base_url}/devops/agent/ping/",
                         headers=self._headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def create_job(self, action_type: str, params: dict | None = None) -> dict[str, Any]:
        r = requests.post(
            f"{self.base_url}/devops/agent/jobs/",
            json={"action_type": action_type, "params": params or {}},
            headers=self._headers,
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    def get_job(self, job_id: str) -> dict[str, Any]:
        r = requests.get(
            f"{self.base_url}/devops/agent/jobs/{job_id}/",
            headers=self._headers,
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()
