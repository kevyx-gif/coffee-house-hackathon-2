"""Minimal Groq-compatible tool router; outputs are never trusted as facts."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_MODEL = "qwen/qwen3.8-27b"
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_menu",
            "description": (
                "Listar o buscar nombres, categorías y extras del menú. Úsala cuando pidan opciones o nombres. "
                "No la uses para saber si una bebida concreta hay, queda, sigue disponible o se agotó; usa check_availability."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 128}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": (
                "Consultar precio, tamaño y existencia de una bebida concreta en los datos del menú. "
                "Úsala para cuánto cuesta, a cómo está, cuánto sale, y preguntas de existencia como hay, tienen, queda, "
                "sigue habiendo, está disponible o se agotó. Ejemplo: '¿Hay té verde mango helado?' usa esta herramienta. "
                "Si solo piden listar opciones, usa search_menu; si no indican tamaño, usa null."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_name": {
                        "type": "string", "minLength": 1, "maxLength": 128,
                        "description": "Usa el nombre del menú cuando reconozcas con claridad la bebida, corrigiendo una errata obvia; por ejemplo, frape de oreo significa Frappé Oreo. No elijas entre productos parecidos. Si no estás seguro, conserva lo escrito.",
                    },
                    "size": {
                        "type": ["string", "null"], "maxLength": 64,
                        "description": "Usa solo mediano, grande o null. Normaliza formas claras como medianito/medianita/mediani a mediano y grand a grande. Si no indicó tamaño o no es claro, usa null.",
                    },
                    "modifiers": {
                        "type": "array", "maxItems": 3,
                        "description": "Solo extras pedidos explícitamente. Usa el nombre del menú si es claro (por ejemplo, lechita de avena equivale a Leche de Avena); si el extra no está claro, conserva la expresión escrita para que el servidor pregunte. Nunca incluyas caliente, frío, hot o iced: la temperatura se interpreta del mensaje original.",
                        "items": {"type": "string", "maxLength": 128},
                    },
                },
                "required": ["product_name", "size", "modifiers"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prepare_order",
            "description": "Proponer un borrador de pedido a partir de los productos, tamaños, cantidades y extras que el cliente pidió explícitamente. Normaliza solo erratas o diminutivos claros; no completa datos omitidos ni confirma el pedido. No envía nada al personal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array", "minItems": 1, "maxItems": 10,
                        "items": {
                            "type": "object",
                            "properties": {
                                "product_name": {
                                    "type": "string", "minLength": 1, "maxLength": 128,
                                    "description": "Usa el nombre del menú cuando reconozcas con claridad la bebida, corrigiendo una errata obvia; por ejemplo, frape de oreo significa Frappé Oreo. No elijas entre productos parecidos. Si no estás seguro, conserva lo escrito.",
                                },
                                "size": {
                                    "type": ["string", "null"], "maxLength": 64,
                                    "description": "Usa solo mediano, grande o null. Normaliza formas claras como medianito/medianita/mediani a mediano y grand a grande. Si no indicó tamaño o no es claro, usa null.",
                                },
                                "quantity": {"type": "integer", "minimum": 1, "maximum": 50},
                                "modifiers": {
                                    "type": "array", "maxItems": 3,
                                    "description": "Solo extras pedidos explícitamente. Usa el nombre del menú si es claro (por ejemplo, lechita de avena equivale a Leche de Avena); si el extra no está claro, conserva la expresión escrita para que el servidor pregunte. Nunca incluyas caliente, frío, hot o iced: la temperatura se toma del mensaje original.",
                                    "items": {"type": "string", "maxLength": 128},
                                },
                            },
                            "required": ["product_name", "size", "quantity", "modifiers"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["items"],
                "additionalProperties": False,
            },
        },
    },
]
ALLOWED_TOOLS = {item["function"]["name"] for item in TOOLS}
SYSTEM_PROMPT = (
    "Eres un enrutador de mensajes de una cafetería en México. No redactes respuestas ni inventes datos. "
    "Elige una sola herramienta cuando la intención esté clara. search_menu es solo para pedir una lista de opciones, "
    "nombres, categorías o extras. check_availability es para precio o existencia de una bebida nombrada, incluso si "
    "lo dicen coloquialmente: '¿cuánto sale?', '¿hay?', '¿tienen?', '¿queda?', '¿sigue habiendo?' o '¿se agotó?'. "
    "Por ejemplo, '¿Hay té verde mango helado?' usa check_availability, no search_menu. prepare_order solo se usa "
    "cuando la persona pide hacer un pedido. En ambas herramientas usa el nombre del menú si identificas claramente "
    "el producto y normaliza erratas obvias sin elegir entre bebidas parecidas. Por ejemplo, 'frape de oreo' es "
    "'Frappé Oreo'. Usa mediano o grande para tamaños claros; medianito, medianita o mediani significan mediano, "
    "y grand significa grande. Si el tamaño no se mencionó "
    "o no se entiende, usa null y no lo adivines. Conserva solo productos, cantidades y extras pedidos explícitamente. "
    "Usa el nombre del menú para un extra que reconoces claramente: por ejemplo, 'lechita de avena' corresponde a "
    "'Leche de Avena'. Si el extra no se reconoce con certeza, conserva lo que escribió el cliente para que el servidor "
    "lo consulte. modifiers contiene solo extras, nunca palabras de temperatura como caliente, frío, hot o iced; "
    "la temperatura se interpreta del mensaje original. Si la petición es ambigua o falta "
    "un producto, no completes los datos por tu cuenta. prepare_order solo prepara una propuesta: el servidor "
    "revisa los datos y pide confirmación; nunca confirma, envía, cobra ni descuenta existencias. No propongas "
    "acciones de personal, autenticación, cambios de inventario, confirmaciones, estado de pedidos ni SQL."
)


@dataclass(frozen=True, slots=True)
class GroqSettings:
    api_key: str = field(repr=False)
    model: str = DEFAULT_MODEL
    timeout_seconds: float = 12.0

    @classmethod
    def from_env(cls) -> GroqSettings:
        return cls(
            api_key=os.getenv("GROQ_API_KEY", ""),
            model=os.getenv("GROQ_MODEL_ID", DEFAULT_MODEL),
        )


@dataclass(frozen=True, slots=True)
class ToolRoute:
    name: str
    arguments: dict[str, Any]
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    remaining_tokens: str | None
    remaining_requests: str | None


class ModelUnavailable(RuntimeError):
    def __init__(self, code: str, *, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after


class GroqToolRouter:
    """Calls one approved model for one closed-list tool proposal, without logging content."""

    def __init__(self, settings: GroqSettings, *, client: httpx.AsyncClient | None = None):
        if not settings.api_key:
            raise ValueError("GROQ_API_KEY no está configurada.")
        if settings.model != DEFAULT_MODEL:
            raise ValueError("El identificador del modelo no coincide con la evaluación aprobada.")
        if not 1 <= settings.timeout_seconds <= 30:
            raise ValueError("El tiempo de espera del modelo no es válido.")
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.timeout_seconds, connect=4.0), trust_env=False
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def route(self, user_text: str) -> ToolRoute | None:
        if not isinstance(user_text, str) or not user_text.strip() or len(user_text) > 2000:
            raise ValueError("La consulta no cumple los límites.")
        started = time.perf_counter()
        try:
            response = await self._client.post(
                GROQ_CHAT_URL,
                headers={"Authorization": f"Bearer {self._settings.api_key}"},
                json={
                    "model": self._settings.model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_text},
                    ],
                    "tools": TOOLS,
                    "tool_choice": "auto",
                    "parallel_tool_calls": False,
                    "temperature": 0,
                    "max_tokens": 512,
                },
            )
        except httpx.TimeoutException:
            raise ModelUnavailable("timeout") from None
        except httpx.HTTPError:
            raise ModelUnavailable("network_error") from None
        elapsed = round((time.perf_counter() - started) * 1000)
        if response.status_code == 429:
            raise ModelUnavailable(
                "rate_limited", status_code=429,
                retry_after=_retry_after(response.headers.get("retry-after")),
            )
        if response.status_code >= 500:
            raise ModelUnavailable("provider_error", status_code=response.status_code)
        if not 200 <= response.status_code < 300:
            raise ModelUnavailable("request_rejected", status_code=response.status_code)
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            raise ModelUnavailable("invalid_response") from None
        try:
            message = payload["choices"][0]["message"]
            calls = message.get("tool_calls")
        except (KeyError, IndexError, TypeError):
            raise ModelUnavailable("invalid_response") from None
        if not calls:
            # Free-form model content is deliberately not forwarded to the customer.
            return None
        if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
            raise ModelUnavailable("invalid_tool_call")
        function = calls[0].get("function")
        if not isinstance(function, dict) or function.get("name") not in ALLOWED_TOOLS:
            raise ModelUnavailable("unknown_tool")
        try:
            arguments = json.loads(function.get("arguments", ""))
        except (json.JSONDecodeError, TypeError):
            raise ModelUnavailable("invalid_arguments") from None
        if not isinstance(arguments, dict):
            raise ModelUnavailable("invalid_arguments")
        usage = _usage(payload)
        return ToolRoute(
            name=function["name"], arguments=arguments, latency_ms=elapsed,
            input_tokens=usage[0], output_tokens=usage[1],
            remaining_tokens=response.headers.get("x-ratelimit-remaining-tokens"),
            remaining_requests=response.headers.get("x-ratelimit-remaining-requests"),
        )


def _usage(payload: Any) -> tuple[int | None, int | None]:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None, None
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    return (
        prompt if type(prompt) is int and prompt >= 0 else None,
        completion if type(completion) is int and completion >= 0 else None,
    )


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        return None
    return min(max(seconds, 0.0), 120.0)
