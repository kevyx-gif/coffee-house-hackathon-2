import asyncio
import hashlib
import hmac
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from coffee_house.hackathon2.guard import GuardDecision
from coffee_house.hackathon2.jobs import DataRetentionWorker
from coffee_house.hackathon2.runtime import create_h2_app
from coffee_house.hackathon2.storage import H2Store, utc_now
from coffee_house.hackathon2.whatsapp import WhatsAppSettings


class FakeModel:
    def __init__(self):
        self.closed = False

    async def route(self, _text):
        raise AssertionError("La prueba de arranque no debe llamar al modelo.")

    async def aclose(self):
        self.closed = True


class FakeGuard:
    def __init__(self):
        self.closed = False

    async def classify_async(self, *_args):
        raise AssertionError("La prueba de arranque no debe llamar a la guardia.")

    async def aclose(self):
        self.closed = True


class SafeGuard:
    def __init__(self):
        self.calls = []

    async def classify_async(self, user_text, assistant_text=None):
        self.calls.append((user_text, assistant_text))
        return GuardDecision("Safe", True)

    async def aclose(self):
        pass


class MenuModel:
    def __init__(self):
        self.calls = []

    async def route(self, text):
        self.calls.append(text)
        return SimpleNamespace(name="search_menu", arguments={"query": "latte"})

    async def aclose(self):
        pass


class FakeCloud:
    def __init__(self):
        self.deliveries = []

    async def send_text(self, *_args):
        raise AssertionError("El recorrido de consulta no debe enviar un OTP.")

    async def send_next_outbox(self, store):
        item = store.claim_outbox()
        if item is None:
            return None
        provider_id = f"wamid.SYNTHETIC{len(self.deliveries) + 1:04d}"
        store.finish_outbox(item["outbox_id"], "sent", provider_message_id=provider_id)
        self.deliveries.append((item["recipient_phone"], item["message_text"], provider_id))
        return "sent"

    async def aclose(self):
        pass


def test_runtime_starts_and_closes_isolated_app_without_external_effects(tmp_path):
    model, guard = FakeModel(), FakeGuard()
    app = create_h2_app(
        catalog_path="data/catalog.json",
        fixture_dir="data/demo-hackathon2",
        database_path=tmp_path / "hackathon2.sqlite3",
        whatsapp=WhatsAppSettings(
            verify_token="synthetic-verify-token",
            app_secret="synthetic-app-secret",
            # A token in the environment alone must not enable outbound calls.
            access_token="synthetic-access-token",
            phone_number_id="123456789012345",
        ),
        guard_url="http://127.0.0.1:19091",
        otp_pepper="p" * 40,
        model=model,
        guard=guard,
    )

    expired_phone = "+521555010031"
    app.state.h2_store.save_conversation(
        expired_phone, {"message": "synthetic expired content"},
        expires_at="2000-01-01T00:00:00.000+00:00",
    )
    assert app.state.h2_external_delivery_enabled is False
    assert app.state.h2_external_text_enabled is False
    assert app.state.h2_handler.cloud is None
    with TestClient(app) as client:
        response = client.get("/webhook", params={
            "hub.mode": "subscribe",
            "hub.verify_token": "synthetic-verify-token",
            "hub.challenge": "123456",
        })
        assert response.status_code == 200
        assert response.text == "123456"

    assert model.closed is True
    assert guard.closed is True
    assert (tmp_path / "hackathon2.sqlite3").is_file()
    with sqlite3.connect(tmp_path / "hackathon2.sqlite3") as connection:
        expired_row = connection.execute(
            "SELECT 1 FROM conversations WHERE phone=?", (expired_phone,),
        ).fetchone()
    assert expired_row is None


def test_retention_worker_redacts_expired_data_after_startup_periodically(tmp_path):
    db_path = tmp_path / "retention.sqlite3"
    store = H2Store(db_path)
    phone = "+521555010032"
    worker = DataRetentionWorker(store, poll_interval=0.05)

    async def exercise():
        await worker.start()
        try:
            store.save_conversation(
                phone, {"message": "synthetic expired content"},
                expires_at="2000-01-01T00:00:00.000+00:00",
            )
            deadline = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < deadline:
                with sqlite3.connect(db_path) as connection:
                    exists = connection.execute(
                        "SELECT 1 FROM conversations WHERE phone=?", (phone,),
                    ).fetchone()
                if exists is None:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("El servicio no limpió la conversación vencida durante el intervalo de prueba.")
        finally:
            await worker.stop()

    asyncio.run(exercise())


def test_runtime_refuses_explicit_meta_delivery_without_a_client_or_token(tmp_path):
    with pytest.raises(ValueError, match="cliente de WhatsApp"):
        create_h2_app(
            catalog_path="data/catalog.json",
            fixture_dir="data/demo-hackathon2",
            database_path=tmp_path / "hackathon2.sqlite3",
            whatsapp=WhatsAppSettings(
                verify_token="synthetic-verify-token",
                app_secret="synthetic-app-secret",
                access_token="",
                phone_number_id="123456789012345",
            ),
            guard_url="http://127.0.0.1:19091",
            otp_pepper="p" * 40,
            enable_meta_delivery=True,
        )


