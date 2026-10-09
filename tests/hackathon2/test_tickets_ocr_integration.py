import io
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from test_tickets import make_service

from coffee_house.hackathon2.image_ocr import TesseractEngine
from coffee_house.hackathon2.tickets import TicketReviewService

pytestmark = pytest.mark.skipif(not os.getenv("TESSERACT_CMD"), reason="Requiere la instalación OCR aislada")


def ticket_image(text: str) -> bytes:
    image = Image.new("RGB", (1200, 500), "white")
    draw = ImageDraw.Draw(image)
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    font = ImageFont.truetype(str(font_path), 48) if font_path.exists() else ImageFont.load_default(size=42)
    draw.multiline_text((45, 40), text, fill="black", font=font, spacing=28)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_tesseract_spanish_reads_clean_and_rotated_synthetic_ticket(tmp_path):
    store, auth, service, _stub = make_service(tmp_path)
    engine = TesseractEngine(os.environ["TESSERACT_CMD"], timeout_seconds=20)
    service = TicketReviewService(store, auth, service.catalog, engine=engine)
    original = ticket_image("COFFEE HOUSE\n1 Hot Latte mediano $70.00\nTOTAL $70.00")
    rotated = [_transform(original, lambda image, angle=angle: image.rotate(angle, expand=True))
               for angle in (180, 90, 270)]
    for image in (original, *rotated):
        document = engine.recognize(image, declared_mime="image/png")
        proposal = service.propose_lines(document)
        assert any(line.get("status") == "recognized" and line.get("product_id") == "hot_latte"
                   and line.get("variant") == "mediano" for line in proposal), (document, proposal)


def test_blurred_cropped_and_unknown_receipts_never_auto_confirm(tmp_path):
    store, auth, base_service, _stub = make_service(tmp_path)
    engine = TesseractEngine(os.environ["TESSERACT_CMD"], timeout_seconds=20)
    service = TicketReviewService(store, auth, base_service.catalog, engine=engine)
    original = ticket_image("COFFEE HOUSE\n1 Hot Latte mediano $70.00\nTOTAL $70.00")
    blurred = _transform(original, lambda source: source.filter(ImageFilter.GaussianBlur(radius=5)))
    cropped = _transform(original, lambda source: source.crop((0, 0, 650, source.height)))
    unknown = ticket_image("COFFEE HOUSE\n1 Latte de temporada mediano $99.00\nTOTAL $99.00")
    for case, payload in (("blurred", blurred), ("cropped", cropped), ("unknown", unknown)):
        document = engine.recognize(payload, declared_mime="image/png")
        proposal = service.propose_lines(document)
        if case == "unknown":
            assert not any(line.get("status") == "recognized" for line in proposal), (document, proposal)
        else:
            # A damaged receipt may remain readable; its proposed lines still need an explicit human confirmation.
            assert proposal
            assert all(line.get("status") in {"recognized", "unresolved"} for line in proposal)


def _transform(payload, transform):
    with Image.open(io.BytesIO(payload)) as original:
        output = io.BytesIO()
        transform(original.copy()).save(output, format="PNG")
    return output.getvalue()
