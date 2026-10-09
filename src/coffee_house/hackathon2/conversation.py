"""Fail-closed customer conversation: model routing, verified facts, no writes."""
from __future__ import annotations

import asyncio
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol

from .auth import PersonnelAuth
from .catalog import SIZE_ALIASES, CatalogService
from .model import ModelUnavailable
from .orders import OrderInventoryShortage, OrderUnavailable, OrderWorkflow
from .storage import H2StorageError, InventoryShortage

PHONE_PATTERN = re.compile(r"^\+[1-9][0-9]{7,14}$")
OTP_PATTERN = re.compile(r"^[0-9]{8}$")
HOLD_REPLY = "Por ahora no puedo confirmar esa información. Puedes revisar el menú o consultarlo con el personal."
PAUSED_REPLY = "En este momento no puedo consultar el menú. Intenta de nuevo en un momento o pregúntale al personal."
WELCOME = "¡Hola! Puedo orientarte con el menú y con información de la cafetería. ¿Qué te gustaría consultar?"


class ToolModel(Protocol):
    async def route(self, user_text: str): ...


class SafetyGuard(Protocol):
    def classify(self, user_text: str, assistant_text: str | None = None): ...


class OtpSender(Protocol):
    async def __call__(self, phone: str, text: str) -> object: ...


class OrderNeedsClarification(ValueError):
    """The customer must supply an order detail before a draft can be priced."""


@dataclass(frozen=True, slots=True)
class ConversationReply:
    text: str
    state: str
    intent: str


