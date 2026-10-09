"""Cashier ticket OCR review and confirmed inventory movements."""
from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone

from .auth import PersonnelAuth
from .catalog import ALIASES, CatalogService
from .image_ocr import OcrDocument, TesseractEngine
from .storage import H2StorageError, H2Store

_QUANTITY = re.compile(r"^\s*(\d{1,3})\s*(?:[x×]\s*)+(.+?)\s*$|^\s*(\d{1,3})\s+(.+?)\s*$", re.IGNORECASE)
_PRICE = re.compile(r"(?:\$\s*)?\d{1,5}[.,]\d{2}")
_SIZE = re.compile(r"\b(mediano|mediana|grande|presentacion unica|presentacion|unica)\b", re.IGNORECASE)
_HOT_COLD = {
    "hot latte": "hot_latte", "latte caliente": "hot_latte",
    "cold latte": "iced_latte", "latte frio": "iced_latte",
}
_IGNORED_TERMS = {"total", "subtotal", "iva", "cambio", "efectivo", "tarjeta", "gracias", "ticket"}
_MIN_CONFIDENCE = 0.65


class TicketReviewService:
    """All OCR output remains local and is presented to authorized staff only."""

    def __init__(self, store: H2Store, auth: PersonnelAuth, catalog: CatalogService, *, engine=None):
        self.store = store
        self.auth = auth
        self.catalog = catalog
        self.engine = engine or TesseractEngine()

    def receive_image(
        self, actor_phone: str, image_bytes: bytes, *, declared_mime: str | None = None,
        now: str | None = None,
    ) -> dict:
        self.auth.authorize(actor_phone, "ticket.review", now=now)
        document: OcrDocument = self.engine.recognize(image_bytes, declared_mime=declared_mime)
        lines = self.propose_lines(document)
        if not lines:
            lines = [{"status": "unresolved", "confidence": 0.0, "source_text": "No se detectaron líneas legibles"}]
        moment = _utc(now)
        ticket_id = "POS-" + uuid.uuid4().hex[:16].upper()
        fingerprint = hashlib.sha256(image_bytes).hexdigest()
        expires_at = (moment + timedelta(hours=24)).isoformat(timespec="milliseconds")
        self.store.create_pos_ticket(ticket_id, actor_phone, lines, fingerprint, expires_at=expires_at,
                                     now=moment.isoformat(timespec="milliseconds"))
        return {"ticket_id": ticket_id, "status": "needs_review", "lines": lines,
                "message": self.format_proposal(ticket_id, lines)}

    def review(self, actor_phone: str, ticket_id: str, *, now: str | None = None) -> dict:
        self.auth.authorize(actor_phone, "ticket.review", now=now)
        ticket = self.store.pos_ticket(ticket_id)
        if ticket is None or ticket["status"] != "needs_review":
            raise H2StorageError("No encontré un ticket pendiente para revisar.")
        if ticket["sender_phone"] != actor_phone and self.auth.principal(actor_phone, now=now).role != "admin":
            raise H2StorageError("Solo quien recibió el ticket o un administrador puede revisarlo.")
        return ticket

    def correct_lines(self, actor_phone: str, ticket_id: str, lines: list[dict], *, now: str | None = None) -> dict:
        self.auth.authorize(actor_phone, "ticket.review", now=now)
        ticket = self.store.pos_ticket(ticket_id)
        principal = self.auth.principal(actor_phone, now=now)
        if ticket is None or (ticket["sender_phone"] != actor_phone and principal.role != "admin"):
            raise H2StorageError("No encontré un ticket que puedas corregir.")
        self.store.revise_pos_ticket_lines(ticket_id, actor_phone, lines, now=now)
        corrected = self.store.pos_ticket(ticket_id)
        return {"ticket_id": ticket_id, "status": corrected["status"], "lines": corrected["lines"],
                "message": self.format_proposal(ticket_id, corrected["lines"])}

    def correct_from_text(self, actor_phone: str, ticket_id: str, corrections: str, *, now: str | None = None) -> dict:
        """Apply explicit staff-confirmed lines such as `2x Latte mediano; 1 Matcha grande`."""
        if not isinstance(corrections, str) or not corrections.strip() or len(corrections) > 2000:
            raise ValueError("Escribe las líneas corregidas, separadas por punto y coma.")
        lines = []
        for raw in re.split(r"[;\n]+", corrections):
            entry = raw.strip()
            if not entry:
                continue
            match = _QUANTITY.fullmatch(entry)
            if match is None:
                raise ValueError("Indica cada línea con cantidad, por ejemplo: 2x latte mediano.")
            quantity = int(match.group(1) or match.group(3))
            description = (match.group(2) or match.group(4)).strip()
            if not 1 <= quantity <= 100:
                raise ValueError("La cantidad de una línea no es válida.")
            product, variant = self._resolve_line(description)
            if product is None or variant is None:
                raise ValueError("No pude identificar un producto o tamaño. Revisa el menú y vuelve a escribir las líneas.")
            lines.append({"product_id": product["id"], "variant": variant, "quantity": quantity})
        if not lines:
            raise ValueError("Escribe al menos una línea corregida.")
        return self.correct_lines(actor_phone, ticket_id, lines, now=now)

    def confirm(self, actor_phone: str, ticket_id: str, *, now: str | None = None) -> dict:
        principal = self.auth.authorize(actor_phone, "ticket.commit", now=now)
        ticket = self.store.pos_ticket(ticket_id)
        if ticket is None or (ticket["sender_phone"] != actor_phone and principal.role != "admin"):
            raise H2StorageError("No encontré un ticket que puedas confirmar.")
        movements = self.store.confirm_pos_ticket(ticket_id, actor_phone, now=now)
        return {"ticket_id": ticket_id, "status": "confirmed", "movements": movements,
                "message": "Venta registrada y existencias actualizadas."}

    def reject(self, actor_phone: str, ticket_id: str, *, now: str | None = None) -> None:
        principal = self.auth.authorize(actor_phone, "ticket.review", now=now)
        ticket = self.store.pos_ticket(ticket_id)
        if ticket is None or (ticket["sender_phone"] != actor_phone and principal.role != "admin"):
            raise H2StorageError("No encontré un ticket que puedas rechazar.")
        self.store.reject_pos_ticket(ticket_id, actor_phone, now=now)

    def propose_lines(self, document: OcrDocument) -> list[dict]:
        proposals = []
        for ocr_line in document.lines[:50]:
            has_price = bool(_PRICE.search(ocr_line.text))
            source = _clean_line(ocr_line.text)
            if not source or _is_receipt_footer(source):
                continue
            match = _QUANTITY.fullmatch(source)
            confidence = max(0.0, min(1.0, float(ocr_line.confidence)))
            if match is None:
                if has_price or _could_be_catalog_line(source, self.catalog):
                    proposals.append(_unresolved(source, confidence))
                continue
            quantity = int(match.group(1) or match.group(3))
            description = (match.group(2) or match.group(4)).strip()
            if not 1 <= quantity <= 100:
                proposals.append(_unresolved(source, confidence))
                continue
            product, variant = self._resolve_line(description)
            if product is None or variant is None or confidence < _MIN_CONFIDENCE:
                proposals.append(_unresolved(source, confidence))
                continue
            proposals.append({"status": "recognized", "product_id": product["id"], "variant": variant,
                              "quantity": quantity, "confidence": confidence, "source_text": source[:512]})
        return proposals

    def _resolve_line(self, description: str) -> tuple[dict | None, str | None]:
        normalized = _normalize(description)
        if not normalized:
            return None, None
        size_match = _SIZE.search(normalized)
        size = None
        if size_match:
            size = {"mediano": "mediano", "mediana": "mediano", "grande": "grande",
                    "presentacion unica": "presentacion_unica", "presentacion": "presentacion_unica",
                    "unica": "presentacion_unica"}[size_match.group(1)]
            normalized = _normalize(_SIZE.sub(" ", normalized))
        target_id = {product_id for alias, product_id in {**ALIASES, **_HOT_COLD}.items()
                     if normalized == _normalize(alias)}
        candidates = []
        for product in self.catalog.products:
            name = _normalize(str(product.get("name", "")))
            identifier = _normalize(str(product.get("id", "").replace("_", " ")))
            if normalized and normalized in {name, identifier}:
                candidates.append(product)
        candidates.extend(item for item in self.catalog.products if item.get("id") in target_id)
        if not candidates:
            return None, None
        unique_candidates = {product["id"]: product for product in candidates}
        if len(unique_candidates) != 1:
            return None, None
        product = next(iter(unique_candidates.values()))
        variants = product.get("variants", [])
        if size is None:
            if len(variants) != 1:
                return None, None
            size = variants[0].get("size")
        if not any(variant.get("size") == size for variant in variants):
            return None, None
        # Only products with an approved synthetic recipe can cause tracked movements.
        if self.store.available_portions(product["id"], size) is None:
            return None, None
        return product, size

    def format_proposal(self, ticket_id: str, lines: list[dict]) -> str:
        display = []
        for line in lines:
            if line.get("status") == "recognized":
                product = next((item for item in self.catalog.products if item.get("id") == line["product_id"]), None)
                name = product["name"] if product else "Producto"
                size = {"mediano": "mediano", "grande": "grande", "presentacion_unica": "presentación única"}.get(line["variant"], line["variant"])
                display.append(f"• {line['quantity']} × {name}, {size}")
            else:
                display.append("• Esta línea no quedó clara y necesita revisión manual.")
        return (f"Leí estas líneas del ticket {ticket_id}:\n" + "\n".join(display)
                + f"\n\nRevisa el ticket y confirma con CONFIRMAR {ticket_id}. Si algo no coincide, corrígelo antes de confirmar.")


