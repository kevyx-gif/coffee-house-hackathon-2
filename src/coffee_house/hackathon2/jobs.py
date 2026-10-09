"""Durable FIFO worker for customer queries; provider and send effects stay bounded."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Protocol

from .conversation import ConversationReply
from .storage import H2StorageError, H2Store

MAX_ACTIVE = 3
MAX_WAITING = 10
QUERY_DEADLINE_SECONDS = 120
POLL_INTERVAL_SECONDS = 0.05
_LOG = logging.getLogger(__name__)


class ConversationHandler(Protocol):
    async def handle(self, phone: str, text: str, *, event_id: str | None = None) -> ConversationReply: ...


def _remaining_seconds(deadline_at: str, now: datetime | None = None) -> float:
    deadline = datetime.fromisoformat(deadline_at)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (deadline - current).total_seconds())


class AssistantQueue:
    """Runs up to three persisted jobs and acknowledges only confirmed cancellation."""

    def __init__(
        self, store: H2Store, handler: ConversationHandler, *, workers: int = MAX_ACTIVE,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ):
        if workers != MAX_ACTIVE:
            raise ValueError("El número de trabajadores debe coincidir con el límite aprobado de tres.")
        if not 0.01 <= poll_interval <= 1:
            raise ValueError("El intervalo de consulta no es válido.")
        self._store = store
        self._handler = handler
        self._workers = workers
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._worker_prefix = uuid.uuid4().hex[:12]

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        self._store.recover_assistant_jobs()
        self._store.recover_webhook_events()
        self._store.recover_uncertain_outbox()
        self._tasks = [
            asyncio.create_task(self._worker_loop(index), name=f"h2-assistant-{index}")
            for index in range(self._workers)
        ]

    async def stop(self) -> None:
        if not self._tasks:
            return
        self._stop.set()
        tasks, self._tasks = self._tasks, []
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
        except TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def process_one(self, worker_label: str = "test") -> bool:
        """Claim and process one item; useful for deterministic integration tests."""
        worker_id = f"{self._worker_prefix}:{worker_label}"[:128]
        job = self._store.claim_assistant_job(worker_id)
        if job is None:
            return False
        payload = job["payload"]
        remaining = _remaining_seconds(job["deadline_at"])
        if remaining <= 0:
            self._store.fail_assistant_job(job["job_id"], "deadline_exceeded")
            return True

        if isinstance(payload, dict) and payload.get("type") == "image":
            media_handler = getattr(self._handler, "handle_media", None)
            if callable(media_handler):
                task = asyncio.create_task(media_handler(job["sender"], payload.get("media_id", "")))
            else:
                task = asyncio.create_task(asyncio.sleep(0, result=ConversationReply(
                    "Por ahora no puedo revisar esa imagen. Si necesitas ayuda, consulta con el personal.",
                    "needs_input", "media_unavailable",
                )))
        else:
            text = payload.get("text") if isinstance(payload, dict) else None
            if not isinstance(text, str) or not text.strip():
                text = ""
            task = asyncio.create_task(
                self._handler.handle(job["sender"], text, event_id=job["event_id"])
            )
        deadline = asyncio.get_running_loop().time() + remaining
        try:
            while True:
                if self._store.assistant_job_cancel_requested(job["job_id"]):
                    await _stop_task(task)
                    self._store.complete_assistant_cancel(job["job_id"])
                    return True
                wait = min(self._poll_interval, max(0.0, deadline - asyncio.get_running_loop().time()))
                if task.done():
                    break
                if wait <= 0:
                    await _stop_task(task)
                    if self._store.assistant_job_cancel_requested(job["job_id"]):
                        self._store.complete_assistant_cancel(job["job_id"])
                    else:
                        self._store.fail_assistant_job(job["job_id"], "deadline_exceeded")
                    return True
                await asyncio.wait({task}, timeout=wait)
            try:
                reply = task.result()
            except asyncio.CancelledError:
                if self._store.assistant_job_cancel_requested(job["job_id"]):
                    self._store.complete_assistant_cancel(job["job_id"])
                else:
                    self._store.fail_assistant_job(job["job_id"], "cancelled_unexpectedly")
                return True
            if not isinstance(reply, ConversationReply) or not isinstance(reply.text, str):
                self._store.fail_assistant_job(job["job_id"], "invalid_handler_result")
                return True
            if (
                not self._store.complete_assistant_job(job["job_id"], reply.text)
                and self._store.assistant_job_cancel_requested(job["job_id"])
            ):
                self._store.complete_assistant_cancel(job["job_id"])
            return True
        except asyncio.CancelledError:
            await _stop_task(task)
            # Leave the durable row active. Startup recovery will mark it uncertain
            # and notify the customer instead of silently retrying it.
            raise
        except Exception:  # noqa: BLE001 - fail closed without exposing provider details
            await _stop_task(task)
            if self._store.assistant_job_cancel_requested(job["job_id"]):
                self._store.complete_assistant_cancel(job["job_id"])
            else:
                self._store.fail_assistant_job(job["job_id"], "worker_error")
            return True

    async def _worker_loop(self, index: int) -> None:
        while not self._stop.is_set():
            did_work = await self.process_one(str(index))
            if not did_work:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
                except TimeoutError:
                    pass


class WebhookReceiptWorker:
    """Apply provider receipts idempotently; customer conversations use AssistantQueue."""

    def __init__(self, store: H2Store, *, poll_interval: float = 0.1):
        if not 0.01 <= poll_interval <= 2:
            raise ValueError("El intervalo de recibos no es válido.")
        self._store = store
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="h2-webhook-receipts")

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

    async def process_one(self) -> bool:
        event = self._store.claim_webhook_event()
        if event is None:
            return False
        payload = event["payload"]
        try:
            if isinstance(payload, dict) and payload.get("kind") == "status":
                self._store.apply_delivery_status(payload["message_id"], payload["status"])
                self._store.finish_webhook_event(event["event_id"], "done")
            else:
                self._store.finish_webhook_event(event["event_id"], "failed", error_code="unsupported_event")
        except (H2StorageError, KeyError, TypeError, ValueError):
            try:
                self._store.finish_webhook_event(event["event_id"], "failed", error_code="invalid_receipt")
            except H2StorageError:
                _LOG.warning("No se pudo cerrar un recibo de WhatsApp tras un error de validación.")
        return True

    async def _loop(self) -> None:
        while not self._stop.is_set():
            did_work = await self.process_one()
            if not did_work:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
                except TimeoutError:
                    pass


class OrderAssignmentScheduler:
    """Advance due cashier rounds from persisted deadlines without holding them in memory."""

    def __init__(self, workflow, *, poll_interval: float = 1.0):
        if not 0.1 <= poll_interval <= 10:
            raise ValueError("El intervalo de enrutamiento no es válido.")
        self._workflow = workflow
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="h2-order-assignment-deadlines")

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
                await asyncio.to_thread(self._workflow.expire_or_route_due_assignments)
            except (H2StorageError, ValueError):
                # Keep the scheduler alive; persisted assignment deadlines remain authoritative.
                _LOG.warning("No se pudo avanzar un vencimiento de asignación; se volverá a revisar.")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except TimeoutError:
                pass


class DataRetentionWorker:
    """Periodically delete or redact expired customer and message data."""

    def __init__(self, store: H2Store, *, poll_interval: float = 60.0):
        if not 0.05 <= poll_interval <= 3600:
            raise ValueError("El intervalo de retención no es válido.")
        self._store = store
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        await self._purge()
        self._task = asyncio.create_task(self._loop(), name="h2-data-retention")

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

    async def _purge(self) -> None:
        try:
            await asyncio.to_thread(self._store.purge_expired)
        except H2StorageError:
            # Keep the service alive and retry on the next interval without logging data.
            _LOG.warning("No se pudo completar la limpieza periódica de datos H2; se volverá a intentar.")

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except TimeoutError:
                await self._purge()


async def _stop_task(task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
    # gather waits until the async provider request has unwound before the slot is released.
    await asyncio.gather(task, return_exceptions=True)