class ConversationEngine:
    """The model may route to two read-only tools; all customer facts stay local."""

    def __init__(
        self,
        catalog: CatalogService,
        guard: SafetyGuard,
        model: ToolModel,
        *,
        auth: PersonnelAuth | None = None,
        orders: OrderWorkflow | None = None,
        otp_sender: OtpSender | None = None,
        allow_external_text: bool | None = None,
    ):
        self._catalog = catalog
        self._guard = guard
        self._model = model
        self._auth = auth
        self._orders = orders
        self._otp_sender = otp_sender
        if allow_external_text is not None and type(allow_external_text) is not bool:
            raise ValueError("La autorización de texto externo debe ser booleana.")
        self._allow_external_text = (
            os.getenv("H2_ALLOW_EXTERNAL_TEXT", "false").strip().casefold() == "true"
            if allow_external_text is None else allow_external_text
        )

    async def handle(self, phone: str, text: str) -> ConversationReply:
        if not isinstance(phone, str) or not PHONE_PATTERN.fullmatch(phone):
            return ConversationReply(HOLD_REPLY, "paused", "invalid_sender")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            return ConversationReply("¿Me escribes de nuevo qué te gustaría consultar?", "needs_input", "invalid_message")
        message = text.strip()

        # Personnel login and one-time codes remain entirely local and never reach
        # the guard or the remote model.
        local_auth = await self._staff_auth(phone, message)
        if local_auth is not None:
            return local_auth

        order_control = self._handle_order_control(phone, message)
        if order_control is not None:
            return order_control

        if not self._allow_external_text:
            return ConversationReply(PAUSED_REPLY, "paused", "external_text_disabled")

        incoming = await _classify_safely_async(self._guard, message, None)
        if not _is_safe(incoming):
            return ConversationReply(HOLD_REPLY, "paused", "input_guard")

        try:
            route = await self._model.route(message)
        except ModelUnavailable:
            return ConversationReply(PAUSED_REPLY, "paused", "model_unavailable")
        except Exception:  # noqa: BLE001 - provider/parsing failures must pause without side effects
            return ConversationReply(PAUSED_REPLY, "paused", "model_unavailable")

        if route is None:
            reply = "Por el momento no tengo conocimiento de eso, pero ¿hay algo más en lo que te pueda ayudar?"
            intent = "unknown_or_ambiguous"
        else:
            try:
                reply, intent = self._execute_read_tool(route.name, route.arguments, phone, message)
            except OrderNeedsClarification as exc:
                return ConversationReply(str(exc), "needs_input", "order_needs_clarification")
            except OrderInventoryShortage as exc:
                if self._orders is None:
                    return ConversationReply(
                        "Lo siento, esa cantidad ya no está disponible. No se envió el pedido ni se descontó existencia.",
                        "needs_input", "order_inventory_changed",
                    )
                return ConversationReply(
                    self._orders.inventory_shortage_message(exc),
                    "needs_input", "order_inventory_changed",
                )
            except InventoryShortage:
                return ConversationReply(
                    "Lo siento, esa cantidad ya no está disponible. No envié el pedido ni desconté existencias. ¿Quieres que revisemos otra opción?",
                    "needs_input", "order_inventory_changed",
                )
            except OrderUnavailable as exc:
                return ConversationReply(str(exc), "needs_input", "order_unavailable")
            except H2StorageError:
                return ConversationReply(
                    "No pude guardar el pedido con seguridad. No se descontó existencia; inténtalo de nuevo en un momento.",
                    "paused", "order_storage_error",
                )
            except (TypeError, ValueError, KeyError):
                return ConversationReply(HOLD_REPLY, "paused", "invalid_tool_arguments")

        outgoing = await _classify_safely_async(self._guard, message, reply)
        if not _is_safe(outgoing):
            return ConversationReply(HOLD_REPLY, "paused", "output_guard")
        state = intent if intent in {
            "confirmation_required", "pending_staff", "not_submitted", "reconfirmation_required",
            "already_processed", "needs_clarification", "paused",
        } else "answered"
        return ConversationReply(reply, state, intent)

    async def _staff_auth(self, phone: str, message: str) -> ConversationReply | None:
        normalized = _normalize(message)
        if normalized in {"/acceso", "acceso personal", "iniciar sesion", "iniciar acceso"}:
            if self._auth is not None and self._otp_sender is not None:
                try:
                    await self._auth.request_code(phone, self._otp_sender)
                except Exception:  # noqa: BLE001, S110 - preserve generic answer; never log sender details
                    pass
            # Same response whether or not the number is registered.
            return ConversationReply(
                "Si este número está registrado como personal, recibirá un código de acceso en WhatsApp.",
                "answered", "staff_login_requested",
            )
        if self._auth is None or not OTP_PATTERN.fullmatch(message) or not self._auth.has_pending_code(phone):
            return None
        if self._auth.verify_code(phone, message):
            return ConversationReply("Acceso confirmado. Ya puedes continuar con tus tareas de personal.", "answered", "staff_login_verified")
        return ConversationReply("No pude validar ese código. Revisa que sea el más reciente y que no hayan pasado 5 minutos.", "needs_input", "staff_login_invalid")

    def _handle_order_control(self, phone: str, message: str) -> ConversationReply | None:
        if self._orders is None:
            return None
        if _is_order_status_question(message):
            match = re.search(r"\bCH-[A-Z0-9-]{3,32}\b", message, re.IGNORECASE)
            result = self._orders.status(phone, match.group(0).upper() if match else None)
            return ConversationReply(result.text, result.state, "order_status")
        state = self._orders.store.conversation(phone) or {}
        if not isinstance(state.get("pending_order_confirmation"), dict):
            return None
        result = self._orders.respond_to_confirmation(phone, message)
        if result.state == "edit_requested":
            return None
        return ConversationReply(result.text, result.state, "order_confirmation")

    def _execute_read_tool(
        self, name: str, arguments: dict, phone: str, user_message: str = ""
    ) -> tuple[str, str]:
        if not isinstance(arguments, dict):
            raise TypeError("Argumentos inválidos.")
        if name == "search_menu":
            if set(arguments) != {"query"} or not isinstance(arguments["query"], str):
                raise ValueError("Argumentos inválidos.")
            query = arguments["query"].strip()
            if not query or len(query) > 128:
                raise ValueError("Argumentos inválidos.")
            return self._catalog.format_menu_results(self._catalog.search(query)), "menu_search"
        if name == "check_availability":
            if set(arguments) != {"product_name", "size", "modifiers"}:
                raise ValueError("Argumentos inválidos.")
            product_name, size, modifiers = (
                arguments["product_name"], arguments["size"], arguments["modifiers"]
            )
            if not isinstance(product_name, str) or not product_name.strip() or len(product_name) > 128:
                raise ValueError("Argumentos inválidos.")
            if size is not None and (not isinstance(size, str) or len(size) > 64):
                raise ValueError("Argumentos inválidos.")
            if not isinstance(modifiers, list) or len(modifiers) > 3 or any(
                not isinstance(value, str) or len(value) > 128 for value in modifiers
            ):
                raise ValueError("Argumentos inválidos.")
            product = self._catalog.resolve_explicit_alias_mention(
                user_message, model_query=product_name
            )
            candidates = []
            if product is None:
                product, candidates = self._catalog.resolve_product(product_name)
            product, temperature_question = _select_temperature_variant(
                product, candidates, self._catalog, user_message
            )
            if temperature_question:
                return temperature_question, "needs_clarification"
            if product is None:
                if len(candidates) > 1:
                    names = ", ".join(item["name"] for item in candidates[:5])
                    return f"¿Cuál de estas bebidas buscas: {names}?", "clarify_product"
                return "No encontré esa bebida en el menú. ¿Me dices cuál te interesa?", "unknown_product"
            extras, error = self._catalog.resolve_extras(
                _remove_explicit_temperature_from_extras(modifiers, user_message)
            )
            if error:
                return error, "clarify_modifier"
            return self._catalog.check_availability(product, size, extras or []), "availability"
        if name == "prepare_order":
            if self._orders is None or set(arguments) != {"items"}:
                raise ValueError("La preparación de pedidos no está disponible.")
            try:
                items = _order_items(arguments["items"], self._catalog, user_message=user_message)
            except OrderNeedsClarification:
                raise
            except ValueError as exc:
                raise OrderNeedsClarification(str(exc)) from exc
            result = self._orders.prepare_confirmation(phone, items)
            return result.text, result.state
        raise ValueError("Herramienta no permitida.")