def test_runtime_startup_recovers_interrupted_query_without_replaying_it(tmp_path):
    database_path = tmp_path / "hackathon2.sqlite3"
    store = H2Store(database_path)
    event_id = "message:wamid.RESTART_TEST1"
    payload = {
        "kind": "message",
        "message_id": "wamid.RESTART_TEST1",
        "sender": "+521555010009",
        "timestamp": "1791547200",
        "type": "text",
        "text": "Consulta sintética para recuperación.",
    }
    received_at = utc_now()
    assert store.admit_webhook_event(event_id, payload, received_at=received_at) == "queued"
    assert store.claim_assistant_job("worker-before-restart", now=received_at)

    model, guard = FakeModel(), FakeGuard()
    app = create_h2_app(
        catalog_path="data/catalog.json",
        fixture_dir="data/demo-hackathon2",
        database_path=database_path,
        whatsapp=WhatsAppSettings(
            verify_token="synthetic-verify-token",
            app_secret="synthetic-app-secret",
            access_token="synthetic-access-token",
            phone_number_id="123456789012345",
        ),
        guard_url="http://127.0.0.1:19091",
        otp_pepper="p" * 40,
        model=model,
        guard=guard,
    )

    with TestClient(app):
        recovered = app.state.h2_store
        assert recovered.assistant_queue_snapshot() == {
            "active": 0, "waiting": 0, "cancel_requested": 0, "uncertain": 1
        }
        assert recovered.claim_assistant_job("worker-after-restart") is None
        with recovered._connect() as connection:
            job_status = connection.execute(
                "SELECT status FROM assistant_jobs WHERE event_id=?", (event_id,)
            ).fetchone()["status"]
            notices = connection.execute(
                "SELECT status,message_text FROM outbox ORDER BY outbox_id"
            ).fetchall()
        assert job_status == "uncertain"
        assert len(notices) == 1
        assert notices[0]["status"] == "queued"
        assert "interrumpió" in notices[0]["message_text"]

    assert model.closed is True
    assert guard.closed is True


def test_signed_webhook_runs_through_queue_guard_model_catalog_and_outbox(tmp_path):
    app_secret = "synthetic-app-secret-for-e2e"
    phone_number_id = "123456789012345"
    verify_token = "synthetic-verify-token"
    guard, model, cloud = SafeGuard(), MenuModel(), FakeCloud()
    app = create_h2_app(
        catalog_path="data/catalog.json",
        fixture_dir="data/demo-hackathon2",
        database_path=tmp_path / "hackathon2.sqlite3",
        whatsapp=WhatsAppSettings(
            verify_token=verify_token,
            app_secret=app_secret,
            access_token="synthetic-access-token",
            phone_number_id=phone_number_id,
        ),
        guard_url="http://127.0.0.1:19091",
        otp_pepper="p" * 40,
        allow_external_text=True,
        enable_meta_delivery=True,
        model=model,
        guard=guard,
        cloud_client=cloud,
    )
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{
            "field": "messages",
            "value": {
                "metadata": {"phone_number_id": phone_number_id},
                "messages": [{
                    "id": "wamid.H2E2E0001",
                    "from": "5215550000099",
                    "timestamp": "1791547200",
                    "type": "text",
                    "text": {"body": "¿Qué tipos de latte tienen?"},
                }],
            },
        }]}],
    }
    raw_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signature = "sha256=" + hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()

    with TestClient(app) as client:
        response = client.post("/webhook", content=raw_body, headers={"x-hub-signature-256": signature})
        assert response.status_code == 200
        assert response.json() == {"received": 1, "queued": 1, "duplicates": 0, "deferred": 0}
        deadline = time.monotonic() + 3
        while not cloud.deliveries and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(cloud.deliveries) == 1
        recipient, message, provider_id = cloud.deliveries[0]
        assert recipient == "+525550000099"
        assert "Latte" in message and "MXN" in message
        assert provider_id == "wamid.SYNTHETIC0001"
        assert model.calls == ["¿Qué tipos de latte tienen?"]
        assert len(guard.calls) == 2

        store = app.state.h2_store
        with store._connect() as connection:
            event = connection.execute(
                "SELECT status FROM webhook_events WHERE event_id=?",
                ("message:wamid.H2E2E0001",),
            ).fetchone()
            job = connection.execute(
                "SELECT status FROM assistant_jobs WHERE event_id=?",
                ("message:wamid.H2E2E0001",),
            ).fetchone()
            outbox = connection.execute(
                "SELECT status,provider_message_id FROM outbox WHERE provider_message_id=?",
                (provider_id,),
            ).fetchone()
        assert event["status"] == "done"
        assert job["status"] == "completed"
        assert tuple(outbox) == ("sent", provider_id)
