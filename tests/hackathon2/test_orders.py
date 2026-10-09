from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from coffee_house.hackathon2.auth import AuthorizationError, PersonnelAuth
from coffee_house.hackathon2.catalog import CatalogService
from coffee_house.hackathon2.orders import OrderWorkflow
from coffee_house.hackathon2.storage import H2StorageError, H2Store

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "data" / "demo-hackathon2"
CATALOG = ROOT / "data" / "catalog.json"
ADMIN = "+5215550000001"
CUSTOMERS = ["+5215550000101", "+5215550000102"]
CASHIERS = ["+5215550000002", "+5215550000003", "+5215550000004", "+5215550000005"]
NOW = "2026-10-09T12:00:00.000+00:00"


def make_workflow(tmp_path, *, cashier_count=3):
    store = H2Store(tmp_path / "h2.sqlite3")
    store.seed_demo_directory(FIXTURES)
    store.create_staff(ADMIN, "admin", now=NOW)
    staff = [ADMIN]
    for phone in CASHIERS[:cashier_count]:
        store.create_staff(phone, "cashier", invited_by=ADMIN, now=NOW)
        staff.append(phone)
    auth = PersonnelAuth(store, otp_pepper="p" * 40)
    for index, phone in enumerate(staff):
        store.store_auth_code(phone, "a" * 64, "2026-10-09T12:30:00+00:00", now=NOW)
        assert store.verify_auth_code_and_create_session(
            phone, "a" * 64, f"{index + 1:064x}", now=NOW, expires_at="2026-10-09T12:30:00+00:00"
        )
    catalog = CatalogService(CATALOG, store)
    return store, OrderWorkflow(store, catalog, auth)


def latte(*, quantity=1, extras=None, variant="mediano"):
    return [{"product_id": "hot_latte", "variant": variant, "quantity": quantity,
             "modifier_ids": extras or []}]


def confirm(workflow, phone=CUSTOMERS[0], *, now=NOW):
    preview = workflow.prepare_confirmation(phone, latte(extras=["leche_avena"]), now=now)
    assert preview.state == "confirmation_required"
    assert "Confirmo tu pedido:" in preview.text
    assert "sustitución por Leche de Avena" in preview.text
    assert "$85.00 MXN" in preview.text
    return workflow.respond_to_confirmation(phone, "Sí, confirmo", now=now)


def test_explicit_confirmation_is_required_and_changes_rebuild_summary(tmp_path):
    store, workflow = make_workflow(tmp_path)
    first = workflow.prepare_confirmation(CUSTOMERS[0], latte(), now=NOW)
    assert "Total: $70.00 MXN" in first.text
    assert workflow.respond_to_confirmation(CUSTOMERS[0], "sí, pero cambia a grande", now=NOW).state == "edit_requested"
    assert workflow.respond_to_confirmation(CUSTOMERS[0], "quizá", now=NOW).state == "needs_clarification"
    assert workflow.respond_to_confirmation(CUSTOMERS[0], "no, gracias", now=NOW).state == "not_submitted"
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM tickets WHERE source='whatsapp_order'").fetchone()[0] == 0
    updated = workflow.update_confirmation(CUSTOMERS[0], latte(quantity=2), now=NOW)
    assert "2 × Latte" in updated.text
    assert "$140.00 MXN" in updated.text
    assert workflow.respond_to_confirmation(CUSTOMERS[0], "sí, confirmo", now=NOW).state == "pending_staff"


