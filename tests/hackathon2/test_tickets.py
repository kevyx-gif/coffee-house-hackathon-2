import asyncio
import io
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image, ImageFont

from coffee_house.hackathon2.auth import AuthorizationError, PersonnelAuth
from coffee_house.hackathon2.catalog import CatalogService
from coffee_house.hackathon2.image_ocr import (
    OcrDocument,
    OcrInputError,
    OcrLine,
    validate_image,
)
from coffee_house.hackathon2.storage import (
    DuplicateConflict,
    H2StorageError,
    H2Store,
    InventoryShortage,
)
from coffee_house.hackathon2.tickets import TicketReviewService

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / "data" / "demo-hackathon2"
CATALOG_PATH = ROOT / "data" / "catalog.json"
PEPPER = "local-test-pepper-that-is-never-a-real-secret-123456"
ADMIN = "+5215550000001"
MANAGER = "+5215550000002"
CASHIER = "+5215550000003"
NOW = "2026-10-09T12:00:00+00:00"


class StubOcr:
    def __init__(self, lines):
        self.document = OcrDocument(tuple(lines))
        self.calls = 0

    def recognize(self, image_bytes, *, declared_mime=None):
        self.calls += 1
        return self.document


def png_bytes(text="COFFEE HOUSE\n1 Hot Latte mediano $70.00\nTOTAL $70.00"):
    image = Image.new("RGB", (1100, 420), "white")
    from PIL import ImageDraw
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("arial.ttf", 42) if Path("C:/Windows/Fonts/arial.ttf").exists() else ImageFont.load_default()
    draw.multiline_text((40, 35), text, fill="black", font=font, spacing=24)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def make_service(tmp_path, *, lines=None):
    store = H2Store(tmp_path / "h2.sqlite3")
    store.seed_demo_directory(FIXTURE_DIR)
    store.create_staff(ADMIN, "admin", now=NOW)
    store.create_staff(CASHIER, "cashier", invited_by=ADMIN, now=NOW)
    store.create_staff(MANAGER, "manager", invited_by=ADMIN, now=NOW)
    auth = PersonnelAuth(store, otp_pepper=PEPPER)
    for phone in (ADMIN, CASHIER, MANAGER):
        _authenticate(auth, phone)
    engine = StubOcr(lines or [OcrLine("1 Hot Latte mediano $70.00", 0.96), OcrLine("TOTAL $70.00", 0.99)])
    catalog = CatalogService(CATALOG_PATH, store)
    return store, auth, TicketReviewService(store, auth, catalog, engine=engine), engine


def test_image_validation_accepts_jpeg_png_and_rejects_mismatched_or_corrupt(tmp_path):
    png = png_bytes()
    assert validate_image(png, "image/png") == "image/png"
    image = Image.open(io.BytesIO(png)).convert("RGB")
    jpeg_file = io.BytesIO()
    image.save(jpeg_file, format="JPEG")
    jpeg = jpeg_file.getvalue()
    assert validate_image(jpeg, "image/jpeg") == "image/jpeg"
    with pytest.raises(OcrInputError):
        validate_image(png, "image/jpeg")
    with pytest.raises(OcrInputError):
        validate_image(b"not an image")
    with pytest.raises(OcrInputError):
        validate_image(png + (b"x" * (5 * 1024 * 1024)))
    oversized = Image.new("RGB", (8_001, 1), "white")
    oversized_file = io.BytesIO()
    oversized.save(oversized_file, format="PNG")
    with pytest.raises(OcrInputError):
        validate_image(oversized_file.getvalue())


def test_receive_only_proposes_lines_and_never_changes_stock(tmp_path):
    store, _auth, service, engine = make_service(tmp_path)
    before = store.inventory_snapshot()
    proposal = service.receive_image(CASHIER, png_bytes(), declared_mime="image/png", now=NOW)
    assert engine.calls == 1
    assert proposal["status"] == "needs_review"
    assert proposal["lines"][0]["status"] == "recognized"
    assert proposal["lines"][0]["product_id"] == "hot_latte"
    assert "CONFIRMAR" in proposal["message"]
    assert store.inventory_snapshot() == before
    assert store.inventory_movements(source_type="pos_ticket") == []
    ticket = store.pos_ticket(proposal["ticket_id"])
    assert ticket["source"] == "pos_receipt"
    assert ticket["status"] == "needs_review"