def _order_items(value: object, catalog: CatalogService, *, user_message: str = "") -> list[dict]:
    if not isinstance(value, list) or not 1 <= len(value) <= 10:
                raise OrderNeedsClarification("No pude identificar qué productos quieres pedir.")
    multiple_items = len(value) > 1
    items = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"product_name", "size", "quantity", "modifiers"}:
            raise OrderNeedsClarification("Necesito confirmar producto, tamaño, cantidad y extras.")
        product_name, size, quantity, modifier_names = (
            entry["product_name"], entry["size"], entry["quantity"], entry["modifiers"]
        )
        if (
            not isinstance(product_name, str) or not product_name.strip() or len(product_name) > 128
            or (size is not None and (not isinstance(size, str) or len(size) > 64))
            or type(quantity) is not int or not 1 <= quantity <= 50
            or not isinstance(modifier_names, list) or len(modifier_names) > 3
            or any(not isinstance(name, str) or not name.strip() or len(name) > 128 for name in modifier_names)
        ):
            raise OrderNeedsClarification("No pude verificar los datos de uno de los productos.")
        item_message = _message_context_for_order_item(user_message, product_name, multiple_items)
        product = catalog.resolve_explicit_alias_mention(
            item_message, model_query=product_name
        )
        candidates = []
        if product is None:
            product, candidates = catalog.resolve_product(product_name)
        product, temperature_question = _select_temperature_variant(
            product, candidates, catalog, item_message
        )
        if temperature_question:
            raise OrderNeedsClarification(temperature_question)
        if product is None:
            if len(candidates) > 1:
                options = ", ".join(candidate["name"] for candidate in candidates[:5])
                raise OrderNeedsClarification(f"¿Cuál de estas bebidas quieres pedir: {options}?")
            raise OrderNeedsClarification("No encontré esa bebida en el menú. ¿Me dices cuál te interesa?")
        variants = product.get("variants", [])
        if size is None or not size.strip():
            if len(variants) != 1:
                options = " o ".join(str(item.get("size", "")) for item in variants[:4])
                raise OrderNeedsClarification(f"¿Qué tamaño prefieres para {product['name']}: {options}?")
            variant = variants[0].get("size")
        else:
            size_key = _normalize(size)
            canonical_size = SIZE_ALIASES.get(size_key)
            selected = [item for item in variants if item.get("size") == canonical_size]
            if len(selected) != 1:
                raise OrderNeedsClarification(f"No encontré ese tamaño para {product['name']}. Revisa el menú y dime cuál prefieres.")
            variant = selected[0].get("size")
        extras, problem = catalog.resolve_extras(
            _remove_explicit_temperature_from_extras(modifier_names, item_message)
        )
        if problem or extras is None:
            raise OrderNeedsClarification(problem or "No pude validar los extras.")
        items.append({
            "product_id": product["id"], "variant": variant, "quantity": quantity,
            "modifier_ids": [extra["id"] for extra in extras],
        })
    return items


def _is_order_status_question(message: str) -> bool:
    normalized = _normalize(message)
    has_order = any(word in normalized.split() for word in ("pedido", "orden", "folio"))
    has_status = any(term in normalized for term in ("estado", "estatus", "como va", "que paso", "ya esta listo", "consulta"))
    has_folio = bool(re.search(r"\bCH-[A-Z0-9-]{3,32}\b", message, re.IGNORECASE))
    return has_order and (has_status or has_folio)


def _is_safe(decision: object) -> bool:
    return getattr(decision, "label", None) == "Safe" and getattr(decision, "allowed", None) is True