def test_confirmed_order_creates_folio_ticket_and_only_notifies_assignee(tmp_path):
    store, workflow = make_workflow(tmp_path)
    before = store.inventory_snapshot()
    result = confirm(workflow)
    assert result.state == "pending_staff" and result.folio.startswith("CH-")
    assert "Tu folio es" in result.text
    assert store.inventory_snapshot() == before
    order = store.order_for_staff_workflow(result.folio)
    assert order["status"] == "pending_staff" and order["total_cents"] == 8500
    with sqlite3.connect(store.path) as connection:
        ticket = connection.execute("SELECT source,status,lines_json FROM tickets WHERE ticket_id=?", (f"wa-{order['order_id']}",)).fetchone()
        assigned = connection.execute("SELECT staff_phone FROM order_recipients WHERE order_id=? AND round_no=1", (order["order_id"],)).fetchall()
        assignment_deadline = connection.execute(
            "SELECT deadline FROM order_rounds WHERE order_id=? AND round_no=1", (order["order_id"],),
        ).fetchone()[0]
        notice_expiry = connection.execute(
            "SELECT expires_at FROM outbox WHERE dedupe_key LIKE ?", (f"order:{result.folio}:assigned:%",),
        ).fetchone()[0]
        assert ticket[0:2] == ("whatsapp_order", "needs_review")
        assert '"product_name":"Latte"' in ticket[2]
        assert len(assigned) == 1 and assigned[0][0] in CASHIERS
        assert notice_expiry == assignment_deadline
        assert connection.execute("SELECT COUNT(*) FROM inventory_movements").fetchone()[0] == 0
    staff_notice = store.claim_outbox(now=NOW)
    assert staff_notice["recipient_phone"] == assigned[0][0]
    assert result.folio in staff_notice["message_text"]
    assert "sustitución por Leche de Avena" in staff_notice["message_text"]


def test_rechecks_current_stock_and_does_not_create_order_if_unavailable(tmp_path):
    store, workflow = make_workflow(tmp_path)
    workflow.prepare_confirmation(CUSTOMERS[0], latte(quantity=2), now=NOW)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE inventory_items SET on_hand=18 WHERE item_id='grano_cafe'")
    response = workflow.respond_to_confirmation(CUSTOMERS[0], "sí", now=NOW)
    assert response.state == "reconfirmation_required"
    assert "No envié el pedido ni desconté existencias" in response.text
    assert "¿Te gustaría alguna?" in response.text
    conversation = store.conversation(CUSTOMERS[0])
    assert conversation is not None
    assert "pending_order_confirmation" not in conversation
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM inventory_movements").fetchone()[0] == 0


def test_reconfirmation_suggests_only_currently_available_options(tmp_path):
    store, workflow = make_workflow(tmp_path)
    workflow.prepare_confirmation(CUSTOMERS[0], latte(quantity=2), now=NOW)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE inventory_items SET on_hand=18 WHERE item_id='grano_cafe'")
    response = workflow.respond_to_confirmation(CUSTOMERS[0], "sí", now=NOW)
    assert response.state == "reconfirmation_required"
    assert "Acabo de revisar estas opciones:" in response.text
    assert "$" in response.text
    alternatives = workflow.catalog.available_alternatives("hot_latte", "mediano", [], quantity=2)
    assert alternatives
    assert all(item["available_portions"] >= 2 for item in alternatives)
    assert all(item["product_id"] != "hot_latte" for item in alternatives)


def test_fifo_load_balancing_and_sequential_escalation(tmp_path):
    store, workflow = make_workflow(tmp_path)
    first = confirm(workflow).folio
    order1 = store.order_for_staff_workflow(first)
    first_staff = _round_phones(store, order1["order_id"], 1)[0]
    second_customer = CUSTOMERS[1]
    second = confirm(workflow, second_customer).folio
    order2 = store.order_for_staff_workflow(second)
    second_staff = _round_phones(store, order2["order_id"], 1)[0]
    assert second_staff != first_staff
    pass1 = workflow.pass_to_next(first_staff, first, now=NOW)
    assert pass1["phase"] == "cashier"
    second_staff_first_order = _round_phones(store, order1["order_id"], 2)[0]
    assert second_staff_first_order != first_staff
    # Pass the second distinct cashier; the third cashier and admin receive the final round together.
    workflow.pass_to_next(second_staff_first_order, first, now=NOW)
    final = _round_info(store, order1["order_id"])
    assert final["phase"] == "escalation"
    assert set(_round_phones(store, order1["order_id"], final["round_no"])) == {ADMIN, next(p for p in CASHIERS if p not in {first_staff, second_staff_first_order})}