def test_confirmation_is_explicit_role_checked_atomic_audited_and_idempotent(tmp_path):
    store, _auth, service, _engine = make_service(tmp_path)
    proposal = service.receive_image(CASHIER, png_bytes(), declared_mime="image/png", now=NOW)
    before = store.inventory_snapshot()
    with pytest.raises(AuthorizationError):
        service.confirm(MANAGER, proposal["ticket_id"], now=NOW)
    assert store.inventory_snapshot() == before
    result = service.confirm(CASHIER, proposal["ticket_id"], now=NOW)
    assert result["status"] == "confirmed"
    assert result["movements"] == {"grano_cafe": 18, "leche_entera": 220, "vaso_12oz": 1}
    after = store.inventory_snapshot()
    assert after["grano_cafe"]["on_hand"] == before["grano_cafe"]["on_hand"] - 18
    assert after["leche_entera"]["on_hand"] == before["leche_entera"]["on_hand"] - 220
    assert after["vaso_12oz"]["on_hand"] == before["vaso_12oz"]["on_hand"] - 1
    assert len(store.inventory_movements(source_type="pos_ticket", source_id=proposal["ticket_id"])) == 3
    assert store.pos_ticket(proposal["ticket_id"])["status"] == "confirmed"
    with pytest.raises(H2StorageError):
        service.confirm(CASHIER, proposal["ticket_id"], now=NOW)
    assert store.inventory_snapshot() == after
    assert sum(row["action"] == "pos_ticket_confirmed" for row in store.audit_rows()) == 1


def test_low_confidence_unknown_and_partial_lines_require_manual_correction(tmp_path):
    store, _auth, service, _engine = make_service(tmp_path, lines=[
        OcrLine("1 Hot Latte mediano $70.00", 0.42),
        OcrLine("1 Latte de temporada $99.00", 0.95),
    ])
    before = store.inventory_snapshot()
    proposal = service.receive_image(CASHIER, png_bytes(), declared_mime="image/png", now=NOW)
    assert all(line["status"] == "unresolved" for line in proposal["lines"])
    assert store.inventory_snapshot() == before
    with pytest.raises(H2StorageError):
        service.confirm(CASHIER, proposal["ticket_id"], now=NOW)
    corrected = service.correct_lines(CASHIER, proposal["ticket_id"], [
        {"product_id": "hot_latte", "variant": "mediano", "quantity": 1},
    ], now=NOW)
    assert corrected["lines"][0]["status"] == "recognized"
    service.confirm(CASHIER, proposal["ticket_id"], now=NOW)
    assert store.inventory_snapshot()["grano_cafe"]["on_hand"] == before["grano_cafe"]["on_hand"] - 18


def test_duplicate_receipt_is_rejected_and_reject_does_not_move_stock(tmp_path):
    store, _auth, service, _engine = make_service(tmp_path)
    image = png_bytes()
    proposal = service.receive_image(CASHIER, image, declared_mime="image/png", now=NOW)
    before = store.inventory_snapshot()
    with pytest.raises(DuplicateConflict):
        service.receive_image(CASHIER, image, declared_mime="image/png", now=NOW)
    service.reject(CASHIER, proposal["ticket_id"], now=NOW)
    assert store.pos_ticket(proposal["ticket_id"])["status"] == "rejected"
    assert store.inventory_snapshot() == before
    assert store.inventory_movements(source_type="pos_ticket") == []


def test_insufficient_stock_rolls_back_ticket_and_all_recipe_movements(tmp_path):
    store, _auth, service, _engine = make_service(tmp_path)
    proposal = service.receive_image(CASHIER, png_bytes(), declared_mime="image/png", now=NOW)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE inventory_items SET on_hand=0 WHERE item_id='leche_entera'")
        connection.commit()
    before = store.inventory_snapshot()
    with pytest.raises(InventoryShortage):
        service.confirm(CASHIER, proposal["ticket_id"], now=NOW)
    assert store.inventory_snapshot() == before
    assert store.pos_ticket(proposal["ticket_id"])["status"] == "needs_review"
    assert store.inventory_movements(source_type="pos_ticket") == []


