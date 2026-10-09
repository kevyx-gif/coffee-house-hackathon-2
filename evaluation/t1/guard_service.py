"""Disposable, loopback-only Qwen3Guard service used to close T1 gates."""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL_ID = "Qwen/Qwen3Guard-Gen-0.6B"
SNAPSHOT_ID = "fada3b2f655b89601929198343c94cd2f64d93cc"
DEFAULT_SNAPSHOT = (
    Path.home() / ".cache" / "huggingface" / "hub"
    / "models--Qwen--Qwen3Guard-Gen-0.6B" / "snapshots" / SNAPSHOT_ID
)


def parse_label(text: str) -> str:
    """Accept only a documented label plus known optional metadata lines."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines or lines[0] not in {
        "Safety: Safe",
        "Safety: Unsafe",
        "Safety: Controversial",
    }:
        return "Unknown"
    allowed_categories = {
        "Violent",
        "Non-violent Illegal Acts",
        "Sexual Content or Sexual Acts",
        "PII",
        "Suicide & Self-Harm",
        "Unethical Acts",
        "Politically Sensitive Topics",
        "Copyright Violation",
        "Jailbreak",
        "None",
    }
    seen = set()
    for line in lines[1:]:
        if line.startswith("Categories: ") and "categories" not in seen:
            values = [part.strip() for part in line.removeprefix("Categories: ").split(",")]
            if not values or any(value not in allowed_categories for value in values):
                return "Unknown"
            seen.add("categories")
        elif re.fullmatch(r"Refusal: (?:Yes|No)", line) and "refusal" not in seen:
            seen.add("refusal")
        else:
            return "Unknown"
    return lines[0].removeprefix("Safety: ")


class Classifier:
    def __init__(self, snapshot: Path):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        torch.set_num_threads(2)
        try:
            torch.set_num_interop_threads(1)
        except RuntimeError:
            pass
        started = time.perf_counter()
        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True)
        self.model = AutoModelForCausalLM.from_pretrained(
            snapshot, local_files_only=True, torch_dtype=torch.float32, low_cpu_mem_usage=True
        )
        self.model.eval()
        self.inference_lock = threading.Lock()
        self.ready_seconds = round(time.perf_counter() - started, 3)

    def classify(self, user_text: str, assistant_text: str | None = None) -> str:
        messages = [{"role": "user", "content": user_text}]
        if assistant_text is not None:
            messages.append({"role": "assistant", "content": assistant_text})
        prompt = self.tokenizer.apply_chat_template(messages, tokenize=False)
        inputs = self.tokenizer([prompt], return_tensors="pt")
        with self.inference_lock, self.torch.inference_mode():
            generated = self.model.generate(**inputs, max_new_tokens=48, do_sample=False)
        completion = self.tokenizer.decode(
            generated[0][inputs.input_ids.shape[-1] :], skip_special_tokens=True
        )
        return parse_label(completion)


def make_handler(classifier: Classifier):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CoffeeHouseT1/1"

        def log_message(self, _format, *_args):
            # Never log request bodies, which may contain conversation text.
            return

        def send_json(self, status: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/health":
                self.send_json(404, {"status": "not_found"})
                return
            self.send_json(
                200,
                {
                    "status": "ready",
                    "model": MODEL_ID,
                    "snapshot": SNAPSHOT_ID,
                    "load_seconds": classifier.ready_seconds,
                },
            )

        def do_POST(self):
            if self.path != "/classify":
                self.send_json(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 32768:
                    raise ValueError
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict) or set(payload) - {"user", "assistant"}:
                    raise ValueError
                user_text = payload.get("user")
                assistant_text = payload.get("assistant")
                if not isinstance(user_text, str) or not user_text.strip() or len(user_text) > 12000:
                    raise ValueError
                if assistant_text is not None and (
                    not isinstance(assistant_text, str) or len(assistant_text) > 12000
                ):
                    raise ValueError
            except (ValueError, TypeError, json.JSONDecodeError):
                self.send_json(400, {"error": "invalid_request"})
                return
            try:
                label = classifier.classify(user_text, assistant_text)
            except Exception:  # noqa: BLE001 - every inference failure must become a safe hold
                self.send_json(503, {"error": "classification_unavailable"})
                return
            self.send_json(200, {"label": label, "allowed": label == "Safe"})

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--snapshot", default=os.environ.get("QWEN3GUARD_SNAPSHOT", str(DEFAULT_SNAPSHOT)))
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1"}:
        raise SystemExit("T1 guard service must remain on loopback")
    snapshot = Path(args.snapshot)
    if not snapshot.is_dir() or not (snapshot / "config.json").is_file():
        raise SystemExit("Pinned local model snapshot is unavailable")
    classifier = Classifier(snapshot)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(classifier))
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
