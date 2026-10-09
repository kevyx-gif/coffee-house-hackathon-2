import asyncio
import hashlib
import hmac
import json
import time

import httpx
from fastapi.testclient import TestClient

from coffee_house.hackathon2.conversation import ConversationReply
from coffee_house.hackathon2.jobs import AssistantQueue
from coffee_house.hackathon2.storage import H2Store, utc_now
from coffee_house.hackathon2.whatsapp import (
    WhatsAppCloudClient,
    WhatsAppSettings,
    create_whatsapp_app,
    normalize_webhook,
    valid_signature,
)

APP_SECRET = "test-app-secret-never-real"
VERIFY_TOKEN = "test-verify-token-never-real"
ACCESS_TOKEN = "test-access-token-never-real"
PHONE_ID = "123456789012345"
CUSTOMER = "5215550000003"
NOW = "2026-10-09T12:00:00+00:00"


def settings(**overrides):
    values = {
        "verify_token": VERIFY_TOKEN,
        "app_secret": APP_SECRET,
        "access_token": ACCESS_TOKEN,
        "phone_number_id": PHONE_ID,
        "graph_api_version": "v23.0",
    }
    values.update(overrides)
    return WhatsAppSettings(**values)


def envelope(messages=None, statuses=None):
    value = {"metadata": {"phone_number_id": PHONE_ID}}
    if messages is not None:
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": value}]}]}


def signed(body):
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_challenge_requires_matching_mode_and_token(tmp_path):
    client = TestClient(create_whatsapp_app(settings(), H2Store(tmp_path / "h2.sqlite3")))
    good = client.get("/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "987654321"
    })
    wrong = client.get("/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "987654321"
    })
    assert good.status_code == 200 and good.text == "987654321"
    assert wrong.status_code == 403


def test_signature_checks_exact_raw_bytes_and_missing_secret_fails_closed():
    raw = b'{"object":"whatsapp_business_account"}'
    signature = signed(raw)
    assert valid_signature(raw, signature, APP_SECRET)
    assert not valid_signature(raw + b" ", signature, APP_SECRET)
    assert not valid_signature(raw, "sha256=" + "0" * 64, APP_SECRET)
    assert not valid_signature(raw, signature, "")
    assert not valid_signature(raw, "", APP_SECRET)


def test_valid_message_is_durable_and_duplicate_delivery_is_idempotent(tmp_path):
    store = H2Store(tmp_path / "h2.sqlite3")
    client = TestClient(create_whatsapp_app(settings(), store))
    body = json.dumps(envelope(messages=[{
        "id": "wamid.HBgLTEST001", "from": CUSTOMER, "timestamp": "1791547200",
        "type": "text", "text": {"body": "Hola, ¿qué lattes tienen?"}
    }]), ensure_ascii=False).encode()
    headers = {"x-hub-signature-256": signed(body), "content-type": "application/json"}
    first = client.post("/webhook", content=body, headers=headers)
    again = client.post("/webhook", content=body, headers=headers)
    assert first.status_code == 200 and first.json() == {"received": 1, "queued": 1, "duplicates": 0, "deferred": 0}
    assert again.status_code == 200 and again.json() == {"received": 1, "queued": 0, "duplicates": 1, "deferred": 0}
    event = store.claim_assistant_job("test-worker", now=utc_now())
    assert event["event_id"] == "message:wamid.HBgLTEST001"
    assert event["payload"]["sender"] == "+" + CUSTOMER
    assert event["payload"]["text"] == "Hola, ¿qué lattes tienen?"
    assert store.claim_webhook_event(now=NOW) is None
    assert store.complete_assistant_job(event["job_id"], "Las opciones de latte son sintéticas.", now=NOW)
    assert store.claim_webhook_event(now=NOW) is None
    assert store.recover_webhook_events() == 0


