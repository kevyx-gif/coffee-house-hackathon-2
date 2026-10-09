import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from coffee_house.hackathon2.storage import (
    DuplicateConflict,
    H2StorageError,
    H2Store,
    InventoryShortage,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / "data" / "demo-hackathon2"
ADMIN = "+5215550000001"
CASHIER = "+5215550000002"
CUSTOMER = "+5215550000003"
NOW = "2026-10-09T12:00:00+00:00"


def make_store(tmp_path):
    store = H2Store(tmp_path / "h2.sqlite3")
    seeded = store.seed_demo_directory(FIXTURE_DIR)
    return store, seeded


def make_order(store, *, folio="H2-TEST-1", idem="same-request", customer=CUSTOMER):
    return store.create_order(
        folio=folio,
        idempotency_key=idem,
        customer_phone=customer,
        items=[{"product_id": "hot_latte", "variant": "mediano", "quantity": 1}],
        total_cents=7000,
        expires_at="2026-10-09T12:10:00+00:00",
        now=NOW,
    )


def assign_cashier(store, order_id):
    store.create_staff(ADMIN, "admin", now=NOW)
    store.create_staff(CASHIER, "cashier", invited_by=ADMIN, now=NOW)
    store.assign_order(order_id, CASHIER, 1, deadline="2026-10-09T12:05:00+00:00", now=NOW)


def accept(store, order_id, *, actor=CASHIER):
    store.accept_order(
        order_id,
        actor,
        [
            {"item_id": "grano_cafe", "quantity": 18},
            {"item_id": "leche_entera", "quantity": 220},
            {"item_id": "vaso_12oz", "quantity": 1},
        ],
        customer_message="El personal aceptó tu pedido H2-TEST-1.",
        now=NOW,
    )


def test_versioned_schema_and_demo_seed_does_not_reset_on_restart(tmp_path):
    store, seeded = make_store(tmp_path)
    assert seeded is True
    assert store.schema_version == 4
    assert store.inventory_snapshot()["grano_cafe"]["on_hand"] == 900
    with sqlite3.connect(tmp_path / "h2.sqlite3") as connection:
        connection.execute("UPDATE inventory_items SET on_hand=777 WHERE item_id='grano_cafe'")
        connection.commit()
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
    restarted = H2Store(tmp_path / "h2.sqlite3")
    assert restarted.seed_demo_directory(FIXTURE_DIR) is False
    assert restarted.inventory_snapshot()["grano_cafe"]["on_hand"] == 777


def test_unknown_existing_database_is_never_recreated(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE preserve_me(value TEXT)")
        connection.execute("INSERT INTO preserve_me VALUES('keep')")
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(H2StorageError):
        H2Store(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM preserve_me").fetchone()[0] == "keep"


def test_sqlite_lock_pauses_webhook_admission_without_partial_rows_and_recovers(tmp_path):
    database_path = tmp_path / "h2.sqlite3"
    store = H2Store(database_path, busy_timeout_ms=25)
    event_id = "message:wamid.DBOUTAGE001"
    payload = {
        "kind": "message",
        "message_id": "wamid.DBOUTAGE001",
        "sender": "+521555010008",
        "timestamp": "1791547200",
        "type": "text",
        "text": "Consulta sintética durante bloqueo de SQLite.",
    }

    with sqlite3.connect(database_path, isolation_level=None, timeout=0) as blocker:
        blocker.execute("BEGIN EXCLUSIVE")
        with pytest.raises(H2StorageError, match="No se confirmó la operación"):
            store.admit_webhook_event(event_id, payload)
        assert blocker.execute(
            "SELECT COUNT(*) FROM webhook_events WHERE event_id=?", (event_id,)
        ).fetchone()[0] == 0
        assert blocker.execute(
            "SELECT COUNT(*) FROM assistant_jobs WHERE event_id=?", (event_id,)
        ).fetchone()[0] == 0

    assert store.admit_webhook_event(event_id, payload) == "queued"
    assert store.assistant_queue_snapshot()["waiting"] == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM webhook_events WHERE event_id=?", (event_id,)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM assistant_jobs WHERE event_id=?", (event_id,)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM outbox WHERE dedupe_key LIKE ?", (f"assistant:{event_id}:%",)
        ).fetchone()[0] == 0


def test_h2_migration_never_opens_or_changes_h1_file(tmp_path):
    h1_path = tmp_path / "h1.sqlite3"
    with sqlite3.connect(h1_path) as connection:
        connection.execute("CREATE TABLE h1_sentinel(value TEXT)")
        connection.execute("INSERT INTO h1_sentinel VALUES('unchanged')")
    before = h1_path.read_bytes()
    store = H2Store(tmp_path / "h2.sqlite3")
    assert store.schema_version == 4
    assert h1_path.read_bytes() == before
    with sqlite3.connect(h1_path) as connection:
        assert connection.execute("SELECT value FROM h1_sentinel").fetchone()[0] == "unchanged"


def test_v1_to_v2_migration_preserves_orders_and_adds_routing_history(tmp_path):
    store, _ = make_store(tmp_path)
    order_id = make_order(store, folio="H2-MIGRATE", idem="migration")
    assign_cashier(store, order_id)
    stock_before = store.inventory_snapshot()
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE order_recipients")
        connection.execute("DROP TABLE order_rounds")
        connection.execute("DROP INDEX idx_assistant_jobs_pending_phone")
        connection.execute("DROP INDEX idx_assistant_jobs_queue")
        connection.execute("DROP TABLE assistant_jobs")
        connection.execute("DROP TABLE catalog_price_overrides")
        connection.execute("ALTER TABLE orders DROP COLUMN accepted_by_phone")
        connection.execute("PRAGMA user_version=1")
    migrated = H2Store(store.path)
    assert migrated.schema_version == 4
    assert migrated.inventory_snapshot() == stock_before
    assert migrated.order_for_customer("H2-MIGRATE", CUSTOMER)["status"] == "pending_staff"
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT round_no,phase,status FROM order_rounds WHERE order_id=?", (order_id,)).fetchone() == (1, "cashier", "active")
        assert connection.execute("SELECT source,status FROM tickets WHERE ticket_id=?", (f"wa-{order_id}",)).fetchone() == ("whatsapp_order", "needs_review")
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='assistant_jobs'").fetchone()


def test_v3_to_v4_adds_price_overrides_without_resetting_inventory(tmp_path):
    store, _ = make_store(tmp_path)
    stock_before = store.inventory_snapshot()
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE catalog_price_overrides")
        connection.execute("PRAGMA user_version=3")
    migrated = H2Store(store.path)
    assert migrated.schema_version == 4
    assert migrated.inventory_snapshot() == stock_before
    assert migrated.catalog_price_overrides() == {}


def test_order_idempotency_and_acceptance_are_atomic(tmp_path):
    store, _ = make_store(tmp_path)
    order_id = make_order(store)
    assert make_order(store) == order_id
    with pytest.raises(DuplicateConflict):
        store.create_order(
            folio="H2-TEST-2", idempotency_key="same-request", customer_phone=CUSTOMER,
            items=[{"product_id": "hot_latte", "variant": "mediano", "quantity": 2}],
            total_cents=14000, expires_at="2026-10-09T12:10:00+00:00", now=NOW,
        )
    assign_cashier(store, order_id)
    before = store.inventory_snapshot()
    accept(store, order_id)
    after = store.inventory_snapshot()
    assert after["grano_cafe"]["on_hand"] == before["grano_cafe"]["on_hand"] - 18
    assert after["leche_entera"]["on_hand"] == before["leche_entera"]["on_hand"] - 220
    assert after["vaso_12oz"]["on_hand"] == before["vaso_12oz"]["on_hand"] - 1
    assert store.order_for_customer("H2-TEST-1", CUSTOMER)["status"] == "accepted"
    assert store.claim_outbox(now=NOW)["recipient_phone"] == CUSTOMER
    with pytest.raises(H2StorageError):
        accept(store, order_id)
    assert store.inventory_snapshot() == after
    with sqlite3.connect(tmp_path / "h2.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM inventory_movements").fetchone()[0] == 3


def test_shortage_and_wrong_staff_leave_order_and_stock_unchanged(tmp_path):
    store, _ = make_store(tmp_path)
    order_id = make_order(store, folio="H2-TEST-3", idem="shortage")
    assign_cashier(store, order_id)
    outsider = "+5215550000010"
    store.create_staff(outsider, "cashier", invited_by=ADMIN, now=NOW)
    with pytest.raises(H2StorageError):
        accept(store, order_id, actor=outsider)
    with sqlite3.connect(tmp_path / "h2.sqlite3") as connection:
        connection.execute("UPDATE inventory_items SET on_hand=17 WHERE item_id='grano_cafe'")
    short_before = store.inventory_snapshot()
    with pytest.raises(InventoryShortage):
        store.accept_order(
            order_id, CASHIER,
            customer_message="No se confirmó el pedido.", now=NOW,
        )
    assert store.inventory_snapshot() == short_before
    assert store.order_for_customer("H2-TEST-3", CUSTOMER)["status"] == "pending_staff"
    with sqlite3.connect(tmp_path / "h2.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM inventory_movements").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_webhook_deduplication_and_outbox_uncertain_state(tmp_path):
    store, _ = make_store(tmp_path)
    payload = {"from": CUSTOMER, "text": "¿Hay latte?"}
    assert store.enqueue_webhook("meta-event-1", payload, received_at=NOW)
    assert not store.enqueue_webhook("meta-event-1", payload, received_at=NOW)
    assert not store.enqueue_webhook("meta-event-1", {"from": CUSTOMER, "text": "different"}, received_at=NOW)
    store.finish_webhook_event("meta-event-1", "done", now=NOW)
    message_id = store.enqueue_message("demo:dedupe-1", CUSTOMER, "Aviso sintético.", now=NOW)
    assert store.claim_outbox(now=NOW)["outbox_id"] == message_id
    store.finish_outbox(message_id, "uncertain", now=NOW, error_code="network_timeout")
    assert store.claim_outbox(now=NOW) is None
    with pytest.raises(H2StorageError):
        store.finish_outbox(message_id, "sent", now=NOW)
    interrupted_id = store.enqueue_message("demo:interrupted", CUSTOMER, "Aviso de prueba.", now=NOW)
    assert store.claim_outbox(now=NOW)["outbox_id"] == interrupted_id
    assert store.recover_uncertain_outbox(now=NOW) == 1
    assert store.claim_outbox(now=NOW) is None


def test_queued_outbox_messages_expire_and_legacy_rows_are_redacted(tmp_path):
    store, _ = make_store(tmp_path)
    message_id = store.enqueue_message("demo:outbox-expiry", CUSTOMER, "Aviso sintético.", now=NOW)
    legacy_id = store.enqueue_message("demo:outbox-legacy", CUSTOMER, "Aviso antiguo.", now=NOW)
    with sqlite3.connect(tmp_path / "h2.sqlite3") as connection:
        connection.execute("UPDATE outbox SET expires_at=NULL,retention_until=NULL WHERE outbox_id=?", (legacy_id,))
        expires_at = connection.execute(
            "SELECT expires_at FROM outbox WHERE outbox_id=?", (message_id,),
        ).fetchone()[0]
    assert expires_at == (datetime.fromisoformat(NOW) + timedelta(hours=24)).isoformat(timespec="milliseconds")
    later = (datetime.fromisoformat(NOW) + timedelta(hours=25)).isoformat()
    assert store.claim_outbox(now=later) is None
    removed = store.purge_expired(now=later)
    assert removed["outbox_expired"] == 1
    assert removed["outbox"] == 2
    with sqlite3.connect(tmp_path / "h2.sqlite3") as connection:
        rows = connection.execute(
            "SELECT status,recipient_phone,message_text,error_code FROM outbox ORDER BY dedupe_key",
        ).fetchall()
    assert rows == [("failed", "", "", "expired"), ("failed", "", "", "expired")]


def test_auth_hash_session_conversation_and_24_hour_redaction(tmp_path):
    store, _ = make_store(tmp_path)
    order_id = make_order(store, folio="H2-TEST-RETENTION", idem="retention")
    assign_cashier(store, order_id)
    accept(store, order_id)
    for state, message in (("preparing", "Tu pedido está en preparación."),
                           ("ready", "Tu pedido está listo para recoger."),
                           ("delivered", "Tu pedido fue entregado.")):
        store.transition_order(order_id, CASHIER, state, message=message, now=NOW)
    code_hash = "a" * 64
    store.store_auth_code(CASHIER, code_hash, "2026-10-09T12:05:00+00:00", now=NOW)
    assert store.verify_auth_code_and_create_session(
        CASHIER, code_hash, "b" * 64, now=NOW, expires_at="2026-10-09T12:30:00+00:00"
    )
    assert store.staff_session(CASHIER, now=NOW)["role"] == "cashier"
    store.save_conversation(CUSTOMER, {"draft": "latte mediano"}, expires_at="2026-10-10T12:00:00+00:00", now=NOW)
    store.enqueue_webhook("meta-event-retention", {"from": CUSTOMER, "body": "dato sintético"}, received_at=NOW)
    store.finish_webhook_event("meta-event-retention", "done", now=NOW)
    for message in ["H2-TEST-RETENTION:accepted", "H2-TEST-RETENTION:status:preparing",
                    "H2-TEST-RETENTION:status:ready", "H2-TEST-RETENTION:status:delivered"]:
        claimed = store.claim_outbox(now=NOW)
        store.finish_outbox(claimed["outbox_id"], "sent", now=NOW)
    later = (datetime.fromisoformat(NOW) + timedelta(hours=25)).isoformat()
    removed = store.purge_expired(now=later)
    assert removed["conversations"] == 1
    assert removed["auth_codes"] == 1
    assert removed["sessions"] == 1
    assert removed["events"] == 1
    assert removed["orders"] == 1
    assert removed["outbox"] == 4
    assert store.conversation(CUSTOMER, now=later) is None
    assert store.order_for_customer("H2-TEST-RETENTION", CUSTOMER) is None
    with sqlite3.connect(tmp_path / "h2.sqlite3") as connection:
        assert connection.execute("SELECT normalized_payload_json FROM webhook_events WHERE event_id='meta-event-retention'").fetchone()[0] == "{}"
        assert connection.execute("SELECT recipient_phone,message_text FROM outbox WHERE recipient_phone=''").fetchall()[0] == ("", "")
        assert connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] >= 5