def _unresolved(source: str, confidence: float) -> dict:
    return {"status": "unresolved", "confidence": confidence, "source_text": source[:512] or "Línea sin texto"}


def _clean_line(value: str) -> str:
    value = _PRICE.sub(" ", value)
    return re.sub(r"\s+", " ", value).strip(" \t|•-:")[:512]


def _is_receipt_footer(value: str) -> bool:
    normalized = _normalize(value)
    return any(term in normalized.split() for term in _IGNORED_TERMS) and not _QUANTITY.fullmatch(value)


def _could_be_catalog_line(value: str, catalog: CatalogService) -> bool:
    normalized = _normalize(value)
    if _SIZE.search(normalized):
        return True
    phrases = [_normalize(alias) for alias in {**ALIASES, **_HOT_COLD}]
    phrases.extend(_normalize(str(product.get("name", ""))) for product in catalog.products)
    return any(_contains_phrase(normalized, phrase) for phrase in phrases if phrase)


def _contains_phrase(text: str, phrase: str) -> bool:
    return bool(phrase and f" {phrase} " in f" {text} ")


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    plain = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", plain)).strip()


def _utc(value: str | None) -> datetime:
    result = datetime.now(timezone.utc) if value is None else datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("La fecha debe incluir zona horaria.")
    return result.astimezone(timezone.utc)
