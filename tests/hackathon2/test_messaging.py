import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from coffee_house.hackathon2.auth import PersonnelAuth
from coffee_house.hackathon2.catalog import CatalogService
from coffee_house.hackathon2.messaging import WhatsAppMessageHandler
from coffee_house.hackathon2.operations import OperationsService
from coffee_house.hackathon2.orders import OrderWorkflow
from coffee_house.hackathon2.storage import H2Store, utc_now
from coffee_house.hackathon2.tickets import TicketReviewService

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "data" / "demo-hackathon2"
CATALOG = ROOT / "data" / "catalog.json"
ADMIN = "+5215550000001"
CASHIER = "+5215550000002"
MANAGER = "+5215550000004"
CUSTOMER = "+5215550000003"


class NeverCallConversation:
    def __init__(self):
        self.calls = []

    async def handle(self, phone, text):
        self.calls.append((phone, text))
        return SimpleNamespace(text="respuesta de prueba", state="answered", intent="test")


def make_staff_workflow(tmp_path):
    store = H2Store(tmp_path / "h2.sqlite3")
    store.seed_demo_directory(FIXTURES)
    now = utc_now()
    expires = (datetime.fromisoformat(now) + timedelta(minutes=30)).isoformat(timespec="milliseconds")
    store.create_staff(ADMIN, "admin", now=now)
    store.create_staff(CASHIER, "cashier", invited_by=ADMIN, now=now)
    store.create_staff(MANAGER, "manager", invited_by=ADMIN, now=now)
    auth = PersonnelAuth(store, otp_pepper="p" * 40)
    for index, phone in enumerate((ADMIN, CASHIER, MANAGER), 1):
        code_hash = f"{index:064x}"
        store.store_auth_code(phone, code_hash, expires, now=now)
        assert store.verify_auth_code_and_create_session(
            phone, code_hash, f"{index + 10:064x}", now=now, expires_at=expires
        )
    catalog = CatalogService(CATALOG, store)
    orders = OrderWorkflow(store, catalog, auth)
    return store, auth, catalog, orders


def make_order(orders):
    preview = orders.prepare_confirmation(CUSTOMER, [{
        "product_id": "hot_latte", "variant": "mediano", "quantity": 1, "modifier_ids": []
    }])
    assert preview.state == "confirmation_required"
    submitted = orders.respond_to_confirmation(CUSTOMER, "sí")
    assert submitted.state == "pending_staff"
    return submitted.folio


def test_staff_status_commands_are_authorized_and_emit_order_notifications(tmp_path):
    store, auth, catalog, orders = make_staff_workflow(tmp_path)
    folio = make_order(orders)
    tickets = TicketReviewService(store, auth, catalog, engine=object())
    conversation = NeverCallConversation()
    handler = WhatsAppMessageHandler(conversation, auth, orders, tickets=tickets)

    accepted = asyncio.run(handler.handle(CASHIER, f"ACEPTAR {folio}"))
    assert accepted.state == "completed"
    assert store.order_for_customer(folio, CUSTOMER)["status"] == "accepted"
    assert asyncio.run(handler.handle(CASHIER, f"PREPARAR {folio}")).state == "completed"
    assert store.order_for_customer(folio, CUSTOMER)["status"] == "preparing"
    assert asyncio.run(handler.handle(CASHIER, f"LISTO {folio}")).state == "completed"
    assert store.order_for_customer(folio, CUSTOMER)["status"] == "ready"
    assert asyncio.run(handler.handle(CASHIER, f"ENTREGAR {folio}")).state == "completed"
    assert store.order_for_customer(folio, CUSTOMER)["status"] == "delivered"
    assert len(conversation.calls) == 0
    with store._connect() as connection:
        queued = connection.execute("SELECT COUNT(*) FROM outbox WHERE status='queued'").fetchone()[0]
        actions = connection.execute("SELECT COUNT(*) FROM audit_log WHERE result='ok'").fetchone()[0]
    assert queued >= 5
    assert actions >= 4


def test_unauthenticated_staff_commands_are_not_sent_to_the_model(tmp_path):
    _store, auth, _catalog, orders = make_staff_workflow(tmp_path)
    conversation = NeverCallConversation()
    handler = WhatsAppMessageHandler(conversation, auth, orders)
    result = asyncio.run(handler.handle("+5215550099999", "ACEPTAR CH-ABC123"))
    assert result.state == "needs_auth"
    assert not conversation.calls