def test_only_current_assignee_can_accept_and_sale_is_atomic(tmp_path):
    store, workflow = make_workflow(tmp_path)
    before = store.inventory_snapshot()
    folio = confirm(workflow).folio
    order = store.order_for_staff_workflow(folio)
    assigned = _round_phones(store, order["order_id"], 1)[0]
    outsider = next(phone for phone in CASHIERS if phone != assigned)
    with pytest.raises(AuthorizationError):
        workflow.accept(outsider, folio, now=NOW)
    workflow.accept(assigned, folio, now=NOW)
    after = store.inventory_snapshot()
    assert after["grano_cafe"]["on_hand"] == before["grano_cafe"]["on_hand"] - 18
    assert after["leche_entera"]["on_hand"] == before["leche_entera"]["on_hand"]
    assert after["leche_avena"]["on_hand"] == before["leche_avena"]["on_hand"] - 220
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM inventory_movements WHERE source_id=?", (order["order_id"],)).fetchone()[0] == 3
    assert store.customer_order_status(CUSTOMERS[0], folio)["status"] == "aceptado"
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT status FROM tickets WHERE ticket_id=?", (f"wa-{order['order_id']}",)).fetchone()[0] == "confirmed"
        accepted = connection.execute("SELECT recipient_phone FROM outbox WHERE dedupe_key LIKE ?", (f"order:{folio}:accepted:%",)).fetchall()
        expected_staff = {row[0] for row in connection.execute("SELECT DISTINCT staff_phone FROM order_recipients WHERE order_id=?", (order["order_id"],))}
        assert {row[0] for row in accepted} == expected_staff | {CUSTOMERS[0]}
    with pytest.raises(H2StorageError):
        workflow.accept(assigned, folio, now=NOW)
    assert store.inventory_snapshot() == after


def test_oat_milk_substitution_uses_the_selected_size_recipe_quantity(tmp_path):
    store, workflow = make_workflow(tmp_path)
    before = store.inventory_snapshot()
    preview = workflow.prepare_confirmation(CUSTOMERS[0], latte(extras=["leche_avena"], variant="grande"), now=NOW)
    assert "sustitución por Leche de Avena" in preview.text
    assert "$95.00 MXN" in preview.text
    result = workflow.respond_to_confirmation(CUSTOMERS[0], "sí, confirmo", now=NOW)
    order = store.order_for_staff_workflow(result.folio)
    assigned = _round_phones(store, order["order_id"], 1)[0]

    workflow.accept(assigned, result.folio, now=NOW)

    after = store.inventory_snapshot()
    assert after["leche_entera"]["on_hand"] == before["leche_entera"]["on_hand"]
    assert after["leche_avena"]["on_hand"] == before["leche_avena"]["on_hand"] - 300
    with sqlite3.connect(store.path) as connection:
        movements = connection.execute(
            "SELECT item_id,quantity_delta FROM inventory_movements WHERE source_id=? ORDER BY item_id",
            (order["order_id"],),
        ).fetchall()
    assert movements == [("grano_cafe", -20), ("leche_avena", -300), ("vaso_16oz", -1)]


def test_two_simultaneous_recipients_only_one_acceptance_wins(tmp_path):
    store, workflow = make_workflow(tmp_path)
    folio = confirm(workflow).folio
    order = store.order_for_staff_workflow(folio)
    first = _round_phones(store, order["order_id"], 1)[0]
    workflow.pass_to_next(first, folio, now=NOW)
    second_assignee = _round_phones(store, order["order_id"], 2)[0]
    workflow.pass_to_next(second_assignee, folio, now=NOW)
    final_round = _round_info(store, order["order_id"])["round_no"]
    recipients = _round_phones(store, order["order_id"], final_round)
    assert ADMIN in recipients and len(recipients) == 2
    before = store.inventory_snapshot()
    def accept_actor(actor):
        try:
            workflow.accept(actor, folio, now=NOW)
            return "accepted"
        except (H2StorageError, AuthorizationError):
            return "blocked"
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(accept_actor, recipients))
    assert outcomes.count("accepted") == 1 and outcomes.count("blocked") == 1
    after = store.inventory_snapshot()
    assert after["grano_cafe"]["on_hand"] == before["grano_cafe"]["on_hand"] - 18
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM inventory_movements").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM outbox WHERE dedupe_key LIKE ?", (f"order:{folio}:accepted:%",)).fetchone()[0] == len(set(recipients + [first, second_assignee])) + 1


