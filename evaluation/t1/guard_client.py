"""Strict fail-closed HTTP client and safe two-pass message subflow."""
from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MODEL_ID = "Qwen/Qwen3Guard-Gen-0.6B"
SNAPSHOT_ID = "fada3b2f655b89601929198343c94cd2f64d93cc"
VALID_LABELS = {"Safe", "Unsafe", "Controversial", "Unknown"}


def may_continue(payload: object) -> bool:
    """Only the exact pair Safe/true is accepted; malformed state is a hold."""
    if not isinstance(payload, dict) or set(payload) != {"label", "allowed"}:
        return False
    label, allowed = payload["label"], payload["allowed"]
    return type(allowed) is bool and label in VALID_LABELS and label == "Safe" and allowed


class GuardClient:
    def __init__(self, base_url: str, timeout: float = 20.0):
        if not base_url.startswith("http://127.0.0.1:"):
            raise ValueError("Guardia T1 solo puede consultarse por loopback")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def healthy(self) -> bool:
        try:
            with urlopen(f"{self.base_url}/health", timeout=min(self.timeout, 3.0)) as response:
                payload = json.loads(response.read(4096))
            return (
                response.status == 200
                and isinstance(payload, dict)
                and payload.get("status") == "ready"
                and payload.get("model") == MODEL_ID
                and payload.get("snapshot") == SNAPSHOT_ID
            )
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
            return False

    def classify(self, user: str, assistant: str | None = None) -> bool:
        return self.classify_with_label(user, assistant)[1]

    def classify_with_label(self, user: str, assistant: str | None = None) -> tuple[str, bool]:
        if not self.healthy():
            return "Unavailable", False
        body = {"user": user}
        if assistant is not None:
            body["assistant"] = assistant
        request = Request(
            f"{self.base_url}/classify",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read(4096))
            if response.status != 200 or not isinstance(payload, dict):
                return "Unknown", False
            label = payload.get("label")
            return (label if isinstance(label, str) else "Unknown", may_continue(payload))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
            return "Unavailable", False

    def safe_subflow(self, user: str, responder) -> tuple[bool, str | None]:
        """Return text only after both actual guard decisions are safe."""
        if not self.classify(user):
            return False, None
        answer = responder(user)
        if not isinstance(answer, str) or not answer.strip():
            return False, None
        if not self.classify(user, answer):
            return False, None
        return True, answer
