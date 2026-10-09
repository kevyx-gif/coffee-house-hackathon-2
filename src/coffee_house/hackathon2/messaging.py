"""WhatsApp-facing command router for customer orders and authenticated staff."""
from __future__ import annotations

import asyncio
import re
import uuid
from decimal import Decimal, InvalidOperation

from .auth import AuthorizationError, PersonnelAuth
from .conversation import ConversationEngine, ConversationReply
from .orders import OrderWorkflow
from .storage import H2StorageError
from .tickets import TicketReviewService
from .whatsapp import WhatsAppCloudClient

_FOLIO = r"(CH-[A-Z0-9-]{3,32})"
_TICKET = r"(POS-[A-Z0-9-]{8,32})"
_ORDER_COMMAND = re.compile(rf"^(ACEPTAR|ACEPTA|PASAR|RECHAZAR|PREPARAR|EN PREPARACI[ÓO]N|LISTO|ENTREGAR|ENTREGADO|VER)\s+{_FOLIO}$", re.IGNORECASE)
_TICKET_COMMAND = re.compile(rf"^(REVISAR|CONFIRMAR|RECHAZAR)\s+{_TICKET}$", re.IGNORECASE)
_TICKET_CORRECTION = re.compile(rf"^CORREGIR\s+{_TICKET}\s*:?\s*(.+)$", re.IGNORECASE | re.DOTALL)
_STAFF_INVITE = re.compile(r"^/alta\s+(\+[1-9][0-9]{7,14})\s+(admin(?:istrador)?|gerente|cajero)$", re.IGNORECASE)
_STAFF_ROLE = re.compile(r"^/rol\s+(\+[1-9][0-9]{7,14})\s+(admin(?:istrador)?|gerente|cajero)$", re.IGNORECASE)
_STAFF_PREFIX = re.compile(r"^(?:ACEPTAR|ACEPTA|PASAR|RECHAZAR|PREPARAR|EN PREPARACI[ÓO]N|LISTO|ENTREGAR|ENTREGADO|VER|REVISAR|CONFIRMAR|CORREGIR|/ALTA|/ROL|INVENTARIO|AJUSTAR_EXISTENCIA|PRECIO|PRECIO_EXTRA|AUDITORIA|AYUDA|/AYUDA)\b", re.IGNORECASE)
_INVENTORY_ADJUSTMENT = re.compile(r"^AJUSTAR_EXISTENCIA\s+([a-z0-9_-]{1,128})\s+([+-][0-9]{1,6})\s+(.+)$", re.IGNORECASE)
_VARIANT_PRICE = re.compile(r"^PRECIO\s+([a-z0-9_-]{1,128})\s+(mediano|grande|presentacion_unica)\s+([0-9]{1,7}(?:[.,][0-9]{1,2})?)$", re.IGNORECASE)
_EXTRA_PRICE = re.compile(r"^PRECIO_EXTRA\s+([a-z0-9_-]{1,128})\s+([0-9]{1,7}(?:[.,][0-9]{1,2})?)$", re.IGNORECASE)