def test_staff_corrects_ocr_lines_before_inventory_is_changed(tmp_path):
    store, auth, catalog, orders = make_staff_workflow(tmp_path)
    tickets = TicketReviewService(store, auth, catalog, engine=object())
    now = utc_now()
    expiry = (datetime.fromisoformat(now) + timedelta(hours=24)).isoformat(timespec="milliseconds")
    ticket_id = "POS-ABCDEF0123456789"
    store.create_pos_ticket(
        ticket_id, CASHIER,
        [{"status": "unresolved", "confidence": 0.2, "source_text": "latte?"}],
        "a" * 64, expires_at=expiry, now=now,
    )
    handler = WhatsAppMessageHandler(NeverCallConversation(), auth, orders, tickets=tickets)
    corrected = asyncio.run(handler.handle(
        CASHIER, f"CORREGIR {ticket_id}: 2x latte caliente mediano"
    ))
    assert corrected.state == "completed"
    assert "2 × Latte" in corrected.text
    before = store.inventory_snapshot()
    committed = asyncio.run(handler.handle(CASHIER, f"CONFIRMAR {ticket_id}"))
    assert committed.state == "completed"
    after = store.inventory_snapshot()
    assert after["grano_cafe"]["on_hand"] == before["grano_cafe"]["on_hand"] - 36


def test_manager_inventory_adjustment_is_audited_and_webhook_idempotent(tmp_path):
    store, auth, catalog, orders = make_staff_workflow(tmp_path)
    operations = OperationsService(store, auth, catalog)
    handler = WhatsAppMessageHandler(NeverCallConversation(), auth, orders, operations=operations)
    command = "AJUSTAR_EXISTENCIA grano_cafe -10 conteo de demostración"
    event_id = "message:wamid.INVENTORY-ADJUST-1"
    first = asyncio.run(handler.handle(MANAGER, command, event_id=event_id))
    again = asyncio.run(handler.handle(MANAGER, command, event_id=event_id))
    assert first.state == "completed" and again.state == "completed"
    assert store.inventory_snapshot()["grano_cafe"]["on_hand"] == 890
    adjustments = store.inventory_movements(source_type="adjustment")
    assert len(adjustments) == 1 and adjustments[0]["quantity_delta"] == -10
    inventory = asyncio.run(handler.handle(MANAGER, "INVENTARIO"))
    assert inventory.state == "completed"
    assert "no son inventario real" in inventory.text


def test_admin_price_change_persists_and_manager_cannot_change_it(tmp_path):
    store, auth, catalog, orders = make_staff_workflow(tmp_path)
    operations = OperationsService(store, auth, catalog)
    handler = WhatsAppMessageHandler(NeverCallConversation(), auth, orders, operations=operations)
    denied = asyncio.run(handler.handle(MANAGER, "PRECIO hot_latte mediano 72.50"))
    assert denied.state == "denied"
    assert catalog.product_by_id("hot_latte")["variants"][0]["price_cents"] == 7000

    changed = asyncio.run(handler.handle(ADMIN, "PRECIO hot_latte mediano 72.50"))
    assert changed.state == "completed" and "provisional" in changed.text
    product = catalog.product_by_id("hot_latte")
    assert product["variants"][0]["price_cents"] == 7250
    assert product["variants"][0]["price_status"] == "provisional"
    preview = orders.prepare_confirmation(CUSTOMER, [{
        "product_id": "hot_latte", "variant": "mediano", "quantity": 1, "modifier_ids": [],
    }])
    assert "$72.50" in preview.text
    restarted = CatalogService(CATALOG, H2Store(store.path))
    assert restarted.product_by_id("hot_latte")["variants"][0]["price_cents"] == 7250

    denied_audit = asyncio.run(handler.handle(MANAGER, "AUDITORIA"))
    assert denied_audit.state == "denied"
    audit = asyncio.run(handler.handle(ADMIN, "AUDITORIA"))
    assert audit.state == "completed" and "catalog_price_changed" in audit.text
    assert ADMIN not in audit.text


def test_only_admin_can_change_personnel_role_and_old_session_is_revoked(tmp_path):
    store, auth, _catalog, orders = make_staff_workflow(tmp_path)
    handler = WhatsAppMessageHandler(NeverCallConversation(), auth, orders)
    denied = asyncio.run(handler.handle(MANAGER, f"/ROL {CASHIER} gerente"))
    assert denied.state == "denied"
    changed = asyncio.run(handler.handle(ADMIN, f"/ROL {CASHIER} gerente"))
    assert changed.state == "completed"
    assert auth.principal(CASHIER) is None
    assert store.staff_session(CASHIER) is None
