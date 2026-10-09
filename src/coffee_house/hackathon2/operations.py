"""Authorized operational views and audited H2-only corrections."""
from __future__ import annotations

from .auth import PersonnelAuth
from .catalog import CatalogService
from .storage import H2Store


class OperationsService:
    def __init__(self, store: H2Store, auth: PersonnelAuth, catalog: CatalogService):
        self.store = store
        self.auth = auth
        self.catalog = catalog

    def inventory(self, actor_phone: str) -> dict:
        self.auth.authorize(actor_phone, "reports.read")
        return self.store.inventory_snapshot()

    def adjust_inventory(self, actor_phone: str, item_id: str, delta: int, reason: str, command_id: str) -> dict:
        self.auth.authorize(actor_phone, "inventory.adjust")
        return self.store.adjust_inventory(item_id, delta, actor_phone, reason, command_id)

    def set_variant_price(self, actor_phone: str, product_id: str, size: str, price_cents: int) -> None:
        self.auth.authorize(actor_phone, "catalog.manage")
        self.catalog.set_variant_price(actor_phone, product_id, size, price_cents)

    def set_extra_price(self, actor_phone: str, extra_id: str, price_cents: int) -> None:
        self.auth.authorize(actor_phone, "catalog.manage")
        self.catalog.set_extra_price(actor_phone, extra_id, price_cents)

    def audit(self, actor_phone: str, *, limit: int = 10) -> list[dict]:
        self.auth.authorize(actor_phone, "audit.read")
        return self.store.audit_rows(limit=limit)