class WhatsAppMessageHandler:
    """Keep staff commands local; customers reach the guarded conversation flow."""

    def __init__(
        self, conversation: ConversationEngine, auth: PersonnelAuth, orders: OrderWorkflow,
        *, tickets: TicketReviewService | None = None, cloud: WhatsAppCloudClient | None = None,
        operations=None,
    ):
        self.conversation = conversation
        self.auth = auth
        self.orders = orders
        self.tickets = tickets
        self.cloud = cloud
        self.operations = operations

    async def handle(self, phone: str, text: str, *, event_id: str | None = None) -> ConversationReply:
        principal = self.auth.principal(phone)
        if principal is not None:
            return self._staff_text(phone, text, event_id=event_id)
        if _STAFF_PREFIX.match(text.strip()):
            return ConversationReply(
                "Para realizar tareas de personal, inicia sesión con /acceso. Si necesitas ayuda con el menú, dime qué buscas.",
                "needs_auth", "staff_login_required",
            )
        return await self.conversation.handle(phone, text)

    async def handle_media(self, phone: str, media_id: str) -> ConversationReply:
        principal = self.auth.principal(phone)
        if principal is None:
            return ConversationReply(
                "No puedo revisar esa imagen desde este número. Si necesitas ayuda, consulta con el personal.",
                "needs_auth", "media_staff_only",
            )
        if self.tickets is None or self.cloud is None:
            return ConversationReply("La revisión de imágenes no está disponible en este momento.", "paused", "media_unavailable")
        try:
            image, mime = await self.cloud.download_media(media_id)
            proposal = await asyncio.to_thread(
                self.tickets.receive_image, phone, image, declared_mime=mime
            )
        except (ValueError, H2StorageError, RuntimeError):
            return ConversationReply(
                "No pude leer la imagen con seguridad. Intenta con una foto más clara o revísala manualmente.",
                "needs_input", "ticket_image_unreadable",
            )
        return ConversationReply(proposal["message"], "needs_review", "pos_ticket_review")

    def _staff_text(self, phone: str, text: str, *, event_id: str | None = None) -> ConversationReply:
        value = text.strip()
        operations_help = value.upper() in {"AYUDA", "/AYUDA"}
        if operations_help:
            principal = self.auth.principal(phone)
            suffix = " Los reportes y ajustes disponibles dependen de tu rol."
            if principal and principal.role in {"admin", "manager"}:
                suffix = " Usa INVENTARIO para consultar insumos y AJUSTAR_EXISTENCIA <id> <+/-cantidad> <motivo> para registrar un ajuste."
            if principal and principal.role == "admin":
                suffix += " Usa /ALTA <número> <cajero|gerente|admin> para invitar y /ROL <número> <rol> para cambiar un rol. AUDITORIA muestra acciones recientes. Para precios provisionales: PRECIO <id_producto> <mediano|grande|presentacion_unica> <MXN> o PRECIO_EXTRA <id_extra> <MXN>."
            return ConversationReply(
                "Comandos: /acceso; ACEPTAR, PASAR, PREPARAR, LISTO, ENTREGAR o VER con folio; REVISAR con folio, CORREGIR con ticket y CONFIRMAR con ticket." + suffix,
                "completed", "staff_help",
            )

        if value.upper() == "INVENTARIO":
            if self.operations is None:
                return ConversationReply("La consulta de existencias no está disponible.", "paused", "inventory_unavailable")
            try:
                snapshot = self.operations.inventory(phone)
            except (AuthorizationError, H2StorageError, ValueError):
                return _staff_denied()
            lines = ["Existencias de demostración (no son inventario real):"]
            lines.extend(f"• {item['name']}: {item['on_hand']} {item['unit']} ({item['item_id']})" for item in snapshot.values())
            return ConversationReply("\n".join(lines), "completed", "inventory_report")

        adjustment = _INVENTORY_ADJUSTMENT.fullmatch(value)
        if adjustment:
            if self.operations is None:
                return ConversationReply("Los ajustes no están disponibles.", "paused", "inventory_unavailable")
            command_id = event_id or f"local:{uuid.uuid4().hex}"
            try:
                item = self.operations.adjust_inventory(
                    phone, adjustment.group(1), int(adjustment.group(2)), adjustment.group(3).strip(), command_id,
                )
            except (AuthorizationError, H2StorageError, ValueError):
                return _staff_denied()
            return ConversationReply(
                f"Ajuste registrado en la simulación. {item['name']}: {item['on_hand']} {item['unit']} en existencia.",
                "completed", "inventory_adjusted",
            )

        variant_price = _VARIANT_PRICE.fullmatch(value)
        extra_price = _EXTRA_PRICE.fullmatch(value)
        if variant_price or extra_price:
            if self.operations is None:
                return ConversationReply("Los cambios de precio no están disponibles.", "paused", "catalog_unavailable")
            try:
                if variant_price:
                    product_id, size, raw_price = variant_price.groups()
                    cents = _parse_price_cents(raw_price)
                    self.operations.set_variant_price(phone, product_id, size.casefold(), cents)
                    label = f"{product_id} {size.casefold()}"
                else:
                    extra_id, raw_price = extra_price.groups()
                    cents = _parse_price_cents(raw_price)
                    self.operations.set_extra_price(phone, extra_id, cents)
                    label = extra_id
            except (AuthorizationError, H2StorageError, ValueError, InvalidOperation):
                return _staff_denied()
            return ConversationReply(
                f"Guardé el precio de {label} en el catálogo de demostración como provisional: ${cents / 100:,.2f} MXN.",
                "completed", "catalog_price_changed",
            )

        if value.upper() == "AUDITORIA":
            if self.operations is None:
                return ConversationReply("La revisión de acciones no está disponible.", "paused", "audit_unavailable")
            try:
                rows = self.operations.audit(phone, limit=10)
            except (AuthorizationError, H2StorageError, ValueError):
                return _staff_denied()
            if not rows:
                return ConversationReply("Todavía no hay acciones registradas.", "completed", "audit_report")
            lines = ["Últimas acciones registradas:"]
            for row in rows:
                target = row["target"]
                if re.fullmatch(r"\+[1-9][0-9]{7,14}", target):
                    target = f"personal ••••{target[-4:]}"
                lines.append(f"• {row['created_at']} — {row['action']} — {target} — {row['result']}")
            return ConversationReply("\n".join(lines), "completed", "audit_report")

        role_change = _STAFF_ROLE.fullmatch(value)
        if role_change:
            role = {"admin": "admin", "administrador": "admin", "gerente": "manager", "cajero": "cashier"}[
                role_change.group(2).casefold()
            ]
            try:
                self.auth.change_staff_role(phone, role_change.group(1), role)
            except (AuthorizationError, H2StorageError, ValueError):
                return _staff_denied()
            return ConversationReply("Listo, actualicé el rol. La persona deberá iniciar sesión de nuevo.", "completed", "staff_role_changed")

        invite = _STAFF_INVITE.fullmatch(value)
        if invite:
            role = {"admin": "admin", "administrador": "admin", "gerente": "manager", "cajero": "cashier"}[invite.group(2).casefold()]
            try:
                self.auth.invite_staff(phone, invite.group(1), role)
            except (AuthorizationError, H2StorageError, ValueError):
                return _staff_denied()
            return ConversationReply(
                "Listo, registré el número. Pídele que escriba /acceso desde su WhatsApp para iniciar sesión.",
                "completed", "staff_invited",
            )

        command = _ORDER_COMMAND.fullmatch(value)
        if command:
            verb, folio = command.group(1).upper(), command.group(2).upper()
            try:
                if verb in {"ACEPTAR", "ACEPTA"}:
                    self.orders.accept(phone, folio)
                    answer = f"Pedido {folio} aceptado; el personal y el cliente recibirán el aviso."
                elif verb == "PASAR":
                    assignment = self.orders.pass_to_next(phone, folio)
                    answer = f"Pedido {folio} enviado al siguiente responsable."
                    if assignment.get("status") == "expired":
                        answer = f"El pedido {folio} venció sin registrar venta ni descontar existencias."
                elif verb == "RECHAZAR":
                    self.orders.reject(phone, folio)
                    answer = f"Pedido {folio} rechazado. No se descontó existencia."
                elif verb in {"PREPARAR", "EN PREPARACIÓN", "EN PREPARACION"}:
                    self.orders.advance(phone, folio, "preparing")
                    answer = f"Pedido {folio} marcado en preparación."
                elif verb == "LISTO":
                    self.orders.advance(phone, folio, "ready")
                    answer = f"Pedido {folio} marcado listo para recoger."
                elif verb in {"ENTREGAR", "ENTREGADO"}:
                    self.orders.advance(phone, folio, "delivered")
                    answer = f"Pedido {folio} marcado como entregado."
                else:
                    order = self.orders.staff_order(phone, folio)
                    answer = f"Pedido {folio}: {order['status']}. Total: ${order['total_cents'] / 100:,.2f} MXN."
            except (AuthorizationError, H2StorageError, ValueError):
                return _staff_denied()
            return ConversationReply(answer, "completed", "staff_order_action")

        ticket_command = _TICKET_COMMAND.fullmatch(value)
        correction = _TICKET_CORRECTION.fullmatch(value)
        if ticket_command or correction:
            if self.tickets is None:
                return ConversationReply("La revisión de tickets no está disponible.", "paused", "ticket_unavailable")
            try:
                if correction:
                    result = self.tickets.correct_from_text(phone, correction.group(1).upper(), correction.group(2))
                    answer = result["message"]
                    intent = "ticket_corrected"
                else:
                    verb, ticket_id = ticket_command.group(1).upper(), ticket_command.group(2).upper()
                    if verb == "REVISAR":
                        result = self.tickets.review(phone, ticket_id)
                        answer = self.tickets.format_proposal(ticket_id, result["lines"])
                        intent = "ticket_reviewed"
                    elif verb == "CONFIRMAR":
                        result = self.tickets.confirm(phone, ticket_id)
                        answer = result["message"]
                        intent = "ticket_confirmed"
                    else:
                        self.tickets.reject(phone, ticket_id)
                        answer = f"Ticket {ticket_id} rechazado; no se descontó existencia."
                        intent = "ticket_rejected"
            except (AuthorizationError, H2StorageError, ValueError):
                return _staff_denied()
            return ConversationReply(answer, "completed", intent)

        return ConversationReply(
            "No reconocí esa instrucción. Escribe AYUDA para ver las opciones disponibles para tu rol.",
            "needs_input", "staff_help",
        )


def _staff_denied() -> ConversationReply:
    return ConversationReply(
        "No pude realizar esa acción. Revisa el folio, tu permiso y el estado actual del pedido.",
        "denied", "staff_action_denied",
    )


def _parse_price_cents(value: str) -> int:
    try:
        amount = Decimal(value.replace(",", "."))
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError("El precio no es válido.") from exc
    if not amount.is_finite() or amount < 0 or amount > Decimal(20_000_000):
        raise ValueError("El precio no es válido.")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ValueError("El precio admite hasta dos decimales.")
    return int(cents)
