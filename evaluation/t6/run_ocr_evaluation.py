"""Reproducible local-only T6 OCR evaluation with fictional tickets."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from coffee_house.hackathon2.auth import PersonnelAuth
from coffee_house.hackathon2.catalog import CatalogService
from coffee_house.hackathon2.image_ocr import TesseractEngine
from coffee_house.hackathon2.storage import DuplicateConflict, H2Store
from coffee_house.hackathon2.tickets import TicketReviewService

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "evaluation" / "t6"
FIXTURE_DIR = ROOT / "data" / "demo-hackathon2"
CATALOG_PATH = ROOT / "data" / "catalog.json"
NOW = "2026-10-09T12:00:00+00:00"
ADMIN = "+5215550000001"
CASHIER = "+5215550000003"
PEPPER = "local-test-pepper-that-is-never-a-real-secret-123456"


def image_bytes(text: str) -> bytes:
    image = Image.new("RGB", (1200, 500), "white")
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    font = ImageFont.truetype(str(font_path), 48) if font_path.exists() else ImageFont.load_default(size=42)
    ImageDraw.Draw(image).multiline_text((45, 40), text, fill="black", font=font, spacing=28)
    result = io.BytesIO()
    image.save(result, format="PNG")
    return result.getvalue()


def transformed(payload: bytes, action) -> bytes:
    with Image.open(io.BytesIO(payload)) as original:
        image = action(original.copy())
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()


def normalize_proposal(proposal: list[dict]) -> dict:
    recognized = [
        {key: line[key] for key in ("product_id", "variant", "quantity", "confidence")}
        for line in proposal if line.get("status") == "recognized"
    ]
    return {"recognized": recognized, "unresolved_count": len(proposal) - len(recognized)}


async def issue_code(auth: PersonnelAuth, phone: str) -> None:
    messages = []

    async def send(_recipient, body):
        messages.append(body)

    if not await auth.request_code(phone, send, now=NOW):
        raise RuntimeError("No se pudo preparar la sesión sintética.")
    code = re.search(r"\b([0-9]{8})\b", messages[0]).group(1)
    if not auth.verify_code(phone, code, now=NOW):
        raise RuntimeError("No se pudo autenticar la sesión sintética.")


def main() -> None:
    executable = os.getenv("TESSERACT_CMD")
    if not executable:
        raise SystemExit("Configura TESSERACT_CMD al ejecutable OCR aislado.")
    engine = TesseractEngine(executable, timeout_seconds=20)
    version = subprocess.run([executable, "--version"], capture_output=True, text=True, check=True).stdout.splitlines()[0]
    model_path = Path(os.environ["TESSDATA_PREFIX"]) / "spa.traineddata"
    model_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()

    clean = image_bytes("COFFEE HOUSE\n1 Hot Latte mediano $70.00\nTOTAL $70.00")
    blurred = transformed(clean, lambda image: image.filter(ImageFilter.GaussianBlur(radius=5)))
    cropped = transformed(clean, lambda image: image.crop((0, 0, 650, image.height)))
    rotated = transformed(clean, lambda image: image.rotate(180, expand=True))
    rotated_90 = transformed(clean, lambda image: image.rotate(90, expand=True))
    rotated_270 = transformed(clean, lambda image: image.rotate(270, expand=True))
    unknown = image_bytes("COFFEE HOUSE\n1 Latte de temporada mediano $99.00\nTOTAL $99.00")
    cases = {"limpio": clean, "girado_180": rotated, "girado_90": rotated_90,
             "girado_270": rotated_270, "borroso": blurred,
             "parcial": cropped, "producto_desconocido": unknown}
    case_results = {}
    expected = {"product_id": "hot_latte", "variant": "mediano", "quantity": 1}
    for name, payload in cases.items():
        started = time.perf_counter()
        document = engine.recognize(payload, declared_mime="image/png")
        service_placeholder = document
        result = normalize_proposal(_proposal(service_placeholder))
        elapsed = round((time.perf_counter() - started) * 1000)
        recognized = result["recognized"]
        if name in {"limpio", "girado_180", "girado_90", "girado_270"}:
            passed = any(all(line.get(key) == value for key, value in expected.items()) for line in recognized)
        elif name == "producto_desconocido":
            passed = not recognized
        else:
            passed = all(all(line.get(key) == value for key, value in expected.items()) for line in recognized)
        case_results[name] = {**result, "elapsed_ms": elapsed, "passed": passed}

    with tempfile.TemporaryDirectory(prefix="coffee-house-h2-t6-") as scratch:
        store = H2Store(Path(scratch) / "h2.sqlite3")
        store.seed_demo_directory(FIXTURE_DIR)
        store.create_staff(ADMIN, "admin", now=NOW)
        store.create_staff(CASHIER, "cashier", invited_by=ADMIN, now=NOW)
        auth = PersonnelAuth(store, otp_pepper=PEPPER)
        asyncio.run(issue_code(auth, CASHIER))
        catalog = CatalogService(CATALOG_PATH, store)
        service = TicketReviewService(store, auth, catalog, engine=engine)
        stock_before = store.inventory_snapshot()
        started = time.perf_counter()
        ticket = service.receive_image(CASHIER, clean, declared_mime="image/png", now=NOW)
        proposal_ms = round((time.perf_counter() - started) * 1000)
        stock_unchanged_before_confirmation = store.inventory_snapshot() == stock_before
        duplicate_rejected = False
        try:
            service.receive_image(CASHIER, clean, declared_mime="image/png", now=NOW)
        except DuplicateConflict:
            duplicate_rejected = True
        confirmed = service.confirm(CASHIER, ticket["ticket_id"], now=NOW)
        stock_after = store.inventory_snapshot()
        expected_delta = {"grano_cafe": 18, "leche_entera": 220, "vaso_12oz": 1}
        movements = store.inventory_movements(source_type="pos_ticket", source_id=ticket["ticket_id"])
        no_partial_movement = len(movements) == len(expected_delta)
        stock_confirmed_correctly = all(
            stock_after[item]["on_hand"] == stock_before[item]["on_hand"] - quantity
            for item, quantity in expected_delta.items()
        )
        safely_closed = (stock_unchanged_before_confirmation and duplicate_rejected and no_partial_movement
                         and stock_confirmed_correctly and confirmed["status"] == "confirmed")

    document = {
        "date": "2026-10-09",
        "scope": "solo imágenes, números y tickets ficticios; sin conexión a Meta ni a modelos",
        "engine": version,
        "language": "spa",
        "traineddata_source": "tesseract-ocr/tessdata_fast@923915d4ced2a7235221788285785a29c4a42d4a",
        "traineddata_sha256": model_hash,
        "cases": case_results,
        "workflow": {
            "proposal_ms": proposal_ms,
            "stock_unchanged_before_confirmation": stock_unchanged_before_confirmation,
            "duplicate_photo_rejected": duplicate_rejected,
            "confirmed_status": confirmed["status"],
            "movement_count": len(movements),
            "stock_delta_matches_demo_recipe": stock_confirmed_correctly,
            "no_partial_movement": no_partial_movement,
        },
    }
    if not all(result["passed"] for result in case_results.values()) or not safely_closed:
        document["result"] = "FAILED"
        raise SystemExit(json.dumps(document, ensure_ascii=False, indent=2))
    document["result"] = "PASSED"
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "t6-ocr-results-2026-10-09.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(document, ensure_ascii=False, indent=2))


def _proposal(document) -> list[dict]:
    # The evaluator uses the same catalog-bound parser as the app, with a disposable DB.
    with tempfile.TemporaryDirectory(prefix="coffee-house-h2-parser-") as scratch:
        store = H2Store(Path(scratch) / "h2.sqlite3")
        store.seed_demo_directory(FIXTURE_DIR)
        auth = PersonnelAuth(store, otp_pepper=PEPPER)
        catalog = CatalogService(CATALOG_PATH, store)
        service = TicketReviewService(store, auth, catalog, engine=object())
        return service.propose_lines(document)


if __name__ == "__main__":
    main()
