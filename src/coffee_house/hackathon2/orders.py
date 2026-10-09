"""Confirmed customer orders, staff routing, and customer-safe status replies."""
from __future__ import annotations

import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .auth import PersonnelAuth
from .catalog import CatalogService
from .storage import H2StorageError, H2Store, InventoryShortage


class OrderUnavailable(ValueError):
    """The current catalog or demo inventory cannot support the requested draft."""


class OrderInventoryShortage(InventoryShortage):
    """A known item has too little verified stock for the requested quantity."""

    def __init__(self, product_id: str, variant: str, modifier_ids: list[str], quantity: int):
        super().__init__("La existencia disponible cambió o no alcanza.")
        self.product_id = product_id
        self.variant = variant
        self.modifier_ids = list(modifier_ids)
        self.quantity = quantity


@dataclass(frozen=True, slots=True)
class OrderReply:
    state: str
    text: str
    folio: str | None = None


class OrderWorkflow:
    """Business policy for confirmation, routing, and authorized order actions.

    Customer facts and totals come from the catalog/SQLite snapshot. The model is
    not called here and cannot create, price, accept, or discount an order.
    """

    def __init__(self, store: H2Store, catalog: CatalogService, auth: PersonnelAuth):
        self.store = store
        self.catalog = catalog
        self.auth = auth

    def prepare_confirmation(self, customer_phone: str, items: list[dict], *, now: str | None = None) -> OrderReply:
        moment = _utc(now)
        normalized, lines, total, provisional = self._price_and_check(items)
        preview = _confirmation_text(lines, total, provisional)
        state = self.store.conversation(customer_phone, now=_iso(moment)) or {}
        state["pending_order_confirmation"] = {
            "draft_id": secrets.token_hex(16),
            "folio": f"CH-{secrets.token_hex(4).upper()}",
            "items": normalized,
            "lines": lines,
            "total_cents": total,
            "provisional": provisional,
            "prepared_at": _iso(moment),
            "expires_at": _iso(moment + timedelta(minutes=10)),
        }
        self.store.save_conversation(customer_phone, state, expires_at=_iso(moment + timedelta(minutes=10)), now=_iso(moment))
        return OrderReply("confirmation_required", preview)

    def update_confirmation(self, customer_phone: str, items: list[dict], *, now: str | None = None) -> OrderReply:
        """Replace a customer's pending draft and request confirmation again."""
        return self.prepare_confirmation(customer_phone, items, now=now)

    def respond_to_confirmation(self, customer_phone: str, response: str, *, now: str | None = None) -> OrderReply:
        moment = _utc(now)
        state = self.store.conversation(customer_phone, now=_iso(moment)) or {}
        draft = state.get("pending_order_confirmation")
        if not isinstance(draft, dict):
            return OrderReply("no_pending_draft", "No tengo un pedido pendiente de confirmación. Si quieres, dime qué te gustaría pedir.")
        decision = _confirmation_decision(response)
        if decision == "ambiguous":
            return OrderReply("needs_clarification", "¿Confirmas el pedido del resumen o quieres cambiar algo?")
        if decision == "no":
            state.pop("pending_order_confirmation", None)
            self.store.save_conversation(customer_phone, state, expires_at=_iso(moment + timedelta(hours=24)), now=_iso(moment))
            return OrderReply("not_submitted", "De acuerdo, no enviaré el pedido. Si quieres cambiarlo, dime qué ajustamos.")
        if decision == "change":
            return OrderReply("edit_requested", "Claro, dime qué producto, tamaño, cantidad o extra quieres cambiar y te muestro el nuevo total.")
        try:
            normalized, lines, total, provisional = self._price_and_check(draft.get("items"))
        except OrderInventoryShortage as exc:
            state.pop("pending_order_confirmation", None)
            self.store.save_conversation(
                customer_phone, state, expires_at=_iso(moment + timedelta(hours=24)), now=_iso(moment)
            )
            return OrderReply("reconfirmation_required", self._shortage_message(exc))
        except (OrderUnavailable, InventoryShortage):
            state.pop("pending_order_confirmation", None)
            self.store.save_conversation(
                customer_phone, state, expires_at=_iso(moment + timedelta(hours=24)), now=_iso(moment)
            )
            return OrderReply(
                "reconfirmation_required",
                "Acabo de revisar el pedido y no puedo confirmar la existencia o el precio. "
                "No se envió ni se descontó nada; el personal te lo puede confirmar.",
            )
        if lines != draft.get("lines") or total != draft.get("total_cents") or provisional != draft.get("provisional"):
            draft.update({"items": normalized, "lines": lines, "total_cents": total, "provisional": provisional,
                          "prepared_at": _iso(moment), "expires_at": _iso(moment + timedelta(minutes=10))})
            state["pending_order_confirmation"] = draft
            self.store.save_conversation(customer_phone, state, expires_at=_iso(moment + timedelta(minutes=10)), now=_iso(moment))
            return OrderReply("confirmation_required", "El menú o la existencia cambió. Revisa este nuevo resumen antes de confirmar:\n\n"
                              + _confirmation_text(lines, total, provisional), draft.get("folio"))
        staff = self.store.active_order_staff()
        if staff["admins"] == 0:
            return OrderReply("paused", "Por el momento no hay un administrador disponible para recibir pedidos. Intenta más tarde.")
        timeout_minutes = 20 if staff["cashiers"] else 10
        expiry = _iso(moment + timedelta(minutes=timeout_minutes))
        folio = draft.get("folio")
        if not isinstance(folio, str) or not folio.startswith("CH-"):
            return OrderReply("paused", "No pude recuperar el resumen. Vuelve a preparar tu pedido para intentarlo otra vez.")
        staff_notice = _staff_notice(folio, lines, total, provisional)
        try:
            created = self.store.submit_order(
                folio=folio, idempotency_key=f"wa-confirm:{draft['draft_id']}",
                customer_phone=customer_phone, items=normalized, total_cents=total,
                expires_at=expiry, staff_notice=staff_notice, now=_iso(moment),
            )
        except (H2StorageError, ValueError):
            return OrderReply("paused", "No pude enviar el pedido con seguridad. No se descontó inventario; intenta de nuevo en un momento.")
        state.pop("pending_order_confirmation", None)
        self.store.save_conversation(customer_phone, state, expires_at=_iso(moment + timedelta(hours=24)), now=_iso(moment))
        if created["status"] != "pending_staff":
            return OrderReply("already_processed", f"El pedido {created['folio']} ya se está atendiendo.", created["folio"])
        return OrderReply("pending_staff", f"Gracias. Envié tu pedido al personal para que lo confirme. Te avisaré en cuanto lo revisen. Tu folio es {created['folio']}.", created["folio"])

    def status(self, customer_phone: str, folio: str | None = None) -> OrderReply:
        result = self.store.customer_order_status(customer_phone, folio)
        if result is None:
            return OrderReply("not_found", "No encuentro un pedido asociado a este número. Si quieres, puedo ayudarte con otra consulta.")
        return OrderReply("status", f"Pedido {result['folio']}: {result['status']}.", result["folio"])

    def accept(self, actor_phone: str, folio: str, *, now: str | None = None) -> None:
        self.auth.authorize(actor_phone, "order.accept", folio=folio, now=now)
        order = self._staff_order(folio)
        self.store.accept_order(
            order["order_id"], actor_phone,
            customer_message=f"El personal aceptó tu pedido {folio}. Te avisaré cuando esté en preparación, listo para recoger y entregado.",
            staff_message=f"El pedido {folio} fue aceptado. La venta y el descuento de la existencia quedaron registrados.",
            now=now,
        )

    def pass_to_next(self, actor_phone: str, folio: str, *, now: str | None = None) -> dict:
        self.auth.authorize(actor_phone, "order.pass", folio=folio, now=now)
        order = self._staff_order(folio)
        return self.store.pass_order(
            order["order_id"], actor_phone,
            notice=f"Pedido {folio}: requiere revisión. Responde aceptar o pasar al siguiente.", now=now,
        )

    def reject(self, actor_phone: str, folio: str, *, now: str | None = None) -> None:
        self.auth.authorize(actor_phone, "order.reject", folio=folio, now=now)
        order = self._staff_order(folio)
        self.store.reject_order(
            order["order_id"], actor_phone,
            customer_message=f"Lo sentimos, no podremos preparar el pedido {folio}. No se descontó inventario. Si quieres, revisamos otra opción disponible.",
            now=now,
        )

    def advance(self, actor_phone: str, folio: str, status: str, *, now: str | None = None) -> None:
        self.auth.authorize(actor_phone, "order.advance", folio=folio, now=now)
        order = self._staff_order(folio)
        messages = {
            "preparing": f"Tu pedido {folio} está en preparación.",
            "ready": f"Tu pedido {folio} está listo para recoger.",
            "delivered": f"Tu pedido {folio} fue entregado. ¡Gracias por visitarnos!",
        }
        if status not in messages:
            raise ValueError("El estado solicitado no está permitido.")
        self.store.transition_order(order["order_id"], actor_phone, status, message=messages[status], now=now)

    def expire_or_route_due_assignments(self, *, now: str | None = None) -> int:
        return self.store.advance_expired_order_assignments(now=now)

    def _staff_order(self, folio: str) -> dict:
        order = self.store.order_for_staff_workflow(folio)
        if order is None:
            raise H2StorageError("No existe ese pedido.")
        return order

    def inventory_shortage_message(self, shortage: OrderInventoryShortage) -> str:
        return self._shortage_message(shortage)

    def _shortage_message(self, shortage: OrderInventoryShortage) -> str:
        text = "Lo siento, esa opción ya no alcanza. No envié el pedido ni desconté existencias."
        try:
            alternatives = self.catalog.available_alternatives(
                shortage.product_id,
                shortage.variant,
                shortage.modifier_ids,
                quantity=shortage.quantity,
            )
        except H2StorageError:
            return f"{text} No puedo revisar otras existencias justo ahora; el personal te puede ayudar."
        if alternatives:
            options = "\n".join(f"• {item['label']}" for item in alternatives)
            return f"{text}\n\nAcabo de revisar estas opciones:\n{options}\n¿Te gustaría alguna?"
        return f"{text} Por ahora no tengo otra opción confirmada; el personal te puede decir qué hay disponible."

    def staff_order(self, actor_phone: str, folio: str) -> dict:
        """Return one order summary after enforcing role and current assignment."""
        self.auth.authorize(actor_phone, "order.read", folio=folio)
        return self._staff_order(folio)

    def _price_and_check(self, items: object) -> tuple[list[dict], list[dict], int, bool]:
        if not isinstance(items, list) or not items or len(items) > 30:
            raise ValueError("El pedido debe tener entre 1 y 30 líneas explícitas.")
        normalized: list[dict] = []
        display: list[dict] = []
        total = 0
        provisional = False
        for item in items:
            required = {"product_id", "variant", "quantity", "modifier_ids"}
            allowed = required | {"product_name", "unit_price_cents", "modifier_snapshot", "line_total_cents"}
            if not isinstance(item, dict) or not required <= set(item) or set(item) - allowed:
                raise ValueError("Cada producto necesita identificador, tamaño, cantidad y extras explícitos.")
            product_id, variant, quantity, modifier_ids = (
                item["product_id"], item["variant"], item["quantity"], item["modifier_ids"]
            )
            if not isinstance(product_id, str) or not isinstance(variant, str) or type(quantity) is not int or not 1 <= quantity <= 50:
                raise ValueError("El producto, tamaño o cantidad no es válido.")
            if not isinstance(modifier_ids, list) or any(not isinstance(value, str) for value in modifier_ids):
                raise ValueError("Los extras deben elegirse de forma explícita.")
            product = self.catalog.product_by_id(product_id)
            if product is None:
                raise OrderUnavailable("No encontré el producto indicado en el menú.")
            selected = [candidate for candidate in product.get("variants", []) if candidate.get("size") == variant]
            if len(selected) != 1:
                raise OrderUnavailable("El tamaño no está confirmado en el menú.")
            selected_variant = selected[0]
            base = selected_variant.get("price_cents")
            if type(base) is not int or base < 0:
                raise OrderUnavailable("El precio de esa presentación no está confirmado.")
            extra_names = []
            for extra_id in modifier_ids:
                extra = next((entry for entry in self.catalog.extras if entry.get("id") == extra_id), None)
                if extra is None:
                    raise OrderUnavailable("No encontré uno de los extras en el menú.")
                extra_names.append(str(extra.get("name", "")))
            extras, problem = self.catalog.resolve_extras(extra_names)
            if problem or extras is None or [entry["id"] for entry in extras] != modifier_ids:
                raise OrderUnavailable(problem or "No pude validar los extras seleccionados.")
            group = product.get("extra_group")
            if group and any(group not in extra.get("groups", []) for extra in extras):
                raise OrderUnavailable("Uno de los extras no corresponde a esa bebida.")
            available = self.store.available_portions(product_id, variant, modifier_ids)
            if available is None:
                raise OrderUnavailable("No tengo existencia confirmada para esa presentación y sus extras.")
            if available < quantity:
                raise OrderInventoryShortage(product_id, variant, modifier_ids, quantity)
            extra_total = 0
            modifier_snapshot = []
            for extra in extras:
                price = extra.get("price_cents")
                if type(price) is not int or price < 0:
                    raise OrderUnavailable("El precio de uno de los extras está por confirmar.")
                extra_total += price
                provisional |= extra.get("price_status", "documentado") != "documentado"
                modifier_snapshot.append({
                    "id": extra["id"], "name": extra["name"],
                    "order_label": extra.get("order_label", f"extra {extra['name']}"),
                    "price_cents": price,
                })
            provisional |= selected_variant.get("price_status") != "documentado"
            line_total = (base + extra_total) * quantity
            total += line_total
            normalized.append({
                "product_id": product_id, "variant": variant, "quantity": quantity,
                "modifier_ids": list(modifier_ids), "product_name": product["name"],
                "unit_price_cents": base, "modifier_snapshot": modifier_snapshot,
                "line_total_cents": line_total,
            })
            display.append({"product_name": product["name"], "variant": variant, "quantity": quantity,
                            "modifier_snapshot": modifier_snapshot, "line_total_cents": line_total})
        if total > 2_000_000_000:
            raise ValueError("El total excede el límite permitido.")
        return normalized, display, total, provisional


