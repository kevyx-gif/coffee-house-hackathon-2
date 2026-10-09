"""Secure WhatsApp Cloud API boundary for the Hackathon 2 demo."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from .storage import H2StorageError, H2Store

_LOG = logging.getLogger(__name__)

MAX_WEBHOOK_BYTES = 65_536
MAX_EVENTS = 100
_PHONE = re.compile(r"^[1-9][0-9]{7,14}$")
_MEDIA_ID = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_API_VERSION = re.compile(r"^v[0-9]{1,2}\.[0-9]{1,2}$")
_SIGNATURE = re.compile(r"^sha256=([0-9a-f]{64})$")


@dataclass(frozen=True, slots=True)
class WhatsAppSettings:
    verify_token: str = field(repr=False)
    app_secret: str = field(repr=False)
    access_token: str = field(repr=False)
    phone_number_id: str
    graph_api_version: str = "v23.0"

    def __post_init__(self) -> None:
        if not self.phone_number_id or len(self.phone_number_id) > 64 or not self.phone_number_id.isascii():
            raise ValueError("PHONE_NUMBER_ID no válido.")
        if not _API_VERSION.fullmatch(self.graph_api_version):
            raise ValueError("META_GRAPH_API_VERSION debe tener formato vN.N.")

    @classmethod
    def from_env(cls) -> WhatsAppSettings:
        return cls(
            verify_token=os.getenv("WHATSAPP_VERIFY_TOKEN", ""),
            app_secret=os.getenv("META_APP_SECRET", ""),
            access_token=os.getenv("WHATSAPP_ACCESS_TOKEN", ""),
            phone_number_id=os.getenv("PHONE_NUMBER_ID", ""),
            graph_api_version=os.getenv("META_GRAPH_API_VERSION", "v23.0"),
        )


class MetaRejected(RuntimeError):
    """Meta returned a definitive non-2xx response; retain only numeric diagnostics."""

    def __init__(self, status_code: int, *, graph_code: int | None = None, graph_subcode: int | None = None):
        super().__init__(f"Meta rechazó el mensaje (HTTP {status_code}).")
        self.status_code = status_code
        self.graph_code = graph_code
        self.graph_subcode = graph_subcode

    @property
    def safe_error_code(self) -> str:
        result = f"meta_http_{self.status_code}"
        if self.graph_code is not None:
            result += f"_graph_{self.graph_code}"
        if self.graph_subcode is not None:
            result += f"_sub_{self.graph_subcode}"
        return result


class DeliveryUncertain(RuntimeError):
    """The request may have reached Meta; it must not be retried automatically."""


def valid_signature(raw_body: bytes, signature: str | None, app_secret: str) -> bool:
    if not app_secret or not signature:
        return False
    match = _SIGNATURE.fullmatch(signature)
    if match is None:
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, match.group(1))


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or len(value) > 12:
        raise ValueError("El evento no contiene una fecha válida.")
    return value


def _sender(value: Any) -> str:
    if not isinstance(value, str) or not _PHONE.fullmatch(value):
        raise ValueError("El evento no contiene un remitente válido.")
    # Meta webhooks may return Mexican mobile wa_id values in the legacy 521...
    # form. Keep one current E.164 representation internally so it matches the
    # +52 number configured by staff and the recipient accepted by the test API.
    if value.startswith("521") and len(value) == 13:
        value = "52" + value[3:]
    return f"+{value}"


def _message_event(message: Any) -> tuple[str, dict]:
    if not isinstance(message, dict):
        raise TypeError("El mensaje no tiene formato válido.")
    message_id = message.get("id")
    if not isinstance(message_id, str) or not message_id.startswith("wamid.") or len(message_id) > 256:
        raise ValueError("El mensaje no tiene un identificador válido.")
    kind = message.get("type")
    if not isinstance(kind, str):
        raise TypeError("El mensaje no tiene tipo válido.")
    event: dict[str, Any] = {
        "kind": "message",
        "message_id": message_id,
        "sender": _sender(message.get("from")),
        "timestamp": _timestamp(message.get("timestamp")),
        "type": kind if kind in {"text", "image", "interactive", "button"} else "unsupported",
    }
    if event["type"] == "text":
        text = message.get("text", {}).get("body") if isinstance(message.get("text"), dict) else None
        if not isinstance(text, str) or len(text) > 4096:
            raise ValueError("El texto recibido no es válido.")
        event["text"] = text
    elif event["type"] == "image":
        media_id = message.get("image", {}).get("id") if isinstance(message.get("image"), dict) else None
        if not isinstance(media_id, str) or not _MEDIA_ID.fullmatch(media_id):
            raise ValueError("La referencia de imagen no es válida.")
        event["media_id"] = media_id
    elif event["type"] in {"interactive", "button"}:
        interactive = message.get("interactive")
        button_reply = interactive.get("button_reply") if isinstance(interactive, dict) else message.get("button")
        if isinstance(button_reply, dict):
            reply_id = button_reply.get("id")
            title = button_reply.get("title")
            if isinstance(reply_id, str) and isinstance(title, str) and len(reply_id) <= 256 and len(title) <= 256:
                event["reply"] = {"id": reply_id, "title": title}
    return f"message:{message_id}", event


def _status_event(status: Any) -> tuple[str, dict]:
    if not isinstance(status, dict):
        raise TypeError("El recibo no tiene formato válido.")
    message_id, state = status.get("id"), status.get("status")
    if not isinstance(message_id, str) or not message_id.startswith("wamid.") or len(message_id) > 256:
        raise ValueError("El recibo no tiene identificador válido.")
    if state not in {"sent", "delivered", "read", "failed"}:
        raise ValueError("El recibo tiene un estado desconocido.")
    timestamp = _timestamp(status.get("timestamp"))
    normalized = {"kind": "status", "message_id": message_id, "status": state, "timestamp": timestamp}
    if isinstance(status.get("recipient_id"), str) and _PHONE.fullmatch(status["recipient_id"]):
        normalized["recipient"] = "+" + status["recipient_id"]
    errors = status.get("errors")
    if isinstance(errors, list):
        codes = [str(error.get("code")) for error in errors[:4] if isinstance(error, dict) and isinstance(error.get("code"), (str, int))]
        normalized["error_codes"] = codes
    return f"status:{message_id}:{state}:{timestamp}", normalized


def normalize_webhook(payload: Any, expected_phone_number_id: str) -> list[tuple[str, dict]]:
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        raise ValueError("El evento no corresponde a WhatsApp Business.")
    entries = payload.get("entry")
    if not isinstance(entries, list) or len(entries) > MAX_EVENTS:
        raise ValueError("El evento contiene una estructura no permitida.")
    normalized: list[tuple[str, dict]] = []
    for entry in entries:
        changes = entry.get("changes") if isinstance(entry, dict) else None
        if not isinstance(changes, list):
            raise TypeError("El evento no contiene cambios válidos.")
        for change in changes:
            if not isinstance(change, dict) or change.get("field") != "messages":
                continue
            value = change.get("value")
            metadata = value.get("metadata") if isinstance(value, dict) else None
            phone_id = metadata.get("phone_number_id") if isinstance(metadata, dict) else None
            if phone_id != expected_phone_number_id:
                raise ValueError("El evento no corresponde al número configurado.")
            messages = value.get("messages", [])
            statuses = value.get("statuses", [])
            if not isinstance(messages, list) or not isinstance(statuses, list):
                raise TypeError("El evento contiene listas no válidas.")
            for message in messages:
                normalized.append(_message_event(message))
            for status in statuses:
                normalized.append(_status_event(status))
            if len(normalized) > MAX_EVENTS:
                raise ValueError("El evento contiene demasiados mensajes.")
    if not normalized:
        raise ValueError("El evento no contiene mensajes ni recibos compatibles.")
    ids = [event_id for event_id, _ in normalized]
    if len(ids) != len(set(ids)):
        # Duplicate content in one Meta delivery should still be safely deduplicated by the store.
        deduped: dict[str, dict] = {}
        for event_id, event in normalized:
            deduped.setdefault(event_id, event)
        return list(deduped.items())
    return normalized


def create_whatsapp_router(settings: WhatsAppSettings, store: H2Store) -> APIRouter:
    router = APIRouter()

    @router.get("/webhook", response_class=PlainTextResponse)
    async def verify_webhook(
        mode: str | None = Query(default=None, alias="hub.mode"),
        token: str | None = Query(default=None, alias="hub.verify_token"),
        challenge: str | None = Query(default=None, alias="hub.challenge"),
    ) -> PlainTextResponse:
        if not settings.verify_token or not settings.verify_token.isascii() or not token:
            raise HTTPException(status_code=403, detail="Verificación rechazada.")
        token_ok = hmac.compare_digest(settings.verify_token, token)
        if mode != "subscribe" or not token_ok or challenge is None or len(challenge) > 128:
            raise HTTPException(status_code=403, detail="Verificación rechazada.")
        return PlainTextResponse(challenge)

    @router.post("/webhook")
    async def receive_webhook(request: Request) -> dict[str, int]:
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > MAX_WEBHOOK_BYTES:
                    raise HTTPException(status_code=413, detail="Evento demasiado grande.")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Longitud no válida.") from exc
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > MAX_WEBHOOK_BYTES:
                raise HTTPException(status_code=413, detail="Evento demasiado grande.")
        raw_body = bytes(chunks)
        if not valid_signature(raw_body, request.headers.get("x-hub-signature-256"), settings.app_secret):
            raise HTTPException(status_code=401, detail="Firma no válida.")
        try:
            payload = json.loads(raw_body)
            events = normalize_webhook(payload, settings.phone_number_id)
            outcomes = [store.admit_webhook_event(event_id, event) for event_id, event in events]
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Evento no válido.") from None
        return {
            "received": len(events),
            "queued": outcomes.count("queued"),
            "duplicates": outcomes.count("duplicate"),
            "deferred": len(events) - outcomes.count("queued") - outcomes.count("duplicate"),
        }

    return router


def create_whatsapp_app(
    settings: WhatsAppSettings, store: H2Store, *, assistant_queue=None,
    receipt_worker=None, outbox_worker=None, order_scheduler=None, retention_worker=None, resources=(),
) -> FastAPI:
    workers = [worker for worker in (retention_worker, assistant_queue, receipt_worker, outbox_worker, order_scheduler) if worker is not None]

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        started = []
        try:
            for worker in workers:
                await worker.start()
                started.append(worker)
            yield
        finally:
            for worker in reversed(started):
                await worker.stop()
            for resource in reversed(resources):
                for name in ("aclose", "close"):
                    closer = getattr(resource, name, None)
                    if callable(closer):
                        result = closer()
                        if isawaitable(result):
                            await result

    app = FastAPI(title="Coffee House Hackathon 2", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.include_router(create_whatsapp_router(settings, store))
    return app


class WhatsAppCloudClient:
    """Small send-only client. The access token never appears in object repr or errors."""

    def __init__(self, settings: WhatsAppSettings, *, client: httpx.AsyncClient | None = None):
        if not settings.access_token:
            raise ValueError("WHATSAPP_ACCESS_TOKEN no está configurado.")
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=5.0))
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send_text(self, recipient: str, body: str) -> str:
        if not _PHONE.fullmatch(recipient.removeprefix("+")):
            raise ValueError("El destinatario no es válido.")
        if not body or len(body) > 4096:
            raise ValueError("El mensaje no es válido.")
        url = (
            f"https://graph.facebook.com/{self._settings.graph_api_version}/"
            f"{self._settings.phone_number_id}/messages"
        )
        try:
            response = await self._client.post(
                url,
                headers={"Authorization": f"Bearer {self._settings.access_token}"},
                json={
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": recipient.removeprefix("+"),
                    "type": "text",
                    "text": {"preview_url": False, "body": body},
                },
            )
        except httpx.TimeoutException:
            raise DeliveryUncertain("Meta no confirmó si recibió el mensaje.") from None
        except httpx.HTTPError:
            raise DeliveryUncertain("No se pudo confirmar si Meta recibió el mensaje.") from None
        if response.status_code >= 500:
            raise DeliveryUncertain("Meta no confirmó si recibió el mensaje.")
        if not 200 <= response.status_code < 300:
            graph_code = graph_subcode = None
            try:
                error = response.json().get("error", {})
                if isinstance(error, dict):
                    candidate = error.get("code")
                    if type(candidate) is int and 0 <= candidate <= 2_147_483_647:
                        graph_code = candidate
                    candidate = error.get("error_subcode")
                    if type(candidate) is int and 0 <= candidate <= 2_147_483_647:
                        graph_subcode = candidate
            except (ValueError, TypeError, AttributeError):
                pass
            raise MetaRejected(
                response.status_code, graph_code=graph_code, graph_subcode=graph_subcode,
            )
        try:
            payload = response.json()
            messages = payload.get("messages") if isinstance(payload, dict) else None
            provider_id = messages[0].get("id") if isinstance(messages, list) and messages and isinstance(messages[0], dict) else None
        except (ValueError, TypeError, IndexError):
            provider_id = None
        if not isinstance(provider_id, str) or not provider_id.startswith("wamid.") or len(provider_id) > 256:
            raise DeliveryUncertain("Meta respondió sin un identificador de envío verificable.")
        return provider_id

    async def download_media(self, media_id: str, *, max_bytes: int = 5 * 1024 * 1024) -> tuple[bytes, str]:
        """Fetch an approved WhatsApp image with bounded size and a fixed download host."""
        if not _MEDIA_ID.fullmatch(media_id) or max_bytes != 5 * 1024 * 1024:
            raise ValueError("La referencia de imagen no es válida.")
        metadata_url = (
            f"https://graph.facebook.com/{self._settings.graph_api_version}/{media_id}"
        )
        try:
            metadata_response = await self._client.get(
                metadata_url,
                headers={"Authorization": f"Bearer {self._settings.access_token}"},
                follow_redirects=False,
            )
            if not 200 <= metadata_response.status_code < 300:
                raise ValueError("No se pudo consultar la imagen de WhatsApp.")
            metadata = metadata_response.json()
        except (httpx.HTTPError, ValueError, json.JSONDecodeError):
            raise ValueError("No se pudo consultar la imagen de WhatsApp.") from None
        url = metadata.get("url") if isinstance(metadata, dict) else None
        mime = metadata.get("mime_type") if isinstance(metadata, dict) else None
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (
            parsed is None or parsed.scheme != "https" or parsed.hostname != "lookaside.fbsbx.com"
            or parsed.username is not None or parsed.password is not None or not isinstance(mime, str)
            or mime.split(";", 1)[0].strip().lower() not in {"image/jpeg", "image/png"}
        ):
            raise ValueError("WhatsApp no devolvió una imagen compatible.")
        chunks: list[bytes] = []
        size = 0
        try:
            async with self._client.stream(
                "GET", url, headers={"Authorization": f"Bearer {self._settings.access_token}"},
                follow_redirects=False,
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise ValueError("No se pudo descargar la imagen.")
                declared_size = response.headers.get("content-length")
                if declared_size and (not declared_size.isdigit() or int(declared_size) > max_bytes):
                    raise ValueError("La imagen supera el tamaño permitido.")
                for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("La imagen supera el tamaño permitido.")
                    chunks.append(chunk)
        except httpx.HTTPError:
            raise ValueError("No se pudo descargar la imagen.") from None
        return b"".join(chunks), mime.split(";", 1)[0].strip().lower()

    async def send_next_outbox(self, store: H2Store, *, now: str | None = None) -> str | None:
        item = store.claim_outbox(now=now)
        if item is None:
            return None
        try:
            provider_id = await self.send_text(item["recipient_phone"], item["message_text"])
        except DeliveryUncertain:
            store.finish_outbox(item["outbox_id"], "uncertain", error_code="meta_uncertain")
            return "uncertain"
        except MetaRejected as exc:
            store.finish_outbox(item["outbox_id"], "failed", error_code=exc.safe_error_code)
            return "failed"
        store.finish_outbox(item["outbox_id"], "sent", provider_message_id=provider_id)
        return "sent"


class WhatsAppOutboxWorker:
    """Optional sender; the application factory only starts it when explicitly supplied."""

    def __init__(self, store: H2Store, client: WhatsAppCloudClient, *, poll_interval: float = 0.2):
        if not 0.05 <= poll_interval <= 5:
            raise ValueError("El intervalo de salida no es válido.")
        self._store = store
        self._client = client
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._store.recover_uncertain_outbox()
            self._task = asyncio.create_task(self._loop(), name="h2-whatsapp-outbox")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        task, self._task = self._task, None
        try:
            await asyncio.wait_for(task, timeout=5)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                result = await self._client.send_next_outbox(self._store)
            except H2StorageError:
                _LOG.warning("No se pudo actualizar la bandeja de mensajes H2; se pausó el envío.")
                result = None
            if result is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
                except TimeoutError:
                    pass