def test_timeouts_route_and_final_expiry_never_discount(tmp_path):
    store, workflow = make_workflow(tmp_path)
    folio = confirm(workflow).folio
    order = store.order_for_staff_workflow(folio)
    first_deadline = _round_info(store, order["order_id"])["deadline"]
    assert workflow.expire_or_route_due_assignments(now=first_deadline) == 1
    second_round = _round_info(store, order["order_id"])
    assert second_round["phase"] == "cashier" and second_round["round_no"] == 2
    next_notice = store.claim_outbox(now=first_deadline)
    assert next_notice["recipient_phone"] == _round_phones(store, order["order_id"], 2)[0]
    assert workflow.expire_or_route_due_assignments(now=second_round["deadline"]) == 1
    final_round = _round_info(store, order["order_id"])
    assert final_round["phase"] == "escalation"
    assert workflow.expire_or_route_due_assignments(now=final_round["deadline"]) == 1
    assert store.customer_order_status(CUSTOMERS[0], folio)["status"] == "sin confirmar"
    assert store.inventory_snapshot()["grano_cafe"]["on_hand"] == 900
    customer_messages = []
    while (message := store.claim_outbox(now=final_round["deadline"])) is not None:
        if message["recipient_phone"] == CUSTOMERS[0]:
            customer_messages.append(message["message_text"])
    assert len(customer_messages) == 1 and "no quedó confirmado" in customer_messages[0]


def test_customer_status_is_bound_to_source_phone_and_falls_back_to_latest(tmp_path):
    _, workflow = make_workflow(tmp_path)
    first = confirm(workflow, CUSTOMERS[0]).folio
    second = confirm(workflow, CUSTOMERS[1]).folio
    own = workflow.status(CUSTOMERS[0], second)
    assert own.folio == first
    assert "pendiente de confirmación" in own.text and second not in own.text
    assert workflow.status("+5215550000199").state == "not_found"


def test_admin_rejection_requires_final_escalation_and_never_deducts(tmp_path):
    store, workflow = make_workflow(tmp_path)
    folio = confirm(workflow).folio
    order = store.order_for_staff_workflow(folio)
    first = _round_phones(store, order["order_id"], 1)[0]
    with pytest.raises(H2StorageError):
        workflow.reject(ADMIN, folio, now=NOW)
    workflow.pass_to_next(first, folio, now=NOW)
    second = _round_phones(store, order["order_id"], 2)[0]
    workflow.pass_to_next(second, folio, now=NOW)
    before = store.inventory_snapshot()
    workflow.reject(ADMIN, folio, now=NOW)
    assert store.inventory_snapshot() == before
    assert store.customer_order_status(CUSTOMERS[0], folio)["status"] == "no aceptado"
    message = store.claim_outbox(now=NOW)
    while message and message["recipient_phone"] != CUSTOMERS[0]:
        message = store.claim_outbox(now=NOW)
    assert "Lo sentimos" in message["message_text"] and "otra opción" in message["message_text"]


