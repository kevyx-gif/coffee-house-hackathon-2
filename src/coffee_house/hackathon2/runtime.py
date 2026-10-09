"""Assembly of the isolated Hackathon 2 WhatsApp service."""
from __future__ import annotations

from pathlib import Path

from .auth import PersonnelAuth
from .catalog import CatalogService
from .conversation import ConversationEngine
from .guard import QwenGuardClient
from .jobs import (
    AssistantQueue,
    DataRetentionWorker,
    OrderAssignmentScheduler,
    WebhookReceiptWorker,
)
from .messaging import WhatsAppMessageHandler
from .model import GroqSettings, GroqToolRouter
from .operations import OperationsService
from .orders import OrderWorkflow
from .storage import H2Store
from .tickets import TicketReviewService
from .whatsapp import (
    WhatsAppCloudClient,
    WhatsAppOutboxWorker,
    WhatsAppSettings,
    create_whatsapp_app,
)


class DisabledToolModel:
    """Non-network placeholder used while external text is disabled."""

    async def route(self, _user_text: str):
        return None


def create_h2_app(
    *,
    catalog_path: str | Path,
    fixture_dir: str | Path,
    database_path: str | Path,
    whatsapp: WhatsAppSettings,
    guard_url: str,
    otp_pepper: str,
    initial_admin_phone: str | None = None,
    allow_external_text: bool = False,
    enable_meta_delivery: bool = False,
    model=None,
    guard=None,
    cloud_client: WhatsAppCloudClient | None = None,
    ticket_engine=None,
):
    """Build H2 dependencies from private settings; no defaults enable external delivery."""
    if type(allow_external_text) is not bool or type(enable_meta_delivery) is not bool:
        raise ValueError("Las autorizaciones externas deben indicarse explícitamente.")
    store = H2Store(database_path)
    store.seed_demo_directory(fixture_dir)
    auth = PersonnelAuth(store, otp_pepper=otp_pepper)
    if initial_admin_phone:
        auth.bootstrap_first_admin(initial_admin_phone)
    catalog = CatalogService(catalog_path, store)
    orders = OrderWorkflow(store, catalog, auth)
    operations = OperationsService(store, auth, catalog)
    model = model or _configured_model(allow_external_text)
    guard = guard or QwenGuardClient(guard_url)
    # Do not even construct a Meta client unless delivery was explicitly enabled.
    # The client is also used for OTPs and media downloads, so merely having a
    # token in the environment must not create an outbound side effect.
    cloud = None
    if enable_meta_delivery:
        cloud = cloud_client
        if cloud is None and whatsapp.access_token:
            cloud = WhatsAppCloudClient(whatsapp)
        if cloud is None:
            raise ValueError("La entrega real requiere un cliente de WhatsApp configurado.")
    otp_sender = cloud.send_text if cloud is not None else None
    conversation = ConversationEngine(
        catalog, guard, model, auth=auth, orders=orders, otp_sender=otp_sender,
        allow_external_text=allow_external_text,
    )
    tickets = TicketReviewService(store, auth, catalog, engine=ticket_engine) if ticket_engine is not None else None
    handler = WhatsAppMessageHandler(
        conversation, auth, orders, tickets=tickets, cloud=cloud, operations=operations,
    )
    retention_worker = DataRetentionWorker(store)
    assistant_queue = AssistantQueue(store, handler)
    receipt_worker = WebhookReceiptWorker(store)
    scheduler = OrderAssignmentScheduler(orders)
    outbox_worker = WhatsAppOutboxWorker(store, cloud) if enable_meta_delivery and cloud is not None else None
    resources = [item for item in (guard, model, cloud) if item is not None]
    app = create_whatsapp_app(
        whatsapp, store, assistant_queue=assistant_queue, receipt_worker=receipt_worker,
        order_scheduler=scheduler, outbox_worker=outbox_worker, retention_worker=retention_worker,
        resources=resources,
    )
    app.state.h2_store = store
    app.state.h2_auth = auth
    app.state.h2_orders = orders
    app.state.h2_operations = operations
    app.state.h2_queue = assistant_queue
    app.state.h2_handler = handler
    app.state.h2_external_delivery_enabled = enable_meta_delivery
    app.state.h2_external_text_enabled = allow_external_text
    return app


def _configured_model(allow_external_text: bool):
    if not allow_external_text:
        return DisabledToolModel()
    settings = GroqSettings.from_env()
    if not settings.api_key:
        raise ValueError("GROQ_API_KEY debe estar configurada para habilitar texto externo.")
    return GroqToolRouter(settings)