def _select_temperature_variant(
    product: dict | None,
    candidates: list[dict],
    catalog: CatalogService,
    user_message: str,
) -> tuple[dict | None, str | None]:
    """Never choose between matching hot/cold menu items unless the user said which."""
    requested_group = _requested_temperature_group(user_message)
    paired: list[dict] = []
    if product is not None:
        paired = [product, *_temperature_counterparts(product, catalog)]
    elif len(candidates) == 2:
        candidate_ids = {item.get("id") for item in candidates}
        for candidate in candidates:
            full_product = catalog.product_by_id(candidate.get("id", ""))
            if full_product is None:
                continue
            counterparts = _temperature_counterparts(full_product, catalog)
            if len(counterparts) == 1 and counterparts[0].get("id") in candidate_ids:
                paired = [full_product, counterparts[0]]
                break

    if not paired:
        return product, None
    if requested_group is None:
        hot = next((item for item in paired if item.get("extra_group") == "calientes"), None)
        cold = next((item for item in paired if item.get("extra_group") == "frias"), None)
        if hot is not None and cold is not None:
            return None, f"¿Lo quieres caliente ({hot['name']}) o frío ({cold['name']})?"
        return product, None
    selected = [item for item in paired if item.get("extra_group") == requested_group]
    if len(selected) == 1:
        return selected[0], None
    return product, None


def _temperature_counterparts(product: dict, catalog: CatalogService) -> list[dict]:
    group = product.get("extra_group")
    if group not in {"calientes", "frias"}:
        return []
    core = _temperature_neutral_name(str(product.get("name", "")))
    if not core:
        return []
    return [
        candidate for candidate in catalog.products
        if candidate.get("id") != product.get("id")
        and candidate.get("extra_group") in {"calientes", "frias"}
        and candidate.get("extra_group") != group
        and _temperature_neutral_name(str(candidate.get("name", ""))) == core
    ]


def _temperature_neutral_name(name: str) -> str:
    normalized = _normalize(name)
    normalized = re.sub(r"^(?:iced|cold|hot|caliente|frio|fria)\s+", "", normalized)
    normalized = re.sub(r"\s+(?:hot|caliente|frio|fria)$", "", normalized)
    return normalized.strip()


def _requested_temperature_group(message: str) -> str | None:
    normalized = _normalize(message)
    hot = bool(re.search(r"\b(?:caliente|calientes|calentito|calentita|hot)\b", normalized))
    cold = bool(re.search(r"\b(?:frio|fria|frios|frias|helado|helada|iced|cold)\b", normalized))
    if hot == cold:
        return None
    return "calientes" if hot else "frias"


def _message_context_for_order_item(message: str, product_name: str, multiple_items: bool) -> str:
    if not multiple_items:
        return message
    normalized = _normalize(message)
    anchor = _normalize(product_name)
    if not anchor:
        return ""
    segments = re.split(r"\b(?:y|and)\b|[,;]+", normalized)
    matches = [segment for segment in segments if anchor in segment]
    return matches[0] if len(matches) == 1 else ""


def _remove_explicit_temperature_from_extras(modifiers: list[str], message: str) -> list[str]:
    """Drop temperature labels misrouted as extras only when the customer said them."""
    normalized_message = _normalize(message)
    temperature_words = {
        "caliente", "calientes", "calentito", "calentita", "hot",
        "frio", "fria", "frios", "frias", "helado", "helada", "iced", "cold",
    }
    explicit_words = {
        word for word in temperature_words
        if re.search(rf"\b{re.escape(word)}\b", normalized_message)
    }
    if not explicit_words:
        return modifiers
    return [
        modifier for modifier in modifiers
        if _normalize(modifier) not in explicit_words
    ]


def _classify_safely(guard: SafetyGuard, user_text: str, assistant_text: str | None):
    try:
        return guard.classify(user_text, assistant_text)
    except Exception:  # noqa: BLE001 - any classifier failure is a closed gate
        return None


async def _classify_safely_async(guard: SafetyGuard, user_text: str, assistant_text: str | None):
    classify_async = getattr(guard, "classify_async", None)
    try:
        if callable(classify_async):
            return await classify_async(user_text, assistant_text)
        return await asyncio.to_thread(_classify_safely, guard, user_text, assistant_text)
    except Exception:  # noqa: BLE001 - any classifier failure is a closed gate
        return None


def _normalize(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.casefold())
    return " ".join("".join(char for char in folded if not unicodedata.combining(char)).split())
