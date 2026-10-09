import asyncio
from datetime import datetime, timedelta, timezone

from coffee_house.hackathon2.conversation import ConversationReply
from coffee_house.hackathon2.jobs import AssistantQueue, WebhookReceiptWorker
from coffee_house.hackathon2.storage import H2Store, utc_now


def inbound(index, *, phone=None, text="¿Qué lattes tienen?"):
    return (
        f"message:wamid.TEST{index:04d}",
        {
            "kind": "message", "message_id": f"wamid.TEST{index:04d}",
            "sender": phone or f"+52155501{index:04d}", "timestamp": "1791547200",
            "type": "text", "text": text,
        },
    )


def iso_after(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


class BlockingHandler:
    def __init__(self):
        self.active = 0
        self.peak = 0
        self.started = asyncio.Queue()
        self.release = asyncio.Event()

    async def handle(self, phone, text, *, event_id=None):
        self.active += 1
        self.peak = max(self.peak, self.active)
        await self.started.put((phone, text))
        try:
            await self.release.wait()
        finally:
            self.active -= 1
        return ConversationReply("Respuesta sintética.", "answered", "test")


class CancellableHandler:
    def __init__(self):
        self.started = asyncio.Event()
        self.was_cancelled = False

    async def handle(self, phone, text, *, event_id=None):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.was_cancelled = True
            raise


def test_queue_limits_three_active_ten_waiting_and_fifo(tmp_path):
    store = H2Store(tmp_path / "h2.sqlite3")
    received = utc_now()
    for number in range(3):
        event_id, payload = inbound(number)
        assert store.admit_webhook_event(event_id, payload, received_at=received) == "queued"
    claimed = [store.claim_assistant_job(f"worker-{number}", now=received) for number in range(4)]
    assert [job["payload"]["message_id"] for job in claimed[:3]] == [
        "wamid.TEST0000", "wamid.TEST0001", "wamid.TEST0002"
    ]
    assert claimed[3] is None
    for number in range(3, 13):
        event_id, payload = inbound(number)
        assert store.admit_webhook_event(event_id, payload, received_at=received) == "queued"
    event_id, payload = inbound(13)
    assert store.admit_webhook_event(event_id, payload, received_at=received) == "full"
    assert store.assistant_queue_snapshot() == {"active": 3, "waiting": 10, "cancel_requested": 0, "uncertain": 0}


def test_one_pending_query_per_person_and_queued_cancel_free_slot(tmp_path):
    store = H2Store(tmp_path / "h2.sqlite3")
    phone = "+521555019999"
    first_id, first_payload = inbound(1, phone=phone)
    second_id, second_payload = inbound(2, phone=phone)
    cancel_id, cancel_payload = inbound(3, phone=phone, text="cancelar consulta")
    assert store.admit_webhook_event(first_id, first_payload) == "queued"
    assert store.admit_webhook_event(second_id, second_payload) == "already_pending"
    assert store.assistant_queue_snapshot()["waiting"] == 1
    assert store.admit_webhook_event(cancel_id, cancel_payload) == "cancelled"
    assert store.assistant_queue_snapshot()["waiting"] == 0
    assert store.claim_assistant_job("worker") is None
    with store._connect() as connection:
        messages = connection.execute("SELECT recipient_phone,message_text FROM outbox ORDER BY dedupe_key").fetchall()
    assert all(message["recipient_phone"] == phone for message in messages)
    assert any("detuve" in message["message_text"] for message in messages)


def test_worker_runs_at_most_three_queries_at_once(tmp_path):
    async def scenario():
        store = H2Store(tmp_path / "h2.sqlite3")
        received = utc_now()
        for number in range(4):
            event_id, payload = inbound(number)
            store.admit_webhook_event(event_id, payload, received_at=received)
        handler = BlockingHandler()
        worker = AssistantQueue(store, handler, poll_interval=0.01)
        tasks = [asyncio.create_task(worker.process_one(str(number))) for number in range(4)]
        for _ in range(3):
            await asyncio.wait_for(handler.started.get(), timeout=1)
        await asyncio.sleep(0.03)
        assert handler.peak == 3
        assert store.assistant_queue_snapshot()["active"] == 3
        assert store.assistant_queue_snapshot()["waiting"] == 1
        handler.release.set()
        await asyncio.gather(*tasks)
        assert handler.active == 0
        assert store.assistant_queue_snapshot()["active"] == 0
        assert store.assistant_queue_snapshot()["waiting"] == 1
        assert await worker.process_one("last")
        assert store.assistant_queue_snapshot()["waiting"] == 0
    asyncio.run(scenario())


def test_running_cancel_stops_handler_before_ack_and_never_sends_result(tmp_path):
    async def scenario():
        store = H2Store(tmp_path / "h2.sqlite3")
        phone = "+521555010001"
        event_id, payload = inbound(1, phone=phone)
        assert store.admit_webhook_event(event_id, payload) == "queued"
        handler = CancellableHandler()
        worker = AssistantQueue(store, handler, poll_interval=0.01)
        task = asyncio.create_task(worker.process_one("cancel-test"))
        await asyncio.wait_for(handler.started.wait(), timeout=1)
        cancel_id, cancel_payload = inbound(2, phone=phone, text="cancelar consulta")
        assert store.admit_webhook_event(cancel_id, cancel_payload) == "cancel_requested"
        await asyncio.wait_for(task, timeout=1)
        snapshot = store.assistant_queue_snapshot()
        assert handler.was_cancelled is True
        assert snapshot["active"] == 0
        assert snapshot["waiting"] == 0
        with store._connect() as connection:
            status = connection.execute("SELECT status FROM assistant_jobs WHERE event_id=?", (event_id,)).fetchone()[0]
            messages = connection.execute("SELECT message_text FROM outbox ORDER BY created_at,outbox_id").fetchall()
        assert status == "cancelled"
        assert len(messages) == 1 and "detuve" in messages[0][0]
    asyncio.run(scenario())


def test_interrupted_query_becomes_uncertain_and_is_not_replayed(tmp_path):
    database_path = tmp_path / "h2.sqlite3"
    store = H2Store(database_path)
    event_id, payload = inbound(1)
    now = utc_now()
    assert store.admit_webhook_event(event_id, payload, received_at=now) == "queued"
    claimed = store.claim_assistant_job("worker", now=now)
    assert claimed
    # A new store object exercises the same durable boundary used after restart.
    restarted = H2Store(database_path)
    assert restarted.recover_assistant_jobs(now=now) == 1
    assert restarted.assistant_queue_snapshot() == {
        "active": 0, "waiting": 0, "cancel_requested": 0, "uncertain": 1
    }
    assert restarted.claim_assistant_job("restarted-worker", now=now) is None
    message = restarted.claim_outbox(now=now)
    assert "interrumpió" in message["message_text"]


def test_expired_waiting_query_is_reported_and_not_started(tmp_path):
    store = H2Store(tmp_path / "h2.sqlite3")
    received = utc_now()
    event_id, payload = inbound(1)
    assert store.admit_webhook_event(event_id, payload, received_at=received) == "queued"
    expired_at = iso_after(121)
    assert store.claim_assistant_job("late-worker", now=expired_at) is None
    assert store.assistant_queue_snapshot()["waiting"] == 0
    message = store.claim_outbox(now=expired_at)
    assert "a tiempo" in message["message_text"]


def test_duplicate_webhook_does_not_add_a_second_job(tmp_path):
    store = H2Store(tmp_path / "h2.sqlite3")
    event_id, payload = inbound(1)
    assert store.admit_webhook_event(event_id, payload) == "queued"
    assert store.admit_webhook_event(event_id, payload) == "duplicate"
    assert store.assistant_queue_snapshot()["waiting"] == 1


def test_delivery_receipts_are_processed_separately_and_monotonically(tmp_path):
    store = H2Store(tmp_path / "h2.sqlite3")
    recipient = "+521555011234"
    message_id = store.enqueue_message("receipt-test", recipient, "Aviso de prueba.")
    claimed = store.claim_outbox()
    store.finish_outbox(claimed["outbox_id"], "sent", provider_message_id="wamid.OUTBOUNDTEST")
    worker = WebhookReceiptWorker(store)
    first = "status:wamid.OUTBOUNDTEST:delivered:1791547201"
    second = "status:wamid.OUTBOUNDTEST:sent:1791547202"
    assert store.admit_webhook_event(first, {
        "kind": "status", "message_id": "wamid.OUTBOUNDTEST", "status": "delivered", "timestamp": "1791547201"
    }) == "queued"
    assert store.admit_webhook_event(second, {
        "kind": "status", "message_id": "wamid.OUTBOUNDTEST", "status": "sent", "timestamp": "1791547202"
    }) == "queued"
    assert asyncio.run(worker.process_one())
    assert asyncio.run(worker.process_one())
    assert not asyncio.run(worker.process_one())
    assert store.apply_delivery_status("wamid.OUTBOUNDTEST", "sent")
    with store._connect() as connection:
        status = connection.execute("SELECT delivery_status FROM outbox WHERE outbox_id=?", (message_id,)).fetchone()[0]
    assert status == "delivered"