def test_admin_can_pass_to_an_untried_cashier_with_same_deadline(tmp_path):
    store, workflow = make_workflow(tmp_path, cashier_count=4)
    folio = confirm(workflow).folio
    order = store.order_for_staff_workflow(folio)
    first = _round_phones(store, order["order_id"], 1)[0]
    workflow.pass_to_next(first, folio, now=NOW)
    second = _round_phones(store, order["order_id"], 2)[0]
    workflow.pass_to_next(second, folio, now=NOW)
    final = _round_info(store, order["order_id"])
    deadline_before = final["deadline"]
    workflow.pass_to_next(ADMIN, folio, now=NOW)
    assert _round_info(store, order["order_id"])["deadline"] == deadline_before
    recipients = _round_phones(store, order["order_id"], final["round_no"])
    assert ADMIN in recipients and len(recipients) == 3


@pytest.mark.parametrize("cashier_count,first_minutes", [(0, 10), (1, 10), (2, 5)])
def test_assignment_timeouts_follow_staff_count_and_end_with_admin(tmp_path, cashier_count, first_minutes):
    store, workflow = make_workflow(tmp_path, cashier_count=cashier_count)
    folio = confirm(workflow).folio
    order = store.order_for_staff_workflow(folio)
    first = _round_info(store, order["order_id"])
    if cashier_count == 0:
        assert first["phase"] == "escalation"
        assert first["deadline"] == "2026-10-09T12:10:00.000+00:00"
    else:
        assert first["phase"] == "cashier"
        assert first["deadline"] == f"2026-10-09T12:{first_minutes:02d}:00.000+00:00"
        assert workflow.expire_or_route_due_assignments(now=first["deadline"]) == 1
        first = _round_info(store, order["order_id"])
        if cashier_count == 2:
            assert first["phase"] == "cashier" and first["round_no"] == 2
            assert workflow.expire_or_route_due_assignments(now=first["deadline"]) == 1
            first = _round_info(store, order["order_id"])
        assert first["phase"] == "escalation" and ADMIN in _round_phones(store, order["order_id"], first["round_no"])
    assert workflow.expire_or_route_due_assignments(now=first["deadline"]) == 1
    assert store.customer_order_status(CUSTOMERS[0], folio)["status"] == "sin confirmar"
    assert store.inventory_snapshot()["grano_cafe"]["on_hand"] == 900


def test_status_transitions_are_sequential_and_only_accepting_staff_can_advance(tmp_path):
    store, workflow = make_workflow(tmp_path)
    folio = confirm(workflow).folio
    order = store.order_for_staff_workflow(folio)
    assigned = _round_phones(store, order["order_id"], 1)[0]
    with pytest.raises(H2StorageError):
        workflow.advance(assigned, folio, "ready", now=NOW)
    workflow.accept(assigned, folio, now=NOW)
    other = next(phone for phone in CASHIERS if phone != assigned)
    with pytest.raises(AuthorizationError):
        workflow.advance(other, folio, "preparing", now=NOW)
    workflow.advance(assigned, folio, "preparing", now=NOW)
    workflow.advance(assigned, folio, "ready", now=NOW)
    workflow.advance(assigned, folio, "delivered", now=NOW)
    assert [workflow.status(CUSTOMERS[0], folio).text for _ in range(1)] == [f"Pedido {folio}: entregado."]
    with pytest.raises(AuthorizationError):
        workflow.advance(assigned, folio, "preparing", now=NOW)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM outbox WHERE dedupe_key LIKE ?", (f"order:{folio}:status:%:customer",)).fetchone()[0] == 3
        actions = {row[0] for row in connection.execute("SELECT action FROM audit_log WHERE target=?", (folio,))}
        assert {"order_accepted", "order_preparing", "order_ready", "order_delivered"} <= actions


def _round_info(store, order_id):
    with sqlite3.connect(store.path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT round_no,phase,deadline FROM order_rounds WHERE order_id=? AND status='active' ORDER BY round_no DESC LIMIT 1", (order_id,)).fetchone()
    return dict(row)


def _round_phones(store, order_id, round_no):
    with sqlite3.connect(store.path) as connection:
        return [row[0] for row in connection.execute("SELECT staff_phone FROM order_recipients WHERE order_id=? AND round_no=? ORDER BY staff_phone", (order_id, round_no))]