def _confirmation_decision(response: str) -> str:
    if not isinstance(response, str) or len(response) > 300:
        return "ambiguous"
    normalized = unicodedata.normalize("NFKD", response.casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = " ".join(normalized.replace("¿", "").replace("?", "").replace("!", "").replace(",", "").replace(".", "").split())
    if normalized in {"si", "si confirmo", "confirmo", "confirmo mi pedido", "si por favor", "si porfa"}:
        return "yes"
    if normalized in {"no", "no gracias", "no lo confirmo", "cancelalo", "cancelar"}:
        return "no"
    if any(word in normalized.split() for word in ("cambia", "cambiar", "ajusta", "modifica", "quita", "agrega", "agregar")):
        return "change"
    return "ambiguous"


def _confirmation_text(lines: list[dict], total: int, provisional: bool) -> str:
    rows = ["Confirmo tu pedido:"]
    for line in lines:
        extras = line["modifier_snapshot"]
        modifier_text = f"; {', '.join(modifier.get('order_label') or ('extra ' + str(modifier['name'])) for modifier in extras)}" if extras else ""
        rows.append(f"• {line['quantity']} × {line['product_name']} {line['variant']}{modifier_text}: {_money(line['line_total_cents'])}")
    qualifier = " (precio provisional de la demo)" if provisional else ""
    rows.extend([f"Total: {_money(total)}{qualifier}.", "¿Lo confirmas? El pedido se enviará al personal; todavía no es una venta ni se ha descontado existencia."])
    return "\n".join(rows)


def _staff_notice(folio: str, lines: list[dict], total: int, provisional: bool) -> str:
    rows = [f"Pedido {folio} para revisión. Todavía no es una venta:"]
    for line in lines:
        modifiers = line["modifier_snapshot"]
        modifier_text = f"; {', '.join(modifier.get('order_label') or ('extra ' + str(modifier['name'])) for modifier in modifiers)}" if modifiers else ""
        rows.append(f"• {line['quantity']} × {line['product_name']} {line['variant']}{modifier_text}")
    rows.append(f"Total: {_money(total)}" + (" (provisional de la demo)." if provisional else "."))
    rows.append("Responde aceptar o pasar al siguiente.")
    return "\n".join(rows)


def _money(cents: int) -> str:
    return f"${cents // 100:,.0f}.{cents % 100:02d} MXN"


def _utc(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("La fecha debe incluir zona horaria.")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")
