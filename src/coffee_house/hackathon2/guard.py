"""Fail-closed client for the pinned, loopback-only Qwen3Guard service."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

MODEL_ID = "Qwen/Qwen3Guard-Gen-0.6B"
SNAPSHOT_ID = "fada3b2f655b89601929198343c94cd2f64d93cc"
VALID_LABELS = {"Safe", "Unsafe", "Controversial", "Unknown"}


@dataclass(frozen=True, slots=True)
class GuardDecision:
    label: str
    allowed: bool


class QwenGuardClient:
    """Only exact `Safe` plus boolean true from the pinned local service is allowed."""

    def __init__(self, base_url: str, *, timeout: float = 20.0, client: httpx.Client | None = None):
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"} or not parsed.port:
            raise ValueError("La guardia solo puede consultarse por loopback y puerto explícito.")
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("La dirección de guardia no es válida.")
        if not 0.1 <= timeout <= 30:
            raise ValueError("El tiempo de espera de la guardia no es válido.")
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        self._base_url = f"http://{host}:{parsed.port}"
        self._timeout = timeout
        self._client = client or httpx.Client(timeout=timeout, trust_env=False)
        self._owns_client = client is None
        self._async_client: httpx.AsyncClient | None = None
        self._async_client_lock = asyncio.Lock()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    async def aclose(self) -> None:
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None

    def healthy(self) -> bool:
        try:
            response = self._client.get(f"{self._base_url}/health", timeout=min(self._timeout, 3.0))
            payload = response.json()
            return (
                response.status_code == 200
                and isinstance(payload, dict)
                and payload.get("status") == "ready"
                and payload.get("model") == MODEL_ID
                and payload.get("snapshot") == SNAPSHOT_ID
            )
        except (httpx.HTTPError, ValueError, json.JSONDecodeError):
            return False

    def classify(self, user_text: str, assistant_text: str | None = None) -> GuardDecision:
        if not self.healthy():
            return GuardDecision("Unavailable", False)
        body: dict[str, str] = {"user": user_text}
        if assistant_text is not None:
            body["assistant"] = assistant_text
        try:
            response = self._client.post(
                f"{self._base_url}/classify", json=body, timeout=self._timeout
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError, json.JSONDecodeError):
            return GuardDecision("Unavailable", False)
        if response.status_code != 200 or not isinstance(payload, dict) or set(payload) != {"label", "allowed"}:
            return GuardDecision("Unknown", False)
        label, allowed = payload["label"], payload["allowed"]
        if not isinstance(label, str) or label not in VALID_LABELS or type(allowed) is not bool:
            return GuardDecision("Unknown", False)
        return GuardDecision(label, label == "Safe" and allowed)

    async def classify_async(self, user_text: str, assistant_text: str | None = None) -> GuardDecision:
        """Async variant so a cancelled customer query can stop the guard request too."""
        if self._async_client is None:
            async with self._async_client_lock:
                if self._async_client is None:
                    self._async_client = httpx.AsyncClient(
                        timeout=httpx.Timeout(self._timeout, connect=min(self._timeout, 4.0)), trust_env=False
                    )
        client = self._async_client
        try:
            response = await client.get(f"{self._base_url}/health", timeout=min(self._timeout, 3.0))
            health = response.json()
        except (httpx.HTTPError, ValueError, json.JSONDecodeError):
            return GuardDecision("Unavailable", False)
        if not (
            response.status_code == 200 and isinstance(health, dict)
            and health.get("status") == "ready" and health.get("model") == MODEL_ID
            and health.get("snapshot") == SNAPSHOT_ID
        ):
            return GuardDecision("Unavailable", False)
        body: dict[str, str] = {"user": user_text}
        if assistant_text is not None:
            body["assistant"] = assistant_text
        try:
            response = await client.post(
                f"{self._base_url}/classify", json=body, timeout=self._timeout
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError, json.JSONDecodeError):
            return GuardDecision("Unavailable", False)
        if response.status_code != 200 or not isinstance(payload, dict) or set(payload) != {"label", "allowed"}:
            return GuardDecision("Unknown", False)
        label, allowed = payload["label"], payload["allowed"]
        if not isinstance(label, str) or label not in VALID_LABELS or type(allowed) is not bool:
            return GuardDecision("Unknown", False)
        return GuardDecision(label, label == "Safe" and allowed)