def test_concurrent_confirmations_can_apply_a_ticket_once(tmp_path):
    store, _auth, service, _engine = make_service(tmp_path)
    proposal = service.receive_image(CASHIER, png_bytes(), declared_mime="image/png", now=NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: _try_confirm(service, proposal["ticket_id"]), range(2)))
    assert sorted(outcomes) == ["confirmed", "duplicate"]
    assert len(store.inventory_movements(source_type="pos_ticket", source_id=proposal["ticket_id"])) == 3


def test_two_different_tickets_racing_for_last_portion_cannot_overdraw(tmp_path):
    store, _auth, service, _engine = make_service(tmp_path)
    first = service.receive_image(CASHIER, png_bytes("POS A\n1 Hot Latte mediano $70.00"), declared_mime="image/png", now=NOW)
    second = service.receive_image(CASHIER, png_bytes("POS B\n1 Hot Latte mediano $70.00"), declared_mime="image/png", now=NOW)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE inventory_items SET on_hand=18 WHERE item_id='grano_cafe'")
        connection.execute("UPDATE inventory_items SET on_hand=220 WHERE item_id='leche_entera'")
        connection.execute("UPDATE inventory_items SET on_hand=1 WHERE item_id='vaso_12oz'")
        connection.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda ticket: _try_confirm(service, ticket),
                                 (first["ticket_id"], second["ticket_id"])))
    assert sorted(outcomes) == ["confirmed", "duplicate"]
    snapshot = store.inventory_snapshot()
    assert snapshot["grano_cafe"]["on_hand"] == 0
    assert snapshot["leche_entera"]["on_hand"] == 0
    assert snapshot["vaso_12oz"]["on_hand"] == 0
    assert len(store.inventory_movements(source_type="pos_ticket")) == 3
    statuses = {store.pos_ticket(ticket_id)["status"] for ticket_id in (first["ticket_id"], second["ticket_id"])}
    assert statuses == {"confirmed", "needs_review"}


def test_admin_can_confirm_as_backup_but_another_cashier_cannot(tmp_path):
    store, _auth, service, _engine = make_service(tmp_path)
    second_cashier = "+5215550000005"
    store.create_staff(second_cashier, "cashier", invited_by=ADMIN, now=NOW)
    # A different cashier without a session is rejected before it can change stock.
    ticket = service.receive_image(CASHIER, png_bytes(), declared_mime="image/png", now=NOW)
    before = store.inventory_snapshot()
    with pytest.raises(AuthorizationError):
        service.confirm(second_cashier, ticket["ticket_id"], now=NOW)
    assert store.inventory_snapshot() == before
    result = service.confirm(ADMIN, ticket["ticket_id"], now=NOW)
    assert result["status"] == "confirmed"


def test_ticket_expiry_scrubs_ocr_contact_and_marks_pending_expired(tmp_path):
    store, _auth, service, _engine = make_service(tmp_path)
    proposal = service.receive_image(CASHIER, png_bytes(), declared_mime="image/png", now=NOW)
    later = (datetime.fromisoformat(NOW) + timedelta(hours=25)).isoformat()
    assert store.purge_expired(now=later)["tickets"] == 1
    ticket = store.pos_ticket(proposal["ticket_id"])
    assert ticket["status"] == "expired"
    assert ticket["sender_phone"] == ""
    assert ticket["lines"] == []
    assert ticket["fingerprint"] == ""


def _try_confirm(service, ticket_id):
    try:
        service.confirm(CASHIER, ticket_id, now=NOW)
        return "confirmed"
    except H2StorageError:
        return "duplicate"


def _authenticate(auth, phone):
    sent = []

    async def sender(recipient, body):
        sent.append(body)

    assert asyncio.run(auth.request_code(phone, sender, now=NOW))
    code = re.search(r"\b([0-9]{8})\b", sent[0]).group(1)
    assert auth.verify_code(phone, code, now=NOW)
