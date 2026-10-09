import json

from evaluation.t1 import guard_client


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def read(self, _limit):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def health_response():
    return FakeResponse(
        json.dumps(
            {
                "status": "ready",
                "model": guard_client.MODEL_ID,
                "snapshot": guard_client.SNAPSHOT_ID,
            }
        ).encode()
    )


def test_malformed_http_response_fails_closed(monkeypatch):
    responses = [health_response(), FakeResponse(b"not-json")]
    monkeypatch.setattr(guard_client, "urlopen", lambda *_args, **_kwargs: responses.pop(0))
    assert not guard_client.GuardClient("http://127.0.0.1:18196").classify("Hola")


def test_http_timeout_after_health_fails_closed(monkeypatch):
    responses = [health_response()]

    def fake_urlopen(*_args, **_kwargs):
        if responses:
            return responses.pop(0)
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr(guard_client, "urlopen", fake_urlopen)
    assert not guard_client.GuardClient("http://127.0.0.1:18196").classify("Hola")


def test_mismatched_or_unexpected_health_fails_closed(monkeypatch):
    bad_health = FakeResponse(
        json.dumps(
            {
                "status": "ready",
                "model": guard_client.MODEL_ID,
                "snapshot": "different-model",
            }
        ).encode()
    )
    monkeypatch.setattr(guard_client, "urlopen", lambda *_args, **_kwargs: bad_health)
    assert not guard_client.GuardClient("http://127.0.0.1:18196").classify("Hola")