def test_invalid_signature_phone_number_and_malformed_json_are_rejected(tmp_path):
    store = H2Store(tmp_path / "h2.sqlite3")
    client = TestClient(create_whatsapp_app(settings(), store))
    valid_body = json.dumps(envelope(messages=[{
        "id": "wamid.HBgLTEST002", "from": CUSTOMER, "timestamp": "1791547200",
        "type": "text", "text": {"body": "hola"}
    }])).encode()
    assert client.post("/webhook", content=valid_body, headers={"x-hub-signature-256": "sha256=" + "0" * 64}).status_code == 401
    wrong_phone = envelope(messages=[{
        "id": "wamid.HBgLTEST003", "from": CUSTOMER, "timestamp": "1791547200",
        "type": "text", "text": {"body": "hola"}
    }])
    wrong_phone["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "wrong"
    body = json.dumps(wrong_phone).encode()
    assert client.post("/webhook", content=body, headers={"x-hub-signature-256": signed(body)}).status_code == 400
    malformed = b"{"
    assert client.post("/webhook", content=malformed, headers={"x-hub-signature-256": signed(malformed)}).status_code == 400
    assert store.claim_webhook_event(now=NOW) is None


def test_webhook_worker_completes_synthetic_message_and_keeps_delivery_local(tmp_path):
    class Handler:
        async def handle(self, phone, text, *, event_id=None):
            assert phone == "+" + CUSTOMER
            assert text == "Hola, ¿qué lattes tienen?"
            return ConversationReply("Respuesta sintética desde catálogo.", "answered", "test")

    store = H2Store(tmp_path / "h2.sqlite3")
    queue = AssistantQueue(store, Handler(), poll_interval=0.01)
    with TestClient(create_whatsapp_app(settings(), store, assistant_queue=queue)) as client:
        body = json.dumps(envelope(messages=[{
            "id": "wamid.HBgLWORKER01", "from": CUSTOMER, "timestamp": "1791547200",
            "type": "text", "text": {"body": "Hola, ¿qué lattes tienen?"}
        }]), ensure_ascii=False).encode()
        response = client.post("/webhook", content=body, headers={"x-hub-signature-256": signed(body)})
        assert response.status_code == 200 and response.json()["queued"] == 1
        until = time.monotonic() + 2
        status = None
        while time.monotonic() < until:
            with store._connect() as connection:
                row = connection.execute("SELECT status FROM assistant_jobs WHERE event_id='message:wamid.HBgLWORKER01'").fetchone()
            status = row["status"] if row else None
            if status == "completed":
                break
            time.sleep(0.01)
        assert status == "completed"
        with store._connect() as connection:
            message = connection.execute("SELECT message_text,status FROM outbox WHERE dedupe_key='assistant:message:wamid.HBgLWORKER01:reply'").fetchone()
        assert tuple(message) == ("Respuesta sintética desde catálogo.", "queued")


def test_payload_limit_and_media_allowlist(tmp_path):
    store = H2Store(tmp_path / "h2.sqlite3")
    client = TestClient(create_whatsapp_app(settings(), store))
    too_large = b" " * 65_537
    assert client.post("/webhook", content=too_large).status_code == 413
    image = {
        "id": "wamid.HBgLIMG001", "from": CUSTOMER, "timestamp": "1791547200",
        "type": "image", "image": {"id": "media_test_01", "mime_type": "image/jpeg"}
    }
    event_id, normalized = normalize_webhook(envelope(messages=[image]), PHONE_ID)[0]
    assert event_id == "message:wamid.HBgLIMG001"
    assert normalized == {
        "kind": "message", "message_id": "wamid.HBgLIMG001", "sender": "+" + CUSTOMER,
        "timestamp": "1791547200", "type": "image", "media_id": "media_test_01"
    }
    image["image"]["id"] = "bad id with spaces"
    try:
        normalize_webhook(envelope(messages=[image]), PHONE_ID)
    except ValueError:
        pass
    else:
        raise AssertionError("La referencia de medio inválida pasó la validación")


def test_out_of_order_statuses_are_normalized_and_deduplicated():
    statuses = [
        {"id": "wamid.OUT001", "status": "read", "timestamp": "1791547202", "recipient_id": CUSTOMER},
        {"id": "wamid.OUT001", "status": "sent", "timestamp": "1791547200", "recipient_id": CUSTOMER},
        {"id": "wamid.OUT001", "status": "delivered", "timestamp": "1791547201", "recipient_id": CUSTOMER},
        {"id": "wamid.OUT001", "status": "read", "timestamp": "1791547202", "recipient_id": CUSTOMER},
    ]
    events = normalize_webhook(envelope(statuses=statuses), PHONE_ID)
    assert [event[1]["status"] for event in events] == ["read", "sent", "delivered"]
    assert all(event[1]["kind"] == "status" for event in events)


def test_meta_send_success_is_persisted_without_token_leak(tmp_path):
    store = H2Store(tmp_path / "h2.sqlite3")
    outbox_id = store.enqueue_message("dedupe-1", "+" + CUSTOMER, "Confirmo tu pedido", now=NOW)
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["auth"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "wamid.OUTBOUND001"}]})

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            api = WhatsAppCloudClient(settings(), client=client)
            result = await api.send_next_outbox(store, now=NOW)
            return api, result

    api, result = asyncio.run(run())
    assert result == "sent"
    assert captured["url"].endswith(f"/{PHONE_ID}/messages")
    assert captured["auth"] == f"Bearer {ACCESS_TOKEN}"
    assert captured["body"]["to"] == CUSTOMER
    assert ACCESS_TOKEN not in repr(api)
    assert store.apply_delivery_status("wamid.OUTBOUND001", "delivered", now=NOW)
    assert store.apply_delivery_status("wamid.OUTBOUND001", "sent", now=NOW)
    with store._connect() as connection:
        row = connection.execute("SELECT status,provider_message_id,delivery_status FROM outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
    assert tuple(row) == ("sent", "wamid.OUTBOUND001", "delivered")


def test_meta_rejection_and_uncertain_timeout_are_not_retried(tmp_path):
    store = H2Store(tmp_path / "h2.sqlite3")
    rejected_id = store.enqueue_message("rejected", "+" + CUSTOMER, "Hola", now=NOW)
    uncertain_id = store.enqueue_message("uncertain", "+" + CUSTOMER, "Hola", now="2026-10-09T12:00:01+00:00")

    def rejection(request):
        return httpx.Response(400, json={"error": {"message": ACCESS_TOKEN, "code": 900001, "error_subcode": 900002}})

    async def run(handler, *, now=NOW):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            api = WhatsAppCloudClient(settings(), client=client)
            return await api.send_next_outbox(store, now=now)

    assert asyncio.run(run(rejection)) == "failed"

    def timeout(request):
        raise httpx.ReadTimeout("sensitive request details", request=request)

    assert asyncio.run(run(timeout, now="2026-10-09T12:00:02+00:00")) == "uncertain"
    with store._connect() as connection:
        rows = connection.execute("SELECT outbox_id,status,error_code FROM outbox ORDER BY created_at,outbox_id").fetchall()
    by_id = {row["outbox_id"]: (row["status"], row["error_code"]) for row in rows}
    assert by_id[rejected_id] == ("failed", "meta_http_400_graph_900001_sub_900002")
    assert by_id[uncertain_id] == ("uncertain", "meta_uncertain")
    assert "sensitive request details" not in repr(rows)
    assert ACCESS_TOKEN not in repr(rows)

    recovered_id = store.enqueue_message(
        "recovered-after-meta", "+" + CUSTOMER, "Aviso sintético tras recuperar Meta.",
        now="2026-10-09T12:00:03+00:00",
    )

    def success(_request):
        return httpx.Response(200, json={"messages": [{"id": "wamid.RECOVERED001"}]})

    assert asyncio.run(run(success, now="2026-10-09T12:00:04+00:00")) == "sent"
    with store._connect() as connection:
        recovered = connection.execute(
            "SELECT status,provider_message_id FROM outbox WHERE outbox_id=?", (recovered_id,)
        ).fetchone()
        uncertain = connection.execute(
            "SELECT status FROM outbox WHERE outbox_id=?", (uncertain_id,)
        ).fetchone()
    assert tuple(recovered) == ("sent", "wamid.RECOVERED001")
    assert uncertain["status"] == "uncertain"


def test_missing_secrets_fail_closed_and_never_appear_in_settings_repr():
    incomplete = settings(app_secret="", verify_token="")
    assert not valid_signature(b"anything", signed(b"anything"), incomplete.app_secret)
    assert APP_SECRET not in repr(settings())
    assert VERIFY_TOKEN not in repr(settings())
    assert ACCESS_TOKEN not in repr(settings())
    try:
        WhatsAppCloudClient(settings(access_token=""))
    except ValueError as exc:
        assert ACCESS_TOKEN not in str(exc)
    else:
        raise AssertionError("El cliente inició sin credencial")
