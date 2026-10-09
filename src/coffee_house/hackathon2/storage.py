"""Private SQLite persistence for the isolated Hackathon 2 demonstration."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 5
PHONE_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
ROLES = {"admin", "manager", "cashier"}
ORDER_STATES = {"pending_staff", "accepted", "preparing", "ready", "delivered", "rejected", "expired", "cancelled"}
MAX_JSON_BYTES = 65536


class H2StorageError(RuntimeError):
    """The H2 state cannot be trusted; preserve state and pause the operation."""


class DuplicateConflict(H2StorageError):
    """An idempotency key was reused with different operation data."""


class InventoryShortage(H2StorageError):
    """The synthetic inventory no longer covers the requested movement."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _timestamp(value: str | None = None) -> str:
    if value is None:
        return utc_now()
    if not isinstance(value, str):
        raise TypeError("La fecha debe incluir zona horaria.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("La fecha no es válida.") from exc
    if parsed.tzinfo is None:
        raise ValueError("La fecha debe incluir zona horaria.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _phone(value: str) -> str:
    if not isinstance(value, str) or not PHONE_RE.fullmatch(value):
        raise ValueError("El número debe estar en formato internacional E.164.")
    return value


def _json(value: Any, *, max_bytes: int = MAX_JSON_BYTES) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Los datos no pueden serializarse de forma segura.") from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError("Los datos exceden el límite permitido.")
    return encoded


def _text(value: str, label: str, *, maximum: int = 512, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip()):
        raise ValueError(f"{label} no válido.")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise ValueError(f"{label} no válido.")
    return value


def _positive_integer(value: int, label: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum or value > 2_000_000_000:
        raise ValueError(f"{label} no válido.")
    return value


def _plus_hours(value: str, hours: int) -> str:
    return (datetime.fromisoformat(value) + timedelta(hours=hours)).isoformat(timespec="milliseconds")


def _plus_seconds(value: str, seconds: int) -> str:
    return (datetime.fromisoformat(value) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def _plus_minutes(value: str, minutes: int) -> str:
    return (datetime.fromisoformat(value) + timedelta(minutes=minutes)).isoformat(timespec="milliseconds")


def _minus_minutes(value: str, minutes: int) -> str:
    return (datetime.fromisoformat(value) - timedelta(minutes=minutes)).isoformat(timespec="milliseconds")


def hmac_compare(expected: str, actual: str) -> bool:
    return hmac.compare_digest(expected, actual)


def _is_cancel_request(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = re.sub(r"\s+", " ", value.casefold().strip())
    return normalized in {"cancelar consulta", "cancelar mi consulta", "/cancelar consulta", "/cancelar"}


class H2Store:
    """One database file for H2; it never opens or migrates the H1 database."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000):
        self.path = Path(path).expanduser().absolute()
        if self.path.is_symlink():
            raise ValueError("La base H2 no puede ser un enlace simbólico.")
        _positive_integer(busy_timeout_ms, "El tiempo de espera", allow_zero=True)
        if busy_timeout_ms > 10_000:
            raise ValueError("El tiempo de espera excede el límite.")
        self.busy_timeout_ms = busy_timeout_ms
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                try:
                    yield connection
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        except sqlite3.Error as exc:
            raise H2StorageError("No se confirmó la operación; el estado requiere revisión.") from exc

    def _migrate(self) -> None:
        try:
            with closing(self._connect()) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                tables = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if version > SCHEMA_VERSION:
                    raise H2StorageError("La base tiene una versión futura; no se modificó.")
                if version == SCHEMA_VERSION:
                    self._verify_schema(connection)
                    return
                if version == 1:
                    self._migrate_v1_to_v2(connection)
                    version = 2
                if version == 2:
                    self._migrate_v2_to_v3(connection)
                    version = 3
                if version == 3:
                    self._migrate_v3_to_v4(connection)
                    version = 4
                if version == 4:
                    self._migrate_v4_to_v5(connection)
                    version = 5
                if version == SCHEMA_VERSION:
                    self._verify_schema(connection)
                    return
                if version != 0 or tables:
                    raise H2StorageError("La base existente no tiene una migración reconocida; no se reconstruyó.")
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute("INSERT INTO metadata(key,value) VALUES('created_at',?)", (utc_now(),))
                connection.execute("INSERT INTO metadata(key,value) VALUES('database_id',?)", (uuid.uuid4().hex,))
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                connection.commit()
            if os.name != "nt":
                os.chmod(self.path, 0o600)
        except H2StorageError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise H2StorageError("No se pudo inicializar la base H2.") from exc

    @staticmethod
    def _verify_schema(connection: sqlite3.Connection) -> None:
        actual = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        expected = {"metadata", "staff", "auth_codes", "staff_sessions", "conversations",
                    "webhook_events", "orders", "order_assignments", "inventory_items",
                    "recipe_components", "modifier_components", "modifier_substitutions", "tickets", "inventory_movements",
                    "audit_log", "outbox", "order_rounds", "order_recipients", "assistant_jobs",
                    "catalog_price_overrides"}
        if actual != expected:
            raise H2StorageError("El esquema H2 está incompleto o fue alterado; no se reconstruyó.")

    @staticmethod
    def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
        """Add durable routing rounds without replacing any existing H2 rows."""
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ALTER TABLE orders ADD COLUMN accepted_by_phone TEXT REFERENCES staff(phone)")
            connection.execute("""CREATE TABLE order_rounds(
                order_id TEXT NOT NULL REFERENCES orders(order_id),round_no INTEGER NOT NULL CHECK(round_no>0),
                phase TEXT NOT NULL CHECK(phase IN ('cashier','escalation')),
                opened_at TEXT NOT NULL,deadline TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('active','closed')),
                PRIMARY KEY(order_id,round_no))""")
            connection.execute("""CREATE TABLE order_recipients(
                order_id TEXT NOT NULL,round_no INTEGER NOT NULL,staff_phone TEXT NOT NULL REFERENCES staff(phone),
                role TEXT NOT NULL CHECK(role IN ('admin','cashier')),notified_at TEXT NOT NULL,
                response_at TEXT,result TEXT CHECK(result IN ('accepted','passed','timed_out','rejected')),
                PRIMARY KEY(order_id,round_no,staff_phone),
                FOREIGN KEY(order_id,round_no) REFERENCES order_rounds(order_id,round_no))""")
            connection.execute("CREATE INDEX idx_order_recipients_staff ON order_recipients(staff_phone,response_at)")
            # Preserve old assignment history as one-recipient rounds.
            rows = connection.execute(
                "SELECT a.order_id,a.sequence,a.staff_phone,a.assigned_at,a.deadline,a.response_at,a.result,s.role,o.status "
                "FROM order_assignments a JOIN staff s ON s.phone=a.staff_phone JOIN orders o ON o.order_id=a.order_id "
                "ORDER BY a.order_id,a.sequence"
            ).fetchall()
            for row in rows:
                if row["role"] not in {"admin", "cashier"}:
                    continue
                active = row["status"] == "pending_staff" and row["response_at"] is None
                connection.execute(
                    "INSERT INTO order_rounds(order_id,round_no,phase,opened_at,deadline,status) VALUES(?,?,?,?,?,?)",
                    (row["order_id"], row["sequence"], "cashier", row["assigned_at"], row["deadline"], "active" if active else "closed"),
                )
                connection.execute(
                    "INSERT INTO order_recipients(order_id,round_no,staff_phone,role,notified_at,response_at,result) VALUES(?,?,?,?,?,?,?)",
                    (row["order_id"], row["sequence"], row["staff_phone"], row["role"], row["assigned_at"], row["response_at"], row["result"]),
                )
            connection.execute(
                "UPDATE orders SET accepted_by_phone=(SELECT staff_phone FROM order_assignments a "
                "WHERE a.order_id=orders.order_id AND a.result='accepted' ORDER BY a.sequence DESC LIMIT 1) "
                "WHERE status IN ('accepted','preparing','ready','delivered')"
            )
            connection.execute(
                "INSERT INTO tickets(ticket_id,source,sender_phone,status,lines_json,media_reference,created_at,expires_at,reviewed_at,reviewed_by,fingerprint) "
                "SELECT 'wa-'||o.order_id,'whatsapp_order',o.customer_phone,CASE WHEN o.status='pending_staff' THEN 'needs_review' "
                "WHEN o.status IN ('rejected','cancelled') THEN 'rejected' WHEN o.status='expired' THEN 'expired' ELSE 'confirmed' END,o.items_json,NULL,o.created_at,"
                "?,o.accepted_at,o.accepted_by_phone,o.request_fingerprint FROM orders o "
                "WHERE NOT EXISTS(SELECT 1 FROM tickets t WHERE t.source='whatsapp_order' AND t.sender_phone=o.customer_phone AND t.fingerprint=o.request_fingerprint)"
                , (_plus_hours(utc_now(), 24),)
            )
            connection.execute("PRAGMA user_version=2")
            connection.commit()
        except (sqlite3.Error, KeyError) as exc:
            connection.rollback()
            raise H2StorageError("No se pudo ampliar el esquema H2; se conservaron los datos existentes.") from exc

    @staticmethod
    def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
        """Add the durable, bounded assistant queue without rewriting prior data."""
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(_ASSISTANT_JOBS_TABLE)
            connection.execute(
                "CREATE UNIQUE INDEX idx_assistant_jobs_pending_phone "
                "ON assistant_jobs(sender_phone) WHERE status IN ('queued','running','cancel_requested')"
            )
            connection.execute(
                "CREATE INDEX idx_assistant_jobs_queue ON assistant_jobs(status,queue_sequence)"
            )
            connection.execute("PRAGMA user_version=3")
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise H2StorageError("No se pudo ampliar la cola H2; se conservaron los datos existentes.") from exc

    @staticmethod
    def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
        """Add durable H2-only catalog price overrides for authorized staff."""
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(_CATALOG_PRICE_OVERRIDES_TABLE)
            connection.execute("PRAGMA user_version=4")
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise H2StorageError("No se pudo ampliar el catálogo H2; se conservaron los datos existentes.") from exc

    @staticmethod
    def _migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
        """Add explicit, inventory-backed ingredient substitutions without resetting stock."""
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE modifier_substitutions("
                "modifier_id TEXT PRIMARY KEY,"
                "replaced_item_id TEXT NOT NULL REFERENCES inventory_items(item_id),"
                "substitute_item_id TEXT NOT NULL REFERENCES inventory_items(item_id),"
                "CHECK(replaced_item_id<>substitute_item_id))"
            )
            connection.execute("PRAGMA user_version=5")
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise H2StorageError("No se pudo añadir el esquema de sustituciones; se conservaron los datos existentes.") from exc

    @property
    def schema_version(self) -> int:
        with closing(self._connect()) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise H2StorageError("La versión de base no coincide con la aplicación.")
        return version

    def seed_demo(
        self, inventory: list[dict], recipes: list[dict], modifiers: list[dict],
        substitutions: list[dict] | None = None,
    ) -> bool:
        """Seed demo quantities once; safely refresh recipe rules without restoring stock."""
        normalized_items = []
        seen_items: set[str] = set()
        for item in inventory:
            item_id = _text(item.get("id"), "Identificador de insumo", maximum=128)
            if item_id in seen_items:
                raise ValueError("Hay identificadores de inventario duplicados.")
            seen_items.add(item_id)
            normalized_items.append((item_id, _text(item.get("name"), "Nombre", maximum=256),
                                     _text(item.get("unit"), "Unidad", maximum=32),
                                     _positive_integer(item.get("on_hand"), "Existencia", allow_zero=True)))
        recipe_rows = self._recipe_rows(recipes, seen_items)
        modifier_rows = self._modifier_rows(modifiers, seen_items)
        substitution_rows = self._substitution_rows(substitutions or [], seen_items)
        if {row[0] for row in modifier_rows} & {row[0] for row in substitution_rows}:
            raise ValueError("Un modificador no puede ser un extra y una sustitución a la vez.")
        with self._transaction() as connection:
            seeded = connection.execute("SELECT value FROM metadata WHERE key='demo_seeded'").fetchone()
            if seeded:
                for modifier_id, item_id, quantity in modifier_rows:
                    connection.execute(
                        "INSERT INTO modifier_components(modifier_id,item_id,quantity) VALUES(?,?,?) "
                        "ON CONFLICT(modifier_id,item_id) DO UPDATE SET quantity=excluded.quantity",
                        (modifier_id, item_id, quantity),
                    )
                for modifier_id, replaced_item_id, substitute_item_id in substitution_rows:
                    connection.execute(
                        "DELETE FROM modifier_components WHERE modifier_id=?", (modifier_id,)
                    )
                    connection.execute(
                        "INSERT INTO modifier_substitutions(modifier_id,replaced_item_id,substitute_item_id) "
                        "VALUES(?,?,?) ON CONFLICT(modifier_id) DO UPDATE SET "
                        "replaced_item_id=excluded.replaced_item_id,substitute_item_id=excluded.substitute_item_id",
                        (modifier_id, replaced_item_id, substitute_item_id),
                    )
                configured = {row[0] for row in substitution_rows}
                if configured:
                    placeholders = ",".join("?" for _ in configured)
                    connection.execute(
                        f"DELETE FROM modifier_substitutions WHERE modifier_id NOT IN ({placeholders})",
                        tuple(sorted(configured)),
                    )
                else:
                    connection.execute("DELETE FROM modifier_substitutions")
                return False
            connection.executemany(
                "INSERT INTO inventory_items(item_id,name,unit,on_hand,updated_at) VALUES(?,?,?,?,?)",
                [(item_id, name, unit, quantity, utc_now()) for item_id, name, unit, quantity in normalized_items],
            )
            connection.executemany(
                "INSERT INTO recipe_components(product_id,variant,item_id,quantity) VALUES(?,?,?,?)", recipe_rows
            )
            connection.executemany(
                "INSERT INTO modifier_components(modifier_id,item_id,quantity) VALUES(?,?,?)", modifier_rows
            )
            connection.executemany(
                "INSERT INTO modifier_substitutions(modifier_id,replaced_item_id,substitute_item_id) VALUES(?,?,?)",
                substitution_rows,
            )
            connection.execute("INSERT INTO metadata(key,value) VALUES('demo_seeded',?)", (utc_now(),))
            return True

    def seed_demo_directory(self, fixture_dir: str | Path) -> bool:
        """Load only the named synthetic fixture files; no product data is copied."""
        fixture_dir = Path(fixture_dir).expanduser().absolute()
        try:
            inventory = json.loads((fixture_dir / "inventory.json").read_text(encoding="utf-8"))
            recipes = json.loads((fixture_dir / "recipes.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise H2StorageError("No se pudieron leer las fixtures sintéticas aprobadas.") from exc
        if any(
            not isinstance(document, dict)
            or document.get("schema_version") != 1
            or document.get("environment") != "demo_sintetica"
            for document in (inventory, recipes)
        ):
            raise H2StorageError("Los datos no están marcados como fixtures sintéticas.")
        items, recipe_rows, modifiers = inventory.get("items"), recipes.get("recipes"), recipes.get("synthetic_modifiers")
        substitutions = recipes.get("synthetic_substitutions", [])
        if (not isinstance(items, list) or not isinstance(recipe_rows, list) or not isinstance(modifiers, list)
                or not isinstance(substitutions, list)):
            raise H2StorageError("Las fixtures no cumplen el esquema esperado.")
        return self.seed_demo(items, recipe_rows, modifiers, substitutions)

    @staticmethod
    def _recipe_rows(recipes: list[dict], item_ids: set[str]) -> list[tuple[str, str, str, int]]:
        rows = []
        seen = set()
        for recipe in recipes:
            product = _text(recipe.get("product_id"), "Producto", maximum=128)
            variant = _text(recipe.get("variant"), "Tamaño", maximum=64)
            for component in recipe.get("consumption_per_unit", []):
                item_id = _text(component.get("inventory_item_id"), "Insumo", maximum=128)
                quantity = _positive_integer(component.get("quantity"), "Consumo")
                key = (product, variant, item_id)
                if item_id not in item_ids or key in seen:
                    raise ValueError("La receta contiene un insumo inexistente o una línea duplicada.")
                seen.add(key)
                rows.append((*key, quantity))
        if not rows:
            raise ValueError("Se requiere al menos una receta sintética.")
        return rows

    @staticmethod
    def _modifier_rows(modifiers: list[dict], item_ids: set[str]) -> list[tuple[str, str, int]]:
        rows = []
        seen = set()
        for modifier in modifiers:
            modifier_id = _text(modifier.get("modifier_id"), "Extra", maximum=128)
            item_id = _text(modifier.get("inventory_item_id"), "Insumo", maximum=128)
            quantity = _positive_integer(modifier.get("quantity_per_beverage"), "Consumo de extra")
            if item_id not in item_ids or modifier_id in seen:
                raise ValueError("El extra referencia un insumo inexistente o duplicado.")
            seen.add(modifier_id)
            rows.append((modifier_id, item_id, quantity))
        return rows

    @staticmethod
    def _substitution_rows(substitutions: list[dict], item_ids: set[str]) -> list[tuple[str, str, str]]:
        rows = []
        seen = set()
        for substitution in substitutions:
            modifier_id = _text(substitution.get("modifier_id"), "Sustitución", maximum=128)
            replaced_id = _text(substitution.get("replaces_inventory_item_id"), "Insumo reemplazado", maximum=128)
            substitute_id = _text(substitution.get("substitute_inventory_item_id"), "Insumo sustituto", maximum=128)
            if (modifier_id in seen or replaced_id not in item_ids or substitute_id not in item_ids
                    or replaced_id == substitute_id):
                raise ValueError("La sustitución referencia insumos inválidos o duplicados.")
            seen.add(modifier_id)
            rows.append((modifier_id, replaced_id, substitute_id))
        return rows

    def inventory_snapshot(self) -> dict[str, dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT item_id,name,unit,on_hand,updated_at FROM inventory_items ORDER BY item_id"
            ).fetchall()
        return {row["item_id"]: dict(row) for row in rows}

    def adjust_inventory(
        self, item_id: str, quantity_delta: int, actor_phone: str, reason: str, command_id: str,
        *, now: str | None = None,
    ) -> dict[str, Any]:
        """Apply an authorized, auditable and idempotent inventory correction."""
        item_id = _text(item_id, "Insumo", maximum=128)
        actor_phone = _phone(actor_phone)
        reason = _text(reason, "Motivo", maximum=240)
        command_id = _text(command_id, "Identificador de comando", maximum=128)
        if type(quantity_delta) is not int or quantity_delta == 0 or abs(quantity_delta) > 100_000:
            raise ValueError("El ajuste de existencia no es válido.")
        now = _timestamp(now)
        source_id = "wa-adjust:" + hashlib.sha256(command_id.encode("utf-8")).hexdigest()
        with self._transaction() as connection:
            item = connection.execute(
                "SELECT item_id,name,unit,on_hand,updated_at FROM inventory_items WHERE item_id=?", (item_id,),
            ).fetchone()
            if item is None:
                raise H2StorageError("No encontré ese insumo de demostración.")
            prior = connection.execute(
                "SELECT quantity_delta,actor_phone FROM inventory_movements "
                "WHERE source_type='adjustment' AND source_id=? AND item_id=?", (source_id, item_id),
            ).fetchone()
            if prior is not None:
                if prior["quantity_delta"] != quantity_delta or prior["actor_phone"] != actor_phone:
                    raise DuplicateConflict("El comando ya fue usado con otros datos.")
                return dict(item)
            changed = connection.execute(
                "UPDATE inventory_items SET on_hand=on_hand+?,updated_at=? "
                "WHERE item_id=? AND on_hand+?>=0", (quantity_delta, now, item_id, quantity_delta),
            )
            if changed.rowcount != 1:
                raise InventoryShortage("El ajuste dejaría la existencia por debajo de cero.")
            new_quantity = item["on_hand"] + quantity_delta
            connection.execute(
                "INSERT INTO inventory_movements(movement_id,source_type,source_id,item_id,quantity_delta,actor_phone,created_at) "
                "VALUES(?, 'adjustment', ?, ?, ?, ?, ?)",
                (f"{source_id}:{item_id}", source_id, item_id, quantity_delta, actor_phone, now),
            )
            self._audit(
                connection, actor_phone, "inventory_adjusted", item_id, "ok",
                {"delta": quantity_delta, "before": item["on_hand"], "after": new_quantity, "reason": reason}, now,
            )
            updated = connection.execute(
                "SELECT item_id,name,unit,on_hand,updated_at FROM inventory_items WHERE item_id=?", (item_id,),
            ).fetchone()
        return dict(updated)

    def catalog_price_overrides(self) -> dict[tuple[str, str, str], dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT entry_type,entry_id,variant_size,price_cents,price_status,updated_by,updated_at "
                "FROM catalog_price_overrides"
            ).fetchall()
        return {
            (row["entry_type"], row["entry_id"], row["variant_size"]): dict(row)
            for row in rows
        }

    def set_catalog_price(
        self, entry_type: str, entry_id: str, variant_size: str, price_cents: int,
        actor_phone: str, *, previous_price_cents: int | None = None, now: str | None = None,
    ) -> dict[str, Any]:
        if entry_type not in {"variant", "extra"}:
            raise ValueError("El tipo de precio no es válido.")
        entry_id = _text(entry_id, "Producto", maximum=128)
        variant_size = _text(variant_size, "Tamaño", maximum=64, allow_empty=entry_type == "extra")
        if entry_type == "extra" and variant_size:
            raise ValueError("El tamaño no aplica a un extra.")
        price_cents = _positive_integer(price_cents, "Precio", allow_zero=True)
        actor_phone, now = _phone(actor_phone), _timestamp(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT price_cents FROM catalog_price_overrides WHERE entry_type=? AND entry_id=? AND variant_size=?",
                (entry_type, entry_id, variant_size),
            ).fetchone()
            if previous_price_cents is not None:
                _positive_integer(previous_price_cents, "Precio anterior", allow_zero=True)
            previous = row["price_cents"] if row is not None else previous_price_cents
            connection.execute(
                "INSERT INTO catalog_price_overrides(entry_type,entry_id,variant_size,price_cents,price_status,updated_by,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(entry_type,entry_id,variant_size) DO UPDATE SET "
                "price_cents=excluded.price_cents,price_status=excluded.price_status,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                (entry_type, entry_id, variant_size, price_cents, "provisional", actor_phone, now),
            )
            self._audit(
                connection, actor_phone, "catalog_price_changed", f"{entry_id}:{variant_size}", "ok",
                {"entry_type": entry_type, "previous_cents": previous, "price_cents": price_cents}, now,
            )
        return {"entry_type": entry_type, "entry_id": entry_id, "variant_size": variant_size,
                "price_cents": price_cents, "price_status": "provisional", "updated_by": actor_phone,
                "updated_at": now}

    def available_portions(self, product_id: str, variant: str, modifiers: list[str] | None = None) -> int | None:
        """Return the synthetic portion limit, or None when the recipe is not verified."""
        product_id = _text(product_id, "Producto", maximum=128)
        variant = _text(variant, "Tamaño", maximum=64)
        modifiers = modifiers or []
        if not isinstance(modifiers, list) or len(modifiers) > 3 or any(not isinstance(value, str) or not value for value in modifiers):
            raise ValueError("La lista de extras no es válida.")
        if len(modifiers) != len(set(modifiers)):
            raise ValueError("La lista de extras no es válida.")
        with closing(self._connect()) as connection:
            base_rows = connection.execute(
                "SELECT rc.item_id,rc.quantity,i.on_hand FROM recipe_components rc "
                "LEFT JOIN inventory_items i ON i.item_id=rc.item_id WHERE rc.product_id=? AND rc.variant=?",
                (product_id, variant),
            ).fetchall()
            if not base_rows or any(row["on_hand"] is None for row in base_rows):
                return None
            needs: dict[str, int] = {row["item_id"]: row["quantity"] for row in base_rows}
            stock = {row["item_id"]: row["on_hand"] for row in base_rows}
            replaced_items: set[str] = set()
            for modifier in modifiers:
                substitution = connection.execute(
                    "SELECT replaced_item_id,substitute_item_id FROM modifier_substitutions WHERE modifier_id=?",
                    (modifier,),
                ).fetchone()
                if substitution is not None:
                    replaced_item = substitution["replaced_item_id"]
                    if replaced_item in replaced_items or replaced_item not in needs:
                        return None
                    replacement_quantity = needs.pop(replaced_item)
                    stock.pop(replaced_item, None)
                    replaced_items.add(replaced_item)
                    substitute = connection.execute(
                        "SELECT on_hand FROM inventory_items WHERE item_id=?", (substitution["substitute_item_id"],)
                    ).fetchone()
                    if substitute is None:
                        return None
                    substitute_item = substitution["substitute_item_id"]
                    needs[substitute_item] = needs.get(substitute_item, 0) + replacement_quantity
                    stock[substitute_item] = substitute["on_hand"]
                    continue
                row = connection.execute(
                    "SELECT mc.item_id,mc.quantity,i.on_hand FROM modifier_components mc "
                    "LEFT JOIN inventory_items i ON i.item_id=mc.item_id WHERE mc.modifier_id=?", (modifier,)
                ).fetchone()
                if row is None or row["on_hand"] is None:
                    return None
                needs[row["item_id"]] = needs.get(row["item_id"], 0) + row["quantity"]
                stock[row["item_id"]] = row["on_hand"]
        if not needs or any(item_id not in stock or quantity <= 0 for item_id, quantity in needs.items()):
            return None
        return min(stock[item_id] // quantity for item_id, quantity in needs.items())

    def create_staff(self, phone: str, role: str, *, invited_by: str | None = None, now: str | None = None) -> None:
        phone = _phone(phone)
        if role not in ROLES:
            raise ValueError("Rol no reconocido.")
        if invited_by is not None:
            invited_by = _phone(invited_by)
        now = _timestamp(now)
        try:
            with self._transaction() as connection:
                if invited_by is not None:
                    inviter = connection.execute("SELECT role,active FROM staff WHERE phone=?", (invited_by,)).fetchone()
                    if inviter is None or not inviter["active"] or inviter["role"] != "admin":
                        raise H2StorageError("Solo un administrador activo puede invitar personal.")
                else:
                    existing = connection.execute("SELECT 1 FROM staff LIMIT 1").fetchone()
                    if role != "admin" or existing is not None:
                        raise H2StorageError("Solo se puede inicializar el primer administrador en una base vacía.")
                connection.execute(
                    "INSERT INTO staff(phone,role,active,invited_by,created_at) VALUES(?,?,1,?,?)",
                    (phone, role, invited_by, now),
                )
                self._audit(connection, invited_by or phone, "staff_created", phone, "ok", {"role": role}, now)
        except sqlite3.IntegrityError as exc:
            raise DuplicateConflict("Ese número ya está registrado.") from exc

    def set_staff_role(self, phone: str, role: str, *, actor: str, active: bool = True, now: str | None = None) -> None:
        phone, actor, now = _phone(phone), _phone(actor), _timestamp(now)
        if role not in ROLES or type(active) is not bool:
            raise ValueError("Rol o estado no válido.")
        with self._transaction() as connection:
            admin = connection.execute("SELECT role,active FROM staff WHERE phone=?", (actor,)).fetchone()
            if admin is None or not admin["active"] or admin["role"] != "admin":
                raise H2StorageError("Acción no autorizada.")
            cursor = connection.execute("UPDATE staff SET role=?,active=? WHERE phone=?", (role, int(active), phone))
            if cursor.rowcount != 1:
                raise H2StorageError("El personal no existe.")
            connection.execute("UPDATE staff_sessions SET revoked_at=? WHERE phone=? AND revoked_at IS NULL", (now, phone))
            connection.execute("UPDATE auth_codes SET used_at=? WHERE phone=? AND used_at IS NULL", (now, phone))
            self._audit(connection, actor, "staff_role_changed", phone, "ok", {"role": role, "active": active}, now)

    def store_auth_code(self, phone: str, code_hash: str, expires_at: str, *, now: str | None = None) -> None:
        phone, now, expires_at = _phone(phone), _timestamp(now), _timestamp(expires_at)
        if not isinstance(code_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", code_hash):
            raise ValueError("El código debe almacenarse como hash SHA-256.")
        with self._transaction() as connection:
            staff = connection.execute("SELECT active FROM staff WHERE phone=?", (phone,)).fetchone()
            if staff is None or not staff["active"]:
                raise H2StorageError("No se puede autenticar este número.")
            previous = connection.execute(
                "SELECT last_sent_at FROM auth_codes WHERE phone=?", (phone,)
            ).fetchone()
            if previous is not None and now < _plus_seconds(previous["last_sent_at"], 60):
                raise H2StorageError("La solicitud debe esperar antes de reenviar el código.")
            connection.execute(
                "INSERT INTO auth_codes(phone,code_hash,created_at,last_sent_at,expires_at,attempts,used_at) "
                "VALUES(?,?,?,?,?,0,NULL) ON CONFLICT(phone) DO UPDATE SET code_hash=excluded.code_hash, "
                "created_at=excluded.created_at,last_sent_at=excluded.last_sent_at,expires_at=excluded.expires_at,attempts=0,used_at=NULL",
                (phone, code_hash, now, now, expires_at),
            )

    def auth_code_pending(self, phone: str, *, now: str | None = None) -> bool:
        """Check locally whether an unexpired, unused staff code can be verified."""
        phone, now = _phone(phone), _timestamp(now)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM auth_codes c JOIN staff s ON s.phone=c.phone "
                "WHERE c.phone=? AND s.active=1 AND c.used_at IS NULL "
                "AND c.expires_at>? AND c.attempts<5",
                (phone, now),
            ).fetchone()
        return row is not None

    def verify_auth_code_and_create_session(
        self, phone: str, code_hash: str, session_hash: str, *, now: str, expires_at: str,
    ) -> bool:
        phone, now, expires_at = _phone(phone), _timestamp(now), _timestamp(expires_at)
        if not isinstance(code_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", code_hash):
            raise ValueError("El código debe compararse mediante su verificador HMAC.")
        if not isinstance(session_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", session_hash):
            raise ValueError("La sesión debe almacenarse como hash SHA-256.")
        with self._transaction() as connection:
            staff = connection.execute("SELECT role,active FROM staff WHERE phone=?", (phone,)).fetchone()
            code = connection.execute(
                "SELECT code_hash,used_at,expires_at,attempts FROM auth_codes WHERE phone=?", (phone,)
            ).fetchone()
            if staff is None or not staff["active"] or code is None or code["used_at"] is not None or code["expires_at"] <= now or code["attempts"] >= 5:
                return False
            if not hmac_compare(code["code_hash"], code_hash):
                attempts = code["attempts"] + 1
                connection.execute(
                    "UPDATE auth_codes SET attempts=?,used_at=? WHERE phone=? AND used_at IS NULL",
                    (attempts, now if attempts >= 5 else None, phone),
                )
                phone_ref = hashlib.sha256(phone.encode()).hexdigest()[:16]
                self._audit(connection, f"staff-ref:{phone_ref}", "staff_login", f"staff-ref:{phone_ref}", "denied", {"reason": "invalid_code"}, now)
                return False
            connection.execute("UPDATE auth_codes SET used_at=? WHERE phone=?", (now, phone))
            connection.execute(
                "UPDATE staff_sessions SET revoked_at=? WHERE phone=? AND revoked_at IS NULL", (now, phone)
            )
            connection.execute(
                "INSERT INTO staff_sessions(session_hash,phone,role,created_at,last_activity_at,expires_at,revoked_at) "
                "VALUES(?,?,?,?,?,?,NULL)",
                (session_hash, phone, staff["role"], now, now, expires_at),
            )
            self._audit(connection, phone, "staff_login", phone, "ok", {}, now)
            return True

    def staff_session(self, phone: str, *, now: str | None = None) -> dict | None:
        phone, now = _phone(phone), _timestamp(now)
        expires_at = _plus_minutes(now, 30)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT s.session_hash,s.phone,u.role,s.role AS session_role,s.last_activity_at,s.expires_at "
                "FROM staff_sessions s JOIN staff u ON u.phone=s.phone "
                "WHERE s.phone=? AND s.revoked_at IS NULL AND u.active=1 "
                "ORDER BY s.created_at DESC LIMIT 1", (phone,)
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= now or row["last_activity_at"] <= _minus_minutes(now, 30) or row["role"] != row["session_role"]:
                connection.execute("UPDATE staff_sessions SET revoked_at=? WHERE session_hash=?", (now, row["session_hash"]))
                return None
            connection.execute(
                "UPDATE staff_sessions SET last_activity_at=?,expires_at=? WHERE session_hash=?",
                (now, expires_at, row["session_hash"]),
            )
            return {"phone": row["phone"], "role": row["role"], "session_expires_at": expires_at}

    def order_assignment_is_current(self, folio: str, phone: str) -> bool:
        folio = _text(folio, "Folio", maximum=32)
        phone = _phone(phone)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT o.status,o.accepted_by_phone,r.staff_phone,r.response_at,rr.status AS round_status "
                "FROM orders o LEFT JOIN order_rounds rr ON rr.order_id=o.order_id AND rr.status='active' "
                "LEFT JOIN order_recipients r ON r.order_id=rr.order_id AND r.round_no=rr.round_no "
                "WHERE o.folio=? AND (r.staff_phone=? OR r.staff_phone IS NULL) ORDER BY rr.round_no DESC LIMIT 1", (folio, phone)
            ).fetchone()
            if row and row["status"] in {"accepted", "preparing", "ready"}:
                return row["accepted_by_phone"] == phone
            if row and row["status"] == "pending_staff":
                return row["round_status"] == "active" and row["staff_phone"] == phone and row["response_at"] is None
            # Compatibility with rows created before the routing-round tables.
            legacy = connection.execute(
                "SELECT a.staff_phone,a.result,o.status FROM orders o JOIN order_assignments a ON a.order_id=o.order_id "
                "WHERE o.folio=? ORDER BY a.sequence DESC LIMIT 1", (folio,)
            ).fetchone()
        return bool(legacy and legacy["staff_phone"] == phone and legacy["result"] in {None, "accepted"}
                    and legacy["status"] in {"pending_staff", "accepted", "preparing", "ready"})

    def staff_count(self) -> int:
        with closing(self._connect()) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM staff").fetchone()[0])

    def record_denial(self, actor: str, action: str, target: str = "-") -> None:
        actor, action, target = _text(actor, "Actor", maximum=128), _text(action, "Acción", maximum=128), _text(target, "Destino", maximum=128)
        now = utc_now()
        with self._transaction() as connection:
            self._audit(connection, actor, action, target, "denied", {}, now)

    def save_conversation(self, phone: str, state: dict, *, expires_at: str, now: str | None = None) -> None:
        phone, now, expires_at = _phone(phone), _timestamp(now), _timestamp(expires_at)
        if not isinstance(state, dict):
            raise TypeError("El estado debe ser un objeto.")
        serialized = _json(state, max_bytes=16_384)
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO conversations(phone,state_json,updated_at,expires_at) VALUES(?,?,?,?) "
                "ON CONFLICT(phone) DO UPDATE SET state_json=excluded.state_json,updated_at=excluded.updated_at,expires_at=excluded.expires_at",
                (phone, serialized, now, expires_at),
            )

    def conversation(self, phone: str, *, now: str | None = None) -> dict | None:
        phone, now = _phone(phone), _timestamp(now)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT state_json FROM conversations WHERE phone=? AND expires_at>?", (phone, now)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def enqueue_webhook(self, event_id: str, normalized_payload: dict, *, received_at: str | None = None) -> bool:
        event_id = _text(event_id, "Identificador de evento", maximum=256)
        received_at = _timestamp(received_at)
        payload_json = _json(normalized_payload, max_bytes=32_768)
        with self._transaction() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO webhook_events(event_id,normalized_payload_json,received_at,status,attempts,finished_at,expires_at,error_code) "
                "VALUES(?,?,?,'queued',0,NULL,NULL,NULL)",
                (event_id, payload_json, received_at),
            )
            return cursor.rowcount == 1

    def admit_webhook_event(
        self, event_id: str, normalized_payload: dict, *, received_at: str | None = None,
        max_waiting: int = 10, deadline_seconds: int = 120,
    ) -> str:
        """Durably admit an inbound message under the per-person and FIFO limits."""
        event_id = _text(event_id, "Identificador de evento", maximum=256)
        received_at = _timestamp(received_at)
        payload_json = _json(normalized_payload, max_bytes=32_768)
        _positive_integer(max_waiting, "El límite de espera")
        if max_waiting > 10 or deadline_seconds != 120:
            raise ValueError("Los límites de la cola no coinciden con la política aprobada.")
        kind = normalized_payload.get("kind") if isinstance(normalized_payload, dict) else None
        if kind != "message":
            return "queued" if self.enqueue_webhook(event_id, normalized_payload, received_at=received_at) else "duplicate"
        sender = _phone(normalized_payload.get("sender"))
        text = normalized_payload.get("text", "")
        cancel_request = _is_cancel_request(text)
        expires = _plus_hours(received_at, 24)
        waiting_reply = "Ya tengo una consulta tuya en espera. Cuando la atienda, podrás enviarme otra."
        full_reply = "El asistente está recibiendo muchas consultas. Inténtalo en un momento, por favor."
        no_pending_reply = "No tienes una consulta pendiente para cancelar. ¿En qué más te puedo ayudar?"
        cancel_reply = "Listo, detuve la consulta antes de enviarte una respuesta."
        with self._transaction() as connection:
            if connection.execute("SELECT 1 FROM webhook_events WHERE event_id=?", (event_id,)).fetchone():
                return "duplicate"
            active = connection.execute(
                "SELECT job_id,status FROM assistant_jobs WHERE sender_phone=? "
                "AND status IN ('queued','running','cancel_requested')", (sender,),
            ).fetchone()
            connection.execute(
                "INSERT INTO webhook_events(event_id,normalized_payload_json,received_at,status,attempts,finished_at,expires_at,error_code) "
                "VALUES(?,?,?, ?,0,?,?,NULL)",
                (event_id, payload_json, received_at, "done" if active or cancel_request else "queued",
                 received_at if active or cancel_request else None, expires),
            )
            if cancel_request:
                if active is None:
                    self._queue_message_in_transaction(
                        connection, f"assistant:{event_id}:cancel-none", sender, no_pending_reply, received_at,
                    )
                    return "no_pending"
                if active["status"] == "queued":
                    connection.execute(
                        "UPDATE assistant_jobs SET status='cancelled',finished_at=?,cancel_event_id=?,retention_until=? "
                        "WHERE job_id=? AND status='queued'",
                        (received_at, event_id, expires, active["job_id"]),
                    )
                    connection.execute(
                        "UPDATE webhook_events SET status='done',finished_at=?,expires_at=? WHERE event_id=(SELECT event_id FROM assistant_jobs WHERE job_id=?)",
                        (received_at, expires, active["job_id"]),
                    )
                    self._queue_message_in_transaction(
                        connection, f"assistant:{event_id}:cancelled", sender, cancel_reply, received_at,
                    )
                    return "cancelled"
                if active["status"] == "cancel_requested":
                    return "cancel_requested"
                connection.execute(
                    "UPDATE assistant_jobs SET status='cancel_requested',cancel_event_id=? WHERE job_id=? AND status='running'",
                    (event_id, active["job_id"]),
                )
                return "cancel_requested"
            if active is not None:
                self._queue_message_in_transaction(
                    connection, f"assistant:{event_id}:already-pending", sender, waiting_reply, received_at,
                )
                return "already_pending"
            waiting = connection.execute(
                "SELECT COUNT(*) FROM assistant_jobs WHERE status='queued'"
            ).fetchone()[0]
            if waiting >= max_waiting:
                connection.execute(
                    "UPDATE webhook_events SET status='done',finished_at=? WHERE event_id=?",
                    (received_at, event_id),
                )
                self._queue_message_in_transaction(
                    connection, f"assistant:{event_id}:queue-full", sender, full_reply, received_at,
                )
                return "full"
            job_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO assistant_jobs(job_id,event_id,sender_phone,status,received_at,deadline_at,started_at,finished_at,cancel_event_id,worker_id,error_code,retention_until) "
                "VALUES(?,?,?,'queued',?,?,NULL,NULL,NULL,NULL,NULL,?)",
                (job_id, event_id, sender, received_at, _plus_seconds(received_at, deadline_seconds), expires),
            )
            return "queued"

    def claim_assistant_job(self, worker_id: str, *, now: str | None = None) -> dict | None:
        """Claim one FIFO message only if an execution slot is available."""
        worker_id, now = _text(worker_id, "Identificador del trabajador", maximum=128), _timestamp(now)
        expired_reply = "No alcancé a atender tu consulta a tiempo. Inténtalo de nuevo en un momento."
        with self._transaction() as connection:
            active = connection.execute(
                "SELECT COUNT(*) FROM assistant_jobs WHERE status IN ('running','cancel_requested')"
            ).fetchone()[0]
            if active >= 3:
                return None
            while True:
                row = connection.execute(
                    "SELECT j.job_id,j.event_id,j.sender_phone,j.received_at,j.deadline_at,e.normalized_payload_json "
                    "FROM assistant_jobs j JOIN webhook_events e USING(event_id) WHERE j.status='queued' "
                    "ORDER BY j.queue_sequence LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                if row["deadline_at"] <= now:
                    connection.execute(
                        "UPDATE assistant_jobs SET status='failed',finished_at=?,error_code='expired_before_start' WHERE job_id=? AND status='queued'",
                        (now, row["job_id"]),
                    )
                    connection.execute(
                        "UPDATE webhook_events SET status='failed',finished_at=?,expires_at=?,error_code='expired_before_start' WHERE event_id=?",
                        (now, _plus_hours(now, 24), row["event_id"]),
                    )
                    self._queue_message_in_transaction(
                        connection, f"assistant:{row['event_id']}:deadline", row["sender_phone"], expired_reply, now,
                    )
                    continue
                changed = connection.execute(
                    "UPDATE assistant_jobs SET status='running',started_at=?,worker_id=? WHERE job_id=? AND status='queued'",
                    (now, worker_id, row["job_id"]),
                ).rowcount
                if changed != 1:
                    raise H2StorageError("La consulta ya fue reclamada por otro trabajador.")
                connection.execute(
                    "UPDATE webhook_events SET status='processing',attempts=attempts+1 WHERE event_id=? AND status='queued'",
                    (row["event_id"],),
                )
                return {
                    "job_id": row["job_id"], "event_id": row["event_id"], "sender": row["sender_phone"],
                    "received_at": row["received_at"], "deadline_at": row["deadline_at"],
                    "payload": json.loads(row["normalized_payload_json"]),
                }

    def assistant_job_status(self, job_id: str) -> dict | None:
        job_id = _text(job_id, "Identificador de consulta", maximum=64)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT job_id,event_id,sender_phone,status,received_at,deadline_at,started_at,finished_at,error_code "
                "FROM assistant_jobs WHERE job_id=?", (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def assistant_queue_snapshot(self) -> dict[str, int]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT status,COUNT(*) count FROM assistant_jobs GROUP BY status").fetchall()
        counts = {row["status"]: int(row["count"]) for row in rows}
        return {
            "active": counts.get("running", 0) + counts.get("cancel_requested", 0),
            "waiting": counts.get("queued", 0),
            "cancel_requested": counts.get("cancel_requested", 0),
            "uncertain": counts.get("uncertain", 0),
        }

    def assistant_job_cancel_requested(self, job_id: str) -> bool:
        job_id = _text(job_id, "Identificador de consulta", maximum=64)
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT status FROM assistant_jobs WHERE job_id=?", (job_id,)).fetchone()
        return bool(row and row["status"] == "cancel_requested")

    def complete_assistant_job(self, job_id: str, reply: str, *, now: str | None = None) -> bool:
        job_id, reply, now = _text(job_id, "Identificador de consulta", maximum=64), _text(reply, "Respuesta", maximum=4096), _timestamp(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT event_id,sender_phone,status FROM assistant_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None or row["status"] != "running":
                return False
            connection.execute(
                "UPDATE assistant_jobs SET status='completed',finished_at=? WHERE job_id=? AND status='running'",
                (now, job_id),
            )
            connection.execute(
                "UPDATE webhook_events SET status='done',finished_at=?,expires_at=? WHERE event_id=? AND status='processing'",
                (now, _plus_hours(now, 24), row["event_id"]),
            )
            self._queue_message_in_transaction(
                connection, f"assistant:{row['event_id']}:reply", row["sender_phone"], reply, now,
            )
            return True

    def complete_assistant_cancel(self, job_id: str, *, now: str | None = None) -> bool:
        job_id, now = _text(job_id, "Identificador de consulta", maximum=64), _timestamp(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT event_id,sender_phone,cancel_event_id,status FROM assistant_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None or row["status"] != "cancel_requested" or not row["cancel_event_id"]:
                return False
            connection.execute(
                "UPDATE assistant_jobs SET status='cancelled',finished_at=? WHERE job_id=? AND status='cancel_requested'",
                (now, job_id),
            )
            connection.execute(
                "UPDATE webhook_events SET status='done',finished_at=?,expires_at=? WHERE event_id=?",
                (now, _plus_hours(now, 24), row["event_id"]),
            )
            self._queue_message_in_transaction(
                connection, f"assistant:{row['cancel_event_id']}:cancelled", row["sender_phone"],
                "Listo, detuve la consulta antes de enviarte una respuesta.", now,
            )
            return True

    def fail_assistant_job(self, job_id: str, error_code: str, *, now: str | None = None) -> bool:
        job_id, error_code, now = (
            _text(job_id, "Identificador de consulta", maximum=64),
            _text(error_code, "Código de error", maximum=64), _timestamp(now),
        )
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT event_id,sender_phone,status FROM assistant_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None or row["status"] != "running":
                return False
            connection.execute(
                "UPDATE assistant_jobs SET status='failed',finished_at=?,error_code=? WHERE job_id=? AND status='running'",
                (now, error_code, job_id),
            )
            connection.execute(
                "UPDATE webhook_events SET status='failed',finished_at=?,expires_at=?,error_code=? WHERE event_id=?",
                (now, _plus_hours(now, 24), error_code, row["event_id"]),
            )
            self._queue_message_in_transaction(
                connection, f"assistant:{row['event_id']}:failed",
                row["sender_phone"], "No pude completar la consulta. Inténtalo de nuevo en un momento o pregúntale al personal.", now,
            )
            return True

    def recover_assistant_jobs(self, *, now: str | None = None) -> int:
        """Keep waiting jobs; interrupted active queries become uncertain, never replayed."""
        now = _timestamp(now)
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT job_id,event_id,sender_phone,status,cancel_event_id FROM assistant_jobs "
                "WHERE status IN ('running','cancel_requested') ORDER BY queue_sequence"
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE assistant_jobs SET status='uncertain',finished_at=?,error_code='process_interrupted' WHERE job_id=?",
                    (now, row["job_id"]),
                )
                connection.execute(
                    "UPDATE webhook_events SET status='failed',finished_at=?,expires_at=?,error_code='process_interrupted' WHERE event_id=?",
                    (now, _plus_hours(now, 24), row["event_id"]),
                )
                if row["cancel_event_id"]:
                    body = "La conexión se interrumpió antes de confirmar la cancelación. No puedo asegurar si se detuvo; puedes volver a intentarlo."
                    dedupe = f"assistant:{row['cancel_event_id']}:cancel-uncertain"
                else:
                    body = "La conexión se interrumpió y no pude confirmar el resultado de tu consulta. Inténtalo de nuevo en un momento."
                    dedupe = f"assistant:{row['event_id']}:interrupted"
                self._queue_message_in_transaction(connection, dedupe, row["sender_phone"], body, now)
            return len(rows)

    def finish_webhook_event(self, event_id: str, result: str, *, now: str | None = None, error_code: str | None = None) -> None:
        event_id, now = _text(event_id, "Identificador de evento", maximum=256), _timestamp(now)
        if result not in {"done", "failed"}:
            raise ValueError("El resultado del webhook no es válido.")
        if error_code is not None:
            error_code = _text(error_code, "Código de error", maximum=64)
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE webhook_events SET status=?,finished_at=?,expires_at=?,error_code=? "
                "WHERE event_id=? AND status IN ('queued','processing')",
                (result, now, _plus_hours(now, 24), error_code, event_id),
            )
            if cursor.rowcount != 1:
                raise H2StorageError("El evento ya se resolvió o no existe.")

    def claim_webhook_event(self, *, now: str | None = None) -> dict | None:
        """Claim a non-conversation webhook event such as a delivery receipt."""
        now = _timestamp(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT event_id,normalized_payload_json,received_at,attempts FROM webhook_events "
                "WHERE status='queued' AND NOT EXISTS(SELECT 1 FROM assistant_jobs j WHERE j.event_id=webhook_events.event_id) "
                "ORDER BY received_at,event_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                "UPDATE webhook_events SET status='processing',attempts=attempts+1 "
                "WHERE event_id=? AND status='queued'", (row["event_id"],)
            )
            if cursor.rowcount != 1:
                raise H2StorageError("El evento ya fue reclamado por otro trabajador.")
            return {
                "event_id": row["event_id"],
                "payload": json.loads(row["normalized_payload_json"]),
                "received_at": row["received_at"],
                "attempts": row["attempts"] + 1,
            }

    def recover_webhook_events(self) -> int:
        """Requeue interrupted idempotent non-conversation events only."""
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE webhook_events SET status='queued' WHERE status='processing' "
                "AND NOT EXISTS(SELECT 1 FROM assistant_jobs j WHERE j.event_id=webhook_events.event_id)"
            )
            return cursor.rowcount

    def enqueue_message(
        self, dedupe_key: str, recipient: str, body: str, *, now: str | None = None,
        expires_at: str | None = None,
    ) -> str:
        dedupe_key = _text(dedupe_key, "Clave de idempotencia", maximum=256)
        recipient = _phone(recipient)
        body = _text(body, "Mensaje", maximum=4096)
        now = _timestamp(now)
        expires_at = _timestamp(expires_at) if expires_at is not None else _plus_hours(now, 24)
        message_id = uuid.uuid4().hex
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO outbox(outbox_id,dedupe_key,recipient_phone,message_text,status,attempts,created_at,next_attempt_at,sent_at,expires_at,error_code,retention_until) "
                "VALUES(?,?,?,?,'queued',0,?,?,NULL,?,NULL,?)",
                (message_id, dedupe_key, recipient, body, now, now, expires_at, expires_at),
            )
        return message_id

    def claim_outbox(self, *, now: str | None = None) -> dict | None:
        now = _timestamp(now)
        with self._transaction() as connection:
            connection.execute(
                "UPDATE outbox SET status='failed',error_code='expired',retention_until=COALESCE(retention_until,expires_at) "
                "WHERE status='queued' AND expires_at IS NOT NULL AND expires_at<=?", (now,),
            )
            row = connection.execute(
                "SELECT outbox_id,recipient_phone,message_text,attempts FROM outbox "
                "WHERE status='queued' AND next_attempt_at<=? AND expires_at>? ORDER BY created_at,outbox_id LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE outbox SET status='sending',attempts=attempts+1 WHERE outbox_id=? AND status='queued'",
                (row["outbox_id"],),
            )
            return dict(row)

    def finish_outbox(
        self, message_id: str, result: str, *, now: str | None = None,
        error_code: str | None = None, provider_message_id: str | None = None,
    ) -> None:
        now = _timestamp(now)
        if result not in {"sent", "uncertain", "failed"}:
            raise ValueError("Resultado de envío no válido.")
        if error_code is not None:
            error_code = _text(error_code, "Código de error", maximum=64)
        if provider_message_id is not None:
            provider_message_id = _text(provider_message_id, "Identificador del proveedor", maximum=256)
            if not provider_message_id.startswith("wamid."):
                raise ValueError("El identificador del proveedor no es válido.")
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE outbox SET status=?,sent_at=?,error_code=?,retention_until=?,provider_message_id=COALESCE(?,provider_message_id) "
                "WHERE outbox_id=? AND status='sending'",
                (result, now if result == "sent" else None, error_code, _plus_hours(now, 24), provider_message_id, message_id),
            )
            if cursor.rowcount != 1:
                raise H2StorageError("El envío ya fue resuelto o no existe; no se repetirá a ciegas.")

    def recover_uncertain_outbox(self, *, now: str | None = None) -> int:
        """Mark sends interrupted by restart as uncertain; never blindly resend."""
        now = _timestamp(now)
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE outbox SET status='uncertain',error_code='interrupted',retention_until=? WHERE status='sending'",
                (_plus_hours(now, 24),),
            )
            return cursor.rowcount

    def apply_delivery_status(self, provider_message_id: str, status: str, *, now: str | None = None) -> bool:
        """Apply monotonic Meta delivery receipts; late receipts never downgrade state."""
        provider_message_id = _text(provider_message_id, "Identificador del proveedor", maximum=256)
        if not provider_message_id.startswith("wamid.") or status not in {"sent", "delivered", "read", "failed"}:
            raise ValueError("El estado de entrega no es válido.")
        _timestamp(now)
        rank = {"sent": 1, "delivered": 2, "read": 3, "failed": 1}[status]
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT delivery_status FROM outbox WHERE provider_message_id=?", (provider_message_id,)
            ).fetchone()
            if row is None:
                return False
            current = row["delivery_status"]
            current_rank = {"sent": 1, "delivered": 2, "read": 3, "failed": 1}.get(current, 0)
            if current == "failed" or rank < current_rank:
                return True
            connection.execute(
                "UPDATE outbox SET delivery_status=? WHERE provider_message_id=?",
                (status, provider_message_id),
            )
            return True

    def purge_expired(self, *, now: str | None = None) -> dict[str, int]:
        """Redact conversational/contact content after the approved 24-hour window."""
        now = _timestamp(now)
        with self._transaction() as connection:
            conversations = connection.execute("DELETE FROM conversations WHERE expires_at<=?", (now,)).rowcount
            auth_codes = connection.execute("DELETE FROM auth_codes WHERE expires_at<=? OR used_at IS NOT NULL", (now,)).rowcount
            sessions = connection.execute(
                "DELETE FROM staff_sessions WHERE expires_at<=? OR revoked_at IS NOT NULL", (now,)
            ).rowcount
            events = connection.execute(
                "UPDATE webhook_events SET normalized_payload_json='{}' WHERE status IN ('done','failed') AND expires_at<=? AND normalized_payload_json<>'{}'",
                (now,),
            ).rowcount
            legacy_queued = connection.execute(
                "SELECT outbox_id,created_at FROM outbox WHERE status='queued' AND expires_at IS NULL"
            ).fetchall()
            for row in legacy_queued:
                expiry = _plus_hours(row["created_at"], 24)
                connection.execute(
                    "UPDATE outbox SET expires_at=?,retention_until=COALESCE(retention_until,?) "
                    "WHERE outbox_id=? AND status='queued' AND expires_at IS NULL",
                    (expiry, expiry, row["outbox_id"]),
                )
            outbox_expired = connection.execute(
                "UPDATE outbox SET status='failed',error_code='expired',retention_until=COALESCE(retention_until,expires_at) "
                "WHERE status='queued' AND expires_at<=?", (now,),
            ).rowcount
            outbox = connection.execute(
                "UPDATE outbox SET recipient_phone='',message_text='',provider_message_id=NULL WHERE status IN ('sent','uncertain','failed') AND retention_until<=? AND (recipient_phone<>'' OR message_text<>'' OR provider_message_id IS NOT NULL)",
                (now,),
            ).rowcount
            orders = connection.execute(
                "UPDATE orders SET customer_phone='',items_json='[]' WHERE status IN ('delivered','rejected','expired','cancelled') AND retention_until<=? AND (customer_phone<>'' OR items_json<>'[]')",
                (now,),
            ).rowcount
            connection.execute(
                "UPDATE tickets SET status='expired' WHERE source='pos_receipt' AND status='needs_review' AND expires_at<=?",
                (now,),
            )
            tickets = connection.execute(
                "UPDATE tickets SET sender_phone='',lines_json='[]',media_reference=NULL,fingerprint='' WHERE expires_at<=? AND (sender_phone<>'' OR lines_json<>'[]' OR media_reference IS NOT NULL OR fingerprint<>'')",
                (now,),
            ).rowcount
            jobs = connection.execute(
                "UPDATE assistant_jobs SET sender_phone='' WHERE status IN ('completed','failed','cancelled','uncertain') "
                "AND retention_until<=? AND sender_phone<>''", (now,),
            ).rowcount
        return {"conversations": conversations, "auth_codes": auth_codes, "sessions": sessions,
                "events": events, "outbox": outbox, "outbox_expired": outbox_expired,
                "orders": orders, "tickets": tickets, "assistant_jobs": jobs}

    def create_order(
        self, *, folio: str, idempotency_key: str, customer_phone: str, items: list[dict],
        total_cents: int, expires_at: str, now: str | None = None,
    ) -> str:
        folio = _text(folio, "Folio", maximum=32)
        idempotency_key = _text(idempotency_key, "Clave de idempotencia", maximum=128)
        customer_phone, now, expires_at = _phone(customer_phone), _timestamp(now), _timestamp(expires_at)
        total_cents = _positive_integer(total_cents, "Total", allow_zero=True)
        if not isinstance(items, list) or not items or len(items) > 30:
            raise ValueError("El pedido debe incluir entre una y treinta líneas.")
        for item in items:
            allowed_item_keys = {
                "product_id", "variant", "quantity", "modifier_ids", "product_name",
                "unit_price_cents", "modifier_snapshot", "line_total_cents",
            }
            if not isinstance(item, dict) or set(item) - allowed_item_keys:
                raise ValueError("La línea del pedido no cumple el esquema permitido.")
            _text(item.get("product_id"), "Producto", maximum=128)
            _text(item.get("variant"), "Tamaño", maximum=64)
            _positive_integer(item.get("quantity"), "Cantidad")
            modifiers = item.get("modifier_ids", [])
            if not isinstance(modifiers, list) or len(modifiers) > 3 or any(
                not isinstance(value, str) or not value or len(value) > 128 for value in modifiers
            ):
                raise ValueError("Los extras del pedido no son válidos.")
            if "product_name" in item:
                _text(item["product_name"], "Nombre del producto", maximum=256)
            for field in ("unit_price_cents", "line_total_cents"):
                if field in item:
                    _positive_integer(item[field], "Precio", allow_zero=True)
            if "modifier_snapshot" in item:
                snapshots = item["modifier_snapshot"]
                if not isinstance(snapshots, list) or len(snapshots) != len(modifiers):
                    raise ValueError("La instantánea de extras no coincide con el pedido.")
                for snapshot in snapshots:
                    if (not isinstance(snapshot, dict)
                            or set(snapshot) not in ({"id", "name", "price_cents"},
                                                    {"id", "name", "order_label", "price_cents"})):
                        raise ValueError("La instantánea de extras no es válida.")
                    _text(snapshot["id"], "Extra", maximum=128)
                    _text(snapshot["name"], "Nombre del extra", maximum=128)
                    if "order_label" in snapshot:
                        _text(snapshot["order_label"], "Descripción del modificador", maximum=128)
                    _positive_integer(snapshot["price_cents"], "Precio del extra", allow_zero=True)
        items_json = _json(items, max_bytes=16_384)
        if all("line_total_cents" in item for item in items) and sum(item["line_total_cents"] for item in items) != total_cents:
            raise ValueError("El total del pedido no coincide con sus líneas guardadas.")
        order_id = uuid.uuid4().hex
        fingerprint = hashlib.sha256(
            _json({"folio": folio, "phone": customer_phone, "items": items, "total": total_cents}).encode()
        ).hexdigest()
        with self._transaction() as connection:
            prior = connection.execute(
                "SELECT order_id,request_fingerprint FROM orders WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if prior:
                if prior["request_fingerprint"] != fingerprint:
                    raise DuplicateConflict("La clave de pedido ya se usó con otros datos.")
                return prior["order_id"]
            connection.execute(
                "INSERT INTO orders(order_id,folio,idempotency_key,request_fingerprint,customer_phone,items_json,total_cents,status,created_at,expires_at,accepted_at) "
                "VALUES(?,?,?,?,?,?,?,'pending_staff',?,?,NULL)",
                (order_id, folio, idempotency_key, fingerprint, customer_phone, items_json, total_cents, now, expires_at),
            )
            customer_ref = hashlib.sha256(customer_phone.encode()).hexdigest()[:16]
            self._audit(connection, f"customer:{customer_ref}", "order_created", folio, "pending_staff", {"total_cents": total_cents}, now)
        return order_id

    def active_order_staff(self) -> dict:
        """Return only role counts, never staff phone numbers."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT role,COUNT(*) AS count FROM staff WHERE active=1 AND role IN ('admin','cashier') GROUP BY role"
            ).fetchall()
        counts = {row["role"]: row["count"] for row in rows}
        return {"admins": counts.get("admin", 0), "cashiers": counts.get("cashier", 0)}

    def submit_order(
        self, *, folio: str, idempotency_key: str, customer_phone: str, items: list[dict],
        total_cents: int, expires_at: str, staff_notice: str, now: str | None = None,
    ) -> dict:
        """Persist a confirmed request and its first assignment before notifying staff."""
        now = _timestamp(now)
        staff_notice = _text(staff_notice, "Aviso al personal", maximum=4096)
        order_id = self.create_order(
            folio=folio, idempotency_key=idempotency_key, customer_phone=customer_phone,
            items=items, total_cents=total_cents, expires_at=expires_at, now=now,
        )
        with self._transaction() as connection:
            order = connection.execute(
                "SELECT folio,status,customer_phone,items_json,created_at,expires_at FROM orders WHERE order_id=?", (order_id,)
            ).fetchone()
            if order is None:
                raise H2StorageError("No se encontró el pedido recién confirmado.")
            if order["status"] != "pending_staff":
                return {"order_id": order_id, "folio": order["folio"], "status": order["status"], "created": False}
            connection.execute(
                "INSERT OR IGNORE INTO tickets(ticket_id,source,sender_phone,status,lines_json,media_reference,created_at,expires_at,reviewed_at,reviewed_by,fingerprint) "
                "VALUES(?,'whatsapp_order',?,'needs_review',?,NULL,?,?,NULL,NULL,?)",
                (f"wa-{order_id}", order["customer_phone"], order["items_json"], order["created_at"],
                 _plus_hours(order["created_at"], 24), hashlib.sha256(order["items_json"].encode()).hexdigest()),
            )
            active = connection.execute(
                "SELECT round_no FROM order_rounds WHERE order_id=? AND status='active'", (order_id,)
            ).fetchone()
            if active is None:
                assignment = self._open_initial_round(connection, order_id, order["folio"], staff_notice, now)
            else:
                recipients = connection.execute(
                    "SELECT staff_phone FROM order_recipients WHERE order_id=? AND round_no=? ORDER BY staff_phone",
                    (order_id, active["round_no"]),
                ).fetchall()
                assignment = [row["staff_phone"] for row in recipients]
            return {"order_id": order_id, "folio": order["folio"], "status": "pending_staff",
                    "assigned_count": len(assignment), "created": active is None}

    def _open_initial_round(self, connection: sqlite3.Connection, order_id: str, folio: str, notice: str, now: str) -> list[str]:
        admins = connection.execute("SELECT phone FROM staff WHERE active=1 AND role='admin' ORDER BY created_at,phone").fetchall()
        if not admins:
            raise H2StorageError("No hay un administrador de respaldo activo para recibir el pedido.")
        cashiers = self._cashier_candidates(connection, order_id, set())
        if cashiers:
            cashier = cashiers[0]["phone"]
            phase = "cashier"
            deadline = _plus_minutes(now, 5 if len(cashiers) > 1 else 10)
            recipients = [(cashier, "cashier")]
        else:
            phase = "escalation"
            deadline = _plus_minutes(now, 10)
            recipients = [(admins[0]["phone"], "admin")]
        self._insert_round(connection, order_id, 1, phase, now, deadline, recipients, folio, notice)
        self._audit(connection, "system:orders", "order_routed", folio, phase,
                    {"round": 1, "recipient_count": len(recipients)}, now)
        return [phone for phone, _ in recipients]

    @staticmethod
    def _cashier_candidates(connection: sqlite3.Connection, order_id: str, excluded: set[str]) -> list[dict]:
        rows = connection.execute(
            "SELECT s.phone,COUNT(DISTINCT CASE WHEN o.status='pending_staff' AND rr.status='active' AND r.response_at IS NULL THEN o.order_id END) AS load "
            "FROM staff s LEFT JOIN order_recipients r ON r.staff_phone=s.phone "
            "LEFT JOIN order_rounds rr ON rr.order_id=r.order_id AND rr.round_no=r.round_no AND rr.status='active' "
            "LEFT JOIN orders o ON o.order_id=r.order_id AND o.status='pending_staff' "
            "WHERE s.active=1 AND s.role='cashier' GROUP BY s.phone ORDER BY load,s.phone"
        ).fetchall()
        candidates = [{"phone": row["phone"], "load": row["load"]} for row in rows if row["phone"] not in excluded]
        if not candidates:
            return []
        lightest = candidates[0]["load"]
        tied = [candidate for candidate in candidates if candidate["load"] == lightest]
        selected = secrets.choice(tied)
        return [selected] + [candidate for candidate in candidates if candidate is not selected]

    def _insert_round(
        self, connection: sqlite3.Connection, order_id: str, round_no: int, phase: str,
        opened_at: str, deadline: str, recipients: list[tuple[str, str]], folio: str, notice: str,
    ) -> None:
        if not recipients:
            raise H2StorageError("No hay destinatarios activos para el pedido.")
        connection.execute(
            "INSERT INTO order_rounds(order_id,round_no,phase,opened_at,deadline,status) VALUES(?,?,?,?,?,'active')",
            (order_id, round_no, phase, opened_at, deadline),
        )
        for phone, role in recipients:
            connection.execute(
                "INSERT INTO order_recipients(order_id,round_no,staff_phone,role,notified_at,response_at,result) VALUES(?,?,?,?,?,NULL,NULL)",
                (order_id, round_no, phone, role, opened_at),
            )
            if role == "cashier":
                sequence = connection.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM order_assignments WHERE order_id=?", (order_id,)
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO order_assignments(order_id,sequence,staff_phone,assigned_at,deadline,response_at,result) VALUES(?,?,?,?,?,NULL,NULL)",
                    (order_id, sequence, phone, opened_at, deadline),
                )
            staff_ref = hashlib.sha256(phone.encode()).hexdigest()[:16]
            self._queue_message_in_transaction(
                connection, f"order:{folio}:assigned:{round_no}:{staff_ref}", phone, notice, opened_at,
                expires_at=deadline,
            )

    @staticmethod
    def _queue_message_in_transaction(
        connection: sqlite3.Connection, dedupe_key: str, recipient: str, body: str, now: str,
        *, expires_at: str | None = None,
    ) -> None:
        expires_at = _timestamp(expires_at) if expires_at is not None else _plus_hours(now, 24)
        connection.execute(
            "INSERT OR IGNORE INTO outbox(outbox_id,dedupe_key,recipient_phone,message_text,status,attempts,created_at,next_attempt_at,sent_at,expires_at,error_code,retention_until) "
            "VALUES(?,?,?,?,'queued',0,?,?,NULL,?,NULL,?)",
            (uuid.uuid4().hex, dedupe_key, recipient, body, now, now, expires_at, expires_at),
        )

    def pass_order(self, order_id: str, actor_phone: str, *, notice: str, now: str | None = None) -> dict:
        """Pass an assignment forward; escalated recipients share one atomic round."""
        order_id, actor_phone = _text(order_id, "Pedido", maximum=64), _phone(actor_phone)
        now, notice = _timestamp(now), _text(notice, "Aviso al siguiente", maximum=4096)
        with self._transaction() as connection:
            order = connection.execute(
                "SELECT folio,status,expires_at FROM orders WHERE order_id=?", (order_id,)
            ).fetchone()
            staff = connection.execute("SELECT role,active FROM staff WHERE phone=?", (actor_phone,)).fetchone()
            current = connection.execute(
                "SELECT rr.round_no,rr.phase,rr.deadline FROM order_rounds rr WHERE rr.order_id=? AND rr.status='active' "
                "ORDER BY rr.round_no DESC LIMIT 1", (order_id,),
            ).fetchone()
            recipient = connection.execute(
                "SELECT response_at FROM order_recipients WHERE order_id=? AND round_no=? AND staff_phone=?",
                (order_id, current["round_no"], actor_phone),
            ).fetchone() if current else None
            if (order is None or order["status"] != "pending_staff" or staff is None or not staff["active"]
                    or staff["role"] not in {"admin", "cashier"} or current is None
                    or current["deadline"] <= now or order["expires_at"] <= now or recipient is None
                    or recipient["response_at"] is not None):
                raise H2StorageError("El pedido ya cambió o no tienes una asignación vigente.")
            if staff["role"] == "admin" and current["phase"] != "escalation":
                raise H2StorageError("El administrador solo puede pasar el pedido durante la escalación.")
            if staff["role"] == "cashier" and current["phase"] == "escalation":
                connection.execute(
                    "UPDATE order_recipients SET response_at=?,result='passed' WHERE order_id=? AND round_no=? AND staff_phone=? AND response_at IS NULL",
                    (now, order_id, current["round_no"], actor_phone),
                )
                connection.execute(
                    "UPDATE order_assignments SET response_at=?,result='passed' WHERE order_id=? AND staff_phone=? AND response_at IS NULL",
                    (now, order_id, actor_phone),
                )
                attempted = {row[0] for row in connection.execute(
                    "SELECT DISTINCT staff_phone FROM order_recipients WHERE order_id=? AND role='cashier'", (order_id,)
                ).fetchall()}
                next_cashier = next((row["phone"] for row in self._cashier_candidates(connection, order_id, attempted)), None)
                if next_cashier:
                    self._insert_round_recipient(connection, order_id, current["round_no"], next_cashier, "cashier", now,
                                                 current["deadline"], order["folio"], notice)
            elif staff["role"] == "admin":
                connection.execute(
                    "UPDATE order_recipients SET response_at=?,result='passed' WHERE order_id=? AND round_no=? AND staff_phone=? AND response_at IS NULL",
                    (now, order_id, current["round_no"], actor_phone),
                )
                attempted = {row[0] for row in connection.execute(
                    "SELECT DISTINCT staff_phone FROM order_recipients WHERE order_id=? AND role='cashier'", (order_id,)
                ).fetchall()}
                next_cashier = next((row["phone"] for row in self._cashier_candidates(connection, order_id, attempted)), None)
                if next_cashier:
                    self._insert_round_recipient(connection, order_id, current["round_no"], next_cashier, "cashier", now,
                                                 current["deadline"], order["folio"], notice)
                else:
                    raise H2StorageError("No queda otro cajero sin probar; el pedido sigue con el administrador hasta su vencimiento.")
            else:
                connection.execute(
                    "UPDATE order_recipients SET response_at=?,result='passed' WHERE order_id=? AND round_no=? AND staff_phone=? AND response_at IS NULL",
                    (now, order_id, current["round_no"], actor_phone),
                )
                connection.execute(
                    "UPDATE order_assignments SET response_at=?,result='passed' WHERE order_id=? AND staff_phone=? AND response_at IS NULL",
                    (now, order_id, actor_phone),
                )
                connection.execute("UPDATE order_rounds SET status='closed' WHERE order_id=? AND round_no=?",
                                   (order_id, current["round_no"]))
                next_info = self._next_after_cashier_pass(connection, order_id, order["folio"], notice, now)
                connection.execute(
                    "UPDATE orders SET expires_at=? WHERE order_id=? AND status='pending_staff' AND expires_at<?",
                    (next_info["deadline"], order_id, next_info["deadline"]),
                )
            self._audit(connection, actor_phone, "order_passed", order["folio"], "ok", {"round": current["round_no"]}, now)
            updated = connection.execute(
                "SELECT round_no,phase,deadline FROM order_rounds WHERE order_id=? AND status='active' ORDER BY round_no DESC LIMIT 1",
                (order_id,),
            ).fetchone()
            return {"folio": order["folio"], "round": updated["round_no"] if updated else current["round_no"],
                    "phase": updated["phase"] if updated else current["phase"],
                    "deadline": updated["deadline"] if updated else current["deadline"]}

    def _insert_round_recipient(
        self, connection: sqlite3.Connection, order_id: str, round_no: int, phone: str, role: str,
        now: str, deadline: str, folio: str, notice: str,
    ) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO order_recipients(order_id,round_no,staff_phone,role,notified_at,response_at,result) VALUES(?,?,?,?,?,NULL,NULL)",
            (order_id, round_no, phone, role, now),
        )
        if role == "cashier":
            sequence = connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM order_assignments WHERE order_id=?",
                                          (order_id,)).fetchone()[0]
            connection.execute(
                "INSERT INTO order_assignments(order_id,sequence,staff_phone,assigned_at,deadline,response_at,result) VALUES(?,?,?,?,?,NULL,NULL)",
                (order_id, sequence, phone, now, deadline),
            )
        staff_ref = hashlib.sha256(phone.encode()).hexdigest()[:16]
        self._queue_message_in_transaction(
            connection, f"order:{folio}:assigned:{round_no}:{staff_ref}", phone, notice, now,
            expires_at=deadline,
        )

    def _next_after_cashier_pass(self, connection: sqlite3.Connection, order_id: str, folio: str, notice: str, now: str) -> dict:
        attempted = {row[0] for row in connection.execute(
            "SELECT DISTINCT staff_phone FROM order_recipients WHERE order_id=? AND role='cashier'", (order_id,)
        ).fetchall()}
        admins = connection.execute("SELECT phone FROM staff WHERE active=1 AND role='admin' ORDER BY created_at,phone").fetchall()
        if not admins:
            raise H2StorageError("No hay un administrador de respaldo activo.")
        next_cashier = next((row["phone"] for row in self._cashier_candidates(connection, order_id, attempted)), None)
        if len(attempted) < 2 and next_cashier:
            round_no = connection.execute("SELECT COALESCE(MAX(round_no),0)+1 FROM order_rounds WHERE order_id=?", (order_id,)).fetchone()[0]
            deadline = _plus_minutes(now, 5)
            self._insert_round(connection, order_id, round_no, "cashier", now, deadline,
                               [(next_cashier, "cashier")], folio, notice)
            return {"deadline": deadline}
        round_no = connection.execute("SELECT COALESCE(MAX(round_no),0)+1 FROM order_rounds WHERE order_id=?", (order_id,)).fetchone()[0]
        deadline = _plus_minutes(now, 10)
        recipients = [(admins[0]["phone"], "admin")]
        if next_cashier:
            recipients.insert(0, (next_cashier, "cashier"))
        self._insert_round(connection, order_id, round_no, "escalation", now, deadline, recipients, folio, notice)
        return {"deadline": deadline}

    def advance_expired_order_assignments(self, *, now: str | None = None, notice_factory=None) -> int:
        """Route timed-out cashier turns or expire final escalation; safe to call repeatedly."""
        now = _timestamp(now)
        changed = 0
        with self._transaction() as connection:
            due = connection.execute(
                "SELECT o.order_id,o.folio,o.customer_phone,o.status,rr.round_no,rr.phase,rr.deadline "
                "FROM orders o JOIN order_rounds rr ON rr.order_id=o.order_id AND rr.status='active' "
                "WHERE o.status='pending_staff' AND rr.deadline<=? ORDER BY rr.deadline,o.order_id", (now,),
            ).fetchall()
            for row in due:
                connection.execute("UPDATE order_rounds SET status='closed' WHERE order_id=? AND round_no=? AND status='active'",
                                   (row["order_id"], row["round_no"]))
                connection.execute(
                    "UPDATE order_recipients SET response_at=COALESCE(response_at,?),result=COALESCE(result,'timed_out') WHERE order_id=? AND round_no=?",
                    (now, row["order_id"], row["round_no"]),
                )
                connection.execute(
                    "UPDATE order_assignments SET response_at=COALESCE(response_at,?),result=COALESCE(result,'timed_out') "
                    "WHERE order_id=? AND staff_phone IN (SELECT staff_phone FROM order_recipients WHERE order_id=? AND round_no=?) AND response_at IS NULL",
                    (now, row["order_id"], row["order_id"], row["round_no"]),
                )
                if row["phase"] == "cashier":
                    notice = notice_factory(row["folio"]) if notice_factory else f"Pedido {row['folio']}: requiere revisión."
                    next_info = self._next_after_cashier_pass(connection, row["order_id"], row["folio"], notice, now)
                    connection.execute("UPDATE orders SET expires_at=? WHERE order_id=? AND expires_at<?",
                                       (next_info["deadline"], row["order_id"], next_info["deadline"]))
                    self._audit(connection, "system:orders", "order_assignment_timed_out", row["folio"], "rerouted", {}, now)
                else:
                    connection.execute("UPDATE orders SET status='expired',retention_until=? WHERE order_id=? AND status='pending_staff'",
                                       (_plus_hours(now, 24), row["order_id"]))
                    connection.execute("UPDATE tickets SET status='expired',expires_at=? WHERE ticket_id=? AND source='whatsapp_order'",
                                       (_plus_hours(now, 24), f"wa-{row['order_id']}"))
                    self._queue_message_in_transaction(
                        connection, f"order:{row['folio']}:expired:customer", row["customer_phone"],
                        f"Tu pedido {row['folio']} no quedó confirmado y no se descontó inventario. Si quieres, puedo ayudarte con otra opción.", now,
                    )
                    self._audit(connection, "system:orders", "order_expired", row["folio"], "expired", {}, now)
                changed += 1
        return changed

    def reject_order(self, order_id: str, actor_phone: str, *, customer_message: str, now: str | None = None) -> None:
        order_id, actor_phone = _text(order_id, "Pedido", maximum=64), _phone(actor_phone)
        now, customer_message = _timestamp(now), _text(customer_message, "Aviso al cliente", maximum=4096)
        with self._transaction() as connection:
            order = connection.execute("SELECT folio,customer_phone,status FROM orders WHERE order_id=?", (order_id,)).fetchone()
            staff = connection.execute("SELECT role,active FROM staff WHERE phone=?", (actor_phone,)).fetchone()
            current = connection.execute(
                "SELECT rr.round_no,rr.phase,rr.deadline FROM order_rounds rr WHERE rr.order_id=? AND rr.status='active' ORDER BY rr.round_no DESC LIMIT 1",
                (order_id,),
            ).fetchone()
            recipient = connection.execute(
                "SELECT 1 FROM order_recipients WHERE order_id=? AND round_no=? AND staff_phone=? AND response_at IS NULL",
                (order_id, current["round_no"], actor_phone),
            ).fetchone() if current else None
            if (order is None or order["status"] != "pending_staff" or staff is None or not staff["active"]
                    or staff["role"] != "admin" or current is None or current["phase"] != "escalation"
                    or current["deadline"] <= now or recipient is None):
                raise H2StorageError("Solo el administrador asignado durante la escalación puede rechazar el pedido.")
            connection.execute("UPDATE orders SET status='rejected',retention_until=? WHERE order_id=? AND status='pending_staff'",
                               (_plus_hours(now, 24), order_id))
            connection.execute("UPDATE order_rounds SET status='closed' WHERE order_id=? AND round_no=?", (order_id, current["round_no"]))
            connection.execute("UPDATE order_recipients SET response_at=COALESCE(response_at,?),result=COALESCE(result,'rejected') WHERE order_id=? AND round_no=?",
                               (now, order_id, current["round_no"]))
            connection.execute("UPDATE tickets SET status='rejected',expires_at=? WHERE ticket_id=? AND source='whatsapp_order'",
                               (_plus_hours(now, 24), f"wa-{order_id}"))
            self._queue_message_in_transaction(connection, f"order:{order['folio']}:rejected:customer",
                                               order["customer_phone"], customer_message, now)
            self._audit(connection, actor_phone, "order_rejected", order["folio"], "rejected", {}, now)

    def customer_order_status(self, customer_phone: str, folio: str | None = None) -> dict | None:
        customer_phone = _phone(customer_phone)
        if folio is not None:
            folio = _text(folio, "Folio", maximum=32)
        with closing(self._connect()) as connection:
            row = None
            if folio:
                row = connection.execute(
                    "SELECT folio,status FROM orders WHERE customer_phone=? AND folio=?", (customer_phone, folio)
                ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT folio,status FROM orders WHERE customer_phone=? ORDER BY created_at DESC,order_id DESC LIMIT 1",
                    (customer_phone,),
                ).fetchone()
        if row is None:
            return None
        labels = {"pending_staff": "pendiente de confirmación", "accepted": "aceptado", "preparing": "en preparación",
                  "ready": "listo para recoger", "delivered": "entregado", "rejected": "no aceptado",
                  "expired": "sin confirmar", "cancelled": "cancelado"}
        return {"folio": row["folio"], "status": labels.get(row["status"], "en revisión")}

    def order_for_staff_workflow(self, folio: str) -> dict | None:
        """Internal service lookup; callers must authorize before returning these fields."""
        folio = _text(folio, "Folio", maximum=32)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT order_id,folio,customer_phone,status,items_json,total_cents,created_at,expires_at,accepted_by_phone "
                "FROM orders WHERE folio=?", (folio,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["items"] = json.loads(result.pop("items_json"))
        return result

    def assign_order(self, order_id: str, staff_phone: str, sequence: int, *, deadline: str, now: str | None = None) -> int:
        order_id = _text(order_id, "Pedido", maximum=64)
        staff_phone, now, deadline = _phone(staff_phone), _timestamp(now), _timestamp(deadline)
        sequence = _positive_integer(sequence, "Secuencia")
        with self._transaction() as connection:
            order = connection.execute("SELECT status,folio FROM orders WHERE order_id=?", (order_id,)).fetchone()
            staff = connection.execute("SELECT role,active FROM staff WHERE phone=?", (staff_phone,)).fetchone()
            if order is None or order["status"] != "pending_staff" or staff is None or not staff["active"] or staff["role"] not in {"cashier", "admin"}:
                raise H2StorageError("No se puede asignar este pedido.")
            cursor = connection.execute(
                "INSERT INTO order_assignments(order_id,sequence,staff_phone,assigned_at,deadline,response_at,result) VALUES(?,?,?,?,?,NULL,NULL)",
                (order_id, sequence, staff_phone, now, deadline),
            )
            connection.execute(
                "INSERT INTO order_rounds(order_id,round_no,phase,opened_at,deadline,status) VALUES(?,?,?,?,?,'active')",
                (order_id, sequence, "cashier" if staff["role"] == "cashier" else "escalation", now, deadline),
            )
            connection.execute(
                "INSERT INTO order_recipients(order_id,round_no,staff_phone,role,notified_at,response_at,result) VALUES(?,?,?,?,?,NULL,NULL)",
                (order_id, sequence, staff_phone, staff["role"], now),
            )
            self._audit(connection, staff_phone, "order_assigned", order["folio"], "ok", {"sequence": sequence}, now)
            return int(cursor.lastrowid)

    def accept_order(
        self, order_id: str, actor_phone: str, consumption: list[dict] | None = None, *,
        customer_message: str, staff_message: str | None = None, now: str | None = None,
    ) -> None:
        order_id, actor_phone, now = _text(order_id, "Pedido", maximum=64), _phone(actor_phone), _timestamp(now)
        customer_message = _text(customer_message, "Aviso", maximum=4096)
        if staff_message is not None:
            staff_message = _text(staff_message, "Aviso al personal", maximum=4096)
        with self._transaction() as connection:
            order = connection.execute("SELECT folio,customer_phone,status,expires_at,items_json FROM orders WHERE order_id=?", (order_id,)).fetchone()
            staff = connection.execute("SELECT role,active FROM staff WHERE phone=?", (actor_phone,)).fetchone()
            current = connection.execute(
                "SELECT rr.round_no,rr.deadline FROM order_rounds rr WHERE rr.order_id=? AND rr.status='active' ORDER BY rr.round_no DESC LIMIT 1",
                (order_id,),
            ).fetchone()
            recipient = connection.execute(
                "SELECT role,response_at FROM order_recipients WHERE order_id=? AND round_no=? AND staff_phone=?",
                (order_id, current["round_no"], actor_phone),
            ).fetchone() if current else None
            if order is None or order["status"] != "pending_staff" or order["expires_at"] <= now or staff is None or not staff["active"]:
                raise H2StorageError("El pedido no está pendiente o el actor no está autorizado.")
            if (staff["role"] not in {"cashier", "admin"} or current is None
                    or current["deadline"] <= now or recipient is None
                    or recipient["response_at"] is not None or recipient["role"] != staff["role"]):
                raise H2StorageError("El actor no está asignado a este pedido.")
            needs: dict[str, int] = {}
            for line in json.loads(order["items_json"]):
                recipe = connection.execute(
                    "SELECT item_id,quantity FROM recipe_components WHERE product_id=? AND variant=?",
                    (line["product_id"], line["variant"]),
                ).fetchall()
                if not recipe:
                    raise H2StorageError("El pedido no tiene una receta de inventario confirmada.")
                quantity = _positive_integer(line["quantity"], "Cantidad")
                line_needs = {part["item_id"]: part["quantity"] for part in recipe}
                replaced_items: set[str] = set()
                for modifier in line.get("modifier_ids", []):
                    substitution = connection.execute(
                        "SELECT replaced_item_id,substitute_item_id FROM modifier_substitutions WHERE modifier_id=?",
                        (modifier,),
                    ).fetchone()
                    if substitution is not None:
                        replaced_item = substitution["replaced_item_id"]
                        if replaced_item in replaced_items or replaced_item not in line_needs:
                            raise H2StorageError("La sustitución no corresponde a la receta del producto.")
                        replacement_quantity = line_needs.pop(replaced_item)
                        replaced_items.add(replaced_item)
                        substitute_item = substitution["substitute_item_id"]
                        line_needs[substitute_item] = line_needs.get(substitute_item, 0) + replacement_quantity
                        continue
                    extra = connection.execute(
                        "SELECT item_id,quantity FROM modifier_components WHERE modifier_id=?", (modifier,)
                    ).fetchone()
                    if extra is None:
                        raise H2StorageError("El extra no tiene una receta de inventario confirmada.")
                    line_needs[extra["item_id"]] = line_needs.get(extra["item_id"], 0) + extra["quantity"]
                for item_id, line_quantity in line_needs.items():
                    needs[item_id] = needs.get(item_id, 0) + line_quantity * quantity
            if not needs:
                raise H2StorageError("El pedido no tiene consumos de inventario verificables.")
            if consumption is not None:
                if not isinstance(consumption, list) or not consumption or len(consumption) > 100:
                    raise ValueError("El consumo no es válido.")
                supplied: dict[str, int] = {}
                for line in consumption:
                    if not isinstance(line, dict) or set(line) != {"item_id", "quantity"}:
                        raise ValueError("El consumo no es válido.")
                    item_id = _text(line["item_id"], "Insumo", maximum=128)
                    supplied[item_id] = supplied.get(item_id, 0) + _positive_integer(line["quantity"], "Consumo")
                if supplied != needs:
                    raise H2StorageError("El consumo no coincide con la receta guardada del pedido.")
            for item_id, quantity in needs.items():
                stock = connection.execute("SELECT on_hand FROM inventory_items WHERE item_id=?", (item_id,)).fetchone()
                if stock is None or stock[0] < quantity:
                    raise InventoryShortage("El inventario cambió; el pedido permanece pendiente y sin descuento.")
            for item_id, quantity in needs.items():
                connection.execute(
                    "UPDATE inventory_items SET on_hand=on_hand-?,updated_at=? WHERE item_id=? AND on_hand>=?",
                    (quantity, now, item_id, quantity),
                )
                connection.execute(
                    "INSERT INTO inventory_movements(movement_id,source_type,source_id,item_id,quantity_delta,actor_phone,created_at) "
                    "VALUES(?, 'order', ?, ?, ?, ?, ?)",
                    (f"{order_id}:{item_id}", order_id, item_id, -quantity, actor_phone, now),
                )
            cursor = connection.execute(
                "UPDATE orders SET status='accepted',accepted_at=?,accepted_by_phone=? WHERE order_id=? AND status='pending_staff'",
                (now, actor_phone, order_id),
            )
            if cursor.rowcount != 1:
                raise H2StorageError("El pedido cambió durante la aceptación.")
            self._queue_message_in_transaction(connection, f"order:{order['folio']}:accepted:customer",
                                               order["customer_phone"], customer_message, now)
            connection.execute("UPDATE order_rounds SET status='closed' WHERE order_id=? AND round_no=?",
                               (order_id, current["round_no"]))
            connection.execute(
                "UPDATE order_recipients SET response_at=COALESCE(response_at,?),result=CASE WHEN staff_phone=? THEN 'accepted' ELSE COALESCE(result,'timed_out') END "
                "WHERE order_id=? AND round_no=?",
                (now, actor_phone, order_id, current["round_no"]),
            )
            connection.execute(
                "UPDATE order_assignments SET response_at=?,result='accepted' WHERE order_id=? AND staff_phone=? AND response_at IS NULL",
                (now, order_id, actor_phone),
            )
            connection.execute("UPDATE tickets SET status='confirmed' WHERE ticket_id=? AND source='whatsapp_order'",
                               (f"wa-{order_id}",))
            if staff_message:
                recipients = connection.execute(
                    "SELECT DISTINCT staff_phone FROM order_recipients WHERE order_id=?", (order_id,)
                ).fetchall()
                for item in recipients:
                    staff_ref = hashlib.sha256(item["staff_phone"].encode()).hexdigest()[:16]
                    self._queue_message_in_transaction(
                        connection, f"order:{order['folio']}:accepted:staff:{staff_ref}",
                        item["staff_phone"], staff_message, now,
                    )
            self._audit(connection, actor_phone, "order_accepted", order["folio"], "ok", {"movement_count": len(needs)}, now)

    def create_pos_ticket(
        self, ticket_id: str, actor_phone: str, lines: list[dict], fingerprint: str, *,
        expires_at: str, now: str | None = None,
    ) -> None:
        """Persist OCR proposals only; an image never causes inventory changes."""
        ticket_id = _text(ticket_id, "Ticket", maximum=64)
        actor_phone, now, expires_at = _phone(actor_phone), _timestamp(now), _timestamp(expires_at)
        if expires_at <= now or datetime.fromisoformat(expires_at) > datetime.fromisoformat(now) + timedelta(hours=24):
            raise ValueError("La retención del ticket debe ser de hasta 24 horas.")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("La huella del ticket no es válida.")
        normalized = _normalize_pos_lines(lines)
        serialized = _json(normalized, max_bytes=16_384)
        with self._transaction() as connection:
            staff = connection.execute("SELECT role,active FROM staff WHERE phone=?", (actor_phone,)).fetchone()
            if staff is None or not staff["active"] or staff["role"] not in {"admin", "cashier"}:
                raise H2StorageError("Solo personal autorizado puede registrar un ticket.")
            duplicate = connection.execute(
                "SELECT ticket_id FROM tickets WHERE source='pos_receipt' AND fingerprint=?", (fingerprint,)
            ).fetchone()
            if duplicate is not None:
                raise DuplicateConflict("Este ticket ya se recibió; no se duplicó.")
            connection.execute(
                "INSERT INTO tickets(ticket_id,source,sender_phone,status,lines_json,media_reference,created_at,expires_at,reviewed_at,reviewed_by,fingerprint) "
                "VALUES(?,'pos_receipt',?,'needs_review',?,NULL,?,?,NULL,NULL,?)",
                (ticket_id, actor_phone, serialized, now, expires_at, fingerprint),
            )
            self._audit(connection, actor_phone, "pos_ticket_received", ticket_id, "needs_review", {"line_count": len(normalized)}, now)

    def pos_ticket(self, ticket_id: str) -> dict | None:
        ticket_id = _text(ticket_id, "Ticket", maximum=64)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT ticket_id,source,sender_phone,status,lines_json,media_reference,created_at,expires_at,reviewed_at,reviewed_by,fingerprint "
                "FROM tickets WHERE ticket_id=? AND source='pos_receipt'", (ticket_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["lines"] = json.loads(result.pop("lines_json"))
        return result

    def revise_pos_ticket_lines(self, ticket_id: str, actor_phone: str, lines: list[dict], *, now: str | None = None) -> None:
        """Replace uncertain OCR with staff-verified catalog lines; still no stock write."""
        ticket_id = _text(ticket_id, "Ticket", maximum=64)
        actor_phone, now = _phone(actor_phone), _timestamp(now)
        if not isinstance(lines, list) or not lines or len(lines) > 50:
            raise ValueError("La corrección debe incluir entre una y cincuenta líneas.")
        normalized = []
        for line in lines:
            if not isinstance(line, dict) or set(line) != {"product_id", "variant", "quantity"}:
                raise ValueError("La corrección debe usar producto, tamaño y cantidad explícitos.")
            normalized.append({
                "status": "recognized", "product_id": _text(line["product_id"], "Producto", maximum=128),
                "variant": _text(line["variant"], "Presentación", maximum=64),
                "quantity": _positive_integer(line["quantity"], "Cantidad"),
                "confidence": 1.0, "source_text": "corregido por personal",
            })
        serialized = _json(normalized, max_bytes=16_384)
        with self._transaction() as connection:
            staff = connection.execute("SELECT role,active FROM staff WHERE phone=?", (actor_phone,)).fetchone()
            ticket = connection.execute("SELECT status,expires_at FROM tickets WHERE ticket_id=? AND source='pos_receipt'", (ticket_id,)).fetchone()
            if staff is None or not staff["active"] or staff["role"] not in {"admin", "cashier"}:
                raise H2StorageError("Acción no autorizada.")
            if ticket is None or ticket["status"] != "needs_review" or ticket["expires_at"] <= now:
                raise H2StorageError("El ticket ya no está disponible para revisión.")
            for line in normalized:
                recipe = connection.execute(
                    "SELECT 1 FROM recipe_components WHERE product_id=? AND variant=? LIMIT 1",
                    (line["product_id"], line["variant"]),
                ).fetchone()
                if recipe is None:
                    raise H2StorageError("El producto o tamaño no tiene receta de inventario confirmada.")
            connection.execute(
                "UPDATE tickets SET lines_json=?,reviewed_at=?,reviewed_by=? WHERE ticket_id=? AND status='needs_review'",
                (serialized, now, actor_phone, ticket_id),
            )
            self._audit(connection, actor_phone, "pos_ticket_lines_corrected", ticket_id, "needs_review", {"line_count": len(normalized)}, now)

    def reject_pos_ticket(self, ticket_id: str, actor_phone: str, *, now: str | None = None) -> None:
        ticket_id = _text(ticket_id, "Ticket", maximum=64)
        actor_phone, now = _phone(actor_phone), _timestamp(now)
        with self._transaction() as connection:
            staff = connection.execute("SELECT role,active FROM staff WHERE phone=?", (actor_phone,)).fetchone()
            ticket = connection.execute("SELECT status,expires_at FROM tickets WHERE ticket_id=? AND source='pos_receipt'", (ticket_id,)).fetchone()
            if staff is None or not staff["active"] or staff["role"] not in {"admin", "cashier"}:
                raise H2StorageError("Acción no autorizada.")
            if ticket is None or ticket["status"] != "needs_review" or ticket["expires_at"] <= now:
                raise H2StorageError("El ticket ya no está pendiente de revisión.")
            connection.execute(
                "UPDATE tickets SET status='rejected',reviewed_at=?,reviewed_by=? WHERE ticket_id=? AND status='needs_review'",
                (now, actor_phone, ticket_id),
            )
            self._audit(connection, actor_phone, "pos_ticket_rejected", ticket_id, "rejected", {}, now)

    def confirm_pos_ticket(self, ticket_id: str, actor_phone: str, *, now: str | None = None) -> dict[str, int]:
        """Atomically confirm one staff-reviewed receipt and apply its recipe movements."""
        ticket_id = _text(ticket_id, "Ticket", maximum=64)
        actor_phone, now = _phone(actor_phone), _timestamp(now)
        with self._transaction() as connection:
            staff = connection.execute("SELECT role,active FROM staff WHERE phone=?", (actor_phone,)).fetchone()
            ticket = connection.execute(
                "SELECT sender_phone,status,lines_json,expires_at,reviewed_by FROM tickets WHERE ticket_id=? AND source='pos_receipt'",
                (ticket_id,),
            ).fetchone()
            if staff is None or not staff["active"] or staff["role"] not in {"admin", "cashier"}:
                raise H2StorageError("Acción no autorizada.")
            if ticket is None or ticket["status"] != "needs_review" or ticket["expires_at"] <= now:
                raise H2StorageError("El ticket ya no está pendiente de confirmación.")
            if ticket["reviewed_by"] not in {None, actor_phone} and staff["role"] != "admin":
                raise H2StorageError("Solo quien revisó el ticket o un administrador puede confirmarlo.")
            lines = json.loads(ticket["lines_json"])
            if not lines or any(line.get("status") != "recognized" or line.get("confidence", 0) < 0.65 for line in lines):
                raise H2StorageError("Hay líneas sin confirmar; corrígelas antes de registrar la venta.")
            needs: dict[str, int] = {}
            for line in lines:
                recipe_rows = connection.execute(
                    "SELECT item_id,quantity FROM recipe_components WHERE product_id=? AND variant=?",
                    (line["product_id"], line["variant"]),
                ).fetchall()
                if not recipe_rows:
                    raise H2StorageError("La receta no está confirmada; no se descontó inventario.")
                for row in recipe_rows:
                    needs[row["item_id"]] = needs.get(row["item_id"], 0) + row["quantity"] * line["quantity"]
            if connection.execute(
                "SELECT 1 FROM inventory_movements WHERE source_type='pos_ticket' AND source_id=? LIMIT 1", (ticket_id,)
            ).fetchone():
                raise DuplicateConflict("Este ticket ya generó movimientos.")
            for item_id, quantity in needs.items():
                stock = connection.execute("SELECT on_hand FROM inventory_items WHERE item_id=?", (item_id,)).fetchone()
                if stock is None or stock[0] < quantity:
                    raise InventoryShortage("No alcanza la existencia actual; no se registró ningún descuento.")
            for item_id, quantity in needs.items():
                updated = connection.execute(
                    "UPDATE inventory_items SET on_hand=on_hand-?,updated_at=? WHERE item_id=? AND on_hand>=?",
                    (quantity, now, item_id, quantity),
                )
                if updated.rowcount != 1:
                    raise InventoryShortage("Cambió la existencia; se revirtió todo el movimiento.")
                connection.execute(
                    "INSERT INTO inventory_movements(movement_id,source_type,source_id,item_id,quantity_delta,actor_phone,created_at) "
                    "VALUES(?, 'pos_ticket', ?, ?, ?, ?, ?)",
                    (f"{ticket_id}:{item_id}", ticket_id, item_id, -quantity, actor_phone, now),
                )
            cursor = connection.execute(
                "UPDATE tickets SET status='confirmed',reviewed_at=?,reviewed_by=? WHERE ticket_id=? AND status='needs_review'",
                (now, actor_phone, ticket_id),
            )
            if cursor.rowcount != 1:
                raise H2StorageError("El ticket cambió durante la confirmación; se revirtió el descuento.")
            self._audit(connection, actor_phone, "pos_ticket_confirmed", ticket_id, "confirmed", {"movement_count": len(needs)}, now)
            return needs

    def inventory_movements(self, *, source_type: str | None = None, source_id: str | None = None) -> list[dict]:
        if source_type is not None and source_type not in {"order", "pos_ticket", "adjustment"}:
            raise ValueError("Tipo de movimiento no válido.")
        if source_id is not None:
            source_id = _text(source_id, "Origen", maximum=128)
        clauses, values = [], []
        if source_type is not None:
            clauses.append("source_type=?")
            values.append(source_type)
        if source_id is not None:
            clauses.append("source_id=?")
            values.append(source_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT movement_id,source_type,source_id,item_id,quantity_delta,actor_phone,created_at "
                f"FROM inventory_movements{where} ORDER BY created_at,movement_id", values,
            ).fetchall()
        return [dict(row) for row in rows]

    def transition_order(self, order_id: str, actor_phone: str, new_status: str, *, message: str, now: str | None = None) -> None:
        order_id, actor_phone, now = _text(order_id, "Pedido", maximum=64), _phone(actor_phone), _timestamp(now)
        message = _text(message, "Aviso", maximum=4096)
        transitions = {"accepted": "preparing", "preparing": "ready", "ready": "delivered"}
        with self._transaction() as connection:
            order = connection.execute("SELECT folio,customer_phone,status,accepted_by_phone FROM orders WHERE order_id=?", (order_id,)).fetchone()
            staff = connection.execute("SELECT role,active FROM staff WHERE phone=?", (actor_phone,)).fetchone()
            if order is None or transitions.get(order["status"]) != new_status or staff is None or not staff["active"]:
                raise H2StorageError("La transición o el actor no están autorizados.")
            if staff["role"] not in {"cashier", "admin"} or order["accepted_by_phone"] != actor_phone:
                raise H2StorageError("El actor no está asignado a este pedido.")
            cursor = connection.execute(
                "UPDATE orders SET status=?,retention_until=? WHERE order_id=? AND status=?",
                (new_status, _plus_hours(now, 24) if new_status == "delivered" else None, order_id, order["status"]),
            )
            if cursor.rowcount != 1:
                raise H2StorageError("El pedido cambió durante la transición.")
            self._queue_message_in_transaction(
                connection, f"order:{order['folio']}:status:{new_status}:customer", order["customer_phone"], message, now,
            )
            if new_status == "delivered":
                connection.execute(
                    "UPDATE tickets SET expires_at=? WHERE ticket_id=? AND source='whatsapp_order'",
                    (_plus_hours(now, 24), f"wa-{order_id}"),
                )
            self._audit(connection, actor_phone, f"order_{new_status}", order["folio"], "ok", {}, now)

    def order_for_customer(self, folio: str, customer_phone: str) -> dict | None:
        folio, customer_phone = _text(folio, "Folio", maximum=32), _phone(customer_phone)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT folio,status,total_cents,items_json,created_at,expires_at FROM orders WHERE folio=? AND customer_phone=?",
                (folio, customer_phone),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["items"] = json.loads(result.pop("items_json"))
        return result

    def audit_rows(self, *, limit: int = 100) -> list[dict]:
        _positive_integer(limit, "Límite")
        if limit > 500:
            raise ValueError("Límite de auditoría demasiado grande.")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT sequence,actor_phone,action,target,result,details_json,created_at FROM audit_log ORDER BY sequence DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _audit(connection: sqlite3.Connection, actor: str, action: str, target: str, result: str, details: dict, now: str) -> None:
        connection.execute(
            "INSERT INTO audit_log(actor_phone,action,target,result,details_json,created_at) VALUES(?,?,?,?,?,?)",
            (_text(actor, "Actor", maximum=128), _text(action, "Acción", maximum=128),
             _text(target, "Destino", maximum=128), _text(result, "Resultado", maximum=64), _json(details, max_bytes=4096), now),
        )


def _normalize_pos_lines(lines: list[dict]) -> list[dict]:
    if not isinstance(lines, list) or not lines or len(lines) > 50:
        raise ValueError("El OCR debe devolver entre una y cincuenta líneas.")
    normalized = []
    for line in lines:
        if not isinstance(line, dict):
            raise TypeError("Una línea OCR no cumple el esquema esperado.")
        status = line.get("status")
        confidence = line.get("confidence")
        if type(confidence) not in {int, float} or not 0 <= confidence <= 1:
            raise ValueError("La confianza OCR debe estar entre 0 y 1.")
        source_text = _text(line.get("source_text"), "Texto OCR", maximum=512)
        if status == "recognized" and set(line) == {"status", "product_id", "variant", "quantity", "confidence", "source_text"}:
            normalized.append({
                "status": "recognized", "product_id": _text(line["product_id"], "Producto", maximum=128),
                "variant": _text(line["variant"], "Presentación", maximum=64),
                "quantity": _positive_integer(line["quantity"], "Cantidad"),
                "confidence": float(confidence), "source_text": source_text,
            })
        elif status == "unresolved" and set(line) == {"status", "confidence", "source_text"}:
            normalized.append({"status": "unresolved", "confidence": float(confidence), "source_text": source_text})
        else:
            raise ValueError("Una línea OCR no cumple el esquema esperado.")
    return normalized


_ASSISTANT_JOBS_TABLE = (
    "CREATE TABLE assistant_jobs(queue_sequence INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL UNIQUE,"
    "event_id TEXT NOT NULL UNIQUE REFERENCES webhook_events(event_id),sender_phone TEXT NOT NULL,"
    "status TEXT NOT NULL CHECK(status IN ('queued','running','cancel_requested','completed','failed','cancelled','uncertain')),"
    "received_at TEXT NOT NULL,deadline_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,"
    "cancel_event_id TEXT REFERENCES webhook_events(event_id),worker_id TEXT,error_code TEXT,retention_until TEXT NOT NULL)"
)

_CATALOG_PRICE_OVERRIDES_TABLE = (
    "CREATE TABLE catalog_price_overrides(entry_type TEXT NOT NULL CHECK(entry_type IN ('variant','extra')),"
    "entry_id TEXT NOT NULL,variant_size TEXT NOT NULL,price_cents INTEGER NOT NULL CHECK(price_cents>=0),"
    "price_status TEXT NOT NULL CHECK(price_status='provisional'),updated_by TEXT NOT NULL REFERENCES staff(phone),"
    "updated_at TEXT NOT NULL,PRIMARY KEY(entry_type,entry_id,variant_size),"
    "CHECK((entry_type='variant' AND variant_size IN ('mediano','grande','presentacion_unica')) OR "
    "(entry_type='extra' AND variant_size='')))"
)


_SCHEMA = (
    "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    "CREATE TABLE staff(phone TEXT PRIMARY KEY,role TEXT NOT NULL CHECK(role IN ('admin','manager','cashier')),active INTEGER NOT NULL CHECK(active IN (0,1)),invited_by TEXT REFERENCES staff(phone),created_at TEXT NOT NULL)",
    "CREATE TABLE auth_codes(phone TEXT PRIMARY KEY REFERENCES staff(phone) ON DELETE CASCADE,code_hash TEXT NOT NULL,created_at TEXT NOT NULL,last_sent_at TEXT NOT NULL,expires_at TEXT NOT NULL,attempts INTEGER NOT NULL CHECK(attempts BETWEEN 0 AND 5),used_at TEXT)",
    "CREATE TABLE staff_sessions(session_hash TEXT PRIMARY KEY,phone TEXT NOT NULL REFERENCES staff(phone),role TEXT NOT NULL CHECK(role IN ('admin','manager','cashier')),created_at TEXT NOT NULL,last_activity_at TEXT NOT NULL,expires_at TEXT NOT NULL,revoked_at TEXT)",
    "CREATE TABLE conversations(phone TEXT PRIMARY KEY,state_json TEXT NOT NULL,updated_at TEXT NOT NULL,expires_at TEXT NOT NULL)",
    "CREATE TABLE webhook_events(event_id TEXT PRIMARY KEY,normalized_payload_json TEXT NOT NULL,received_at TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('queued','processing','done','failed')),attempts INTEGER NOT NULL DEFAULT 0,finished_at TEXT,expires_at TEXT,error_code TEXT)",
    "CREATE TABLE orders(order_id TEXT PRIMARY KEY,folio TEXT NOT NULL UNIQUE,idempotency_key TEXT NOT NULL UNIQUE,request_fingerprint TEXT NOT NULL,customer_phone TEXT NOT NULL,items_json TEXT NOT NULL,total_cents INTEGER NOT NULL CHECK(total_cents>=0),status TEXT NOT NULL CHECK(status IN ('pending_staff','accepted','preparing','ready','delivered','rejected','expired','cancelled')),created_at TEXT NOT NULL,expires_at TEXT NOT NULL,accepted_at TEXT,retention_until TEXT,accepted_by_phone TEXT REFERENCES staff(phone))",
    "CREATE TABLE order_assignments(assignment_id INTEGER PRIMARY KEY,order_id TEXT NOT NULL REFERENCES orders(order_id),sequence INTEGER NOT NULL CHECK(sequence>0),staff_phone TEXT NOT NULL REFERENCES staff(phone),assigned_at TEXT NOT NULL,deadline TEXT NOT NULL,response_at TEXT,result TEXT,UNIQUE(order_id,sequence))",
    "CREATE INDEX idx_assignments_staff_activity ON order_assignments(staff_phone,response_at)",
    "CREATE TABLE inventory_items(item_id TEXT PRIMARY KEY,name TEXT NOT NULL,unit TEXT NOT NULL,on_hand INTEGER NOT NULL CHECK(on_hand>=0),updated_at TEXT NOT NULL)",
    "CREATE TABLE recipe_components(product_id TEXT NOT NULL,variant TEXT NOT NULL,item_id TEXT NOT NULL REFERENCES inventory_items(item_id),quantity INTEGER NOT NULL CHECK(quantity>0),PRIMARY KEY(product_id,variant,item_id))",
    "CREATE TABLE modifier_components(modifier_id TEXT NOT NULL,item_id TEXT NOT NULL REFERENCES inventory_items(item_id),quantity INTEGER NOT NULL CHECK(quantity>0),PRIMARY KEY(modifier_id,item_id))",
    "CREATE TABLE modifier_substitutions(modifier_id TEXT PRIMARY KEY,replaced_item_id TEXT NOT NULL REFERENCES inventory_items(item_id),substitute_item_id TEXT NOT NULL REFERENCES inventory_items(item_id),CHECK(replaced_item_id<>substitute_item_id))",
    "CREATE TABLE tickets(ticket_id TEXT PRIMARY KEY,source TEXT NOT NULL CHECK(source IN ('whatsapp_order','pos_receipt')),sender_phone TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('draft','needs_review','confirmed','rejected','expired')),lines_json TEXT NOT NULL,media_reference TEXT,created_at TEXT NOT NULL,expires_at TEXT,reviewed_at TEXT,reviewed_by TEXT REFERENCES staff(phone),fingerprint TEXT NOT NULL)",
    "CREATE TABLE inventory_movements(movement_id TEXT PRIMARY KEY,source_type TEXT NOT NULL CHECK(source_type IN ('order','pos_ticket','adjustment')),source_id TEXT NOT NULL,item_id TEXT NOT NULL REFERENCES inventory_items(item_id),quantity_delta INTEGER NOT NULL CHECK(quantity_delta<>0),actor_phone TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(source_type,source_id,item_id))",
    "CREATE TABLE audit_log(sequence INTEGER PRIMARY KEY,actor_phone TEXT NOT NULL,action TEXT NOT NULL,target TEXT NOT NULL,result TEXT NOT NULL,details_json TEXT NOT NULL,created_at TEXT NOT NULL)",
    "CREATE TABLE outbox(outbox_id TEXT PRIMARY KEY,dedupe_key TEXT NOT NULL UNIQUE,recipient_phone TEXT NOT NULL,message_text TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('queued','sending','sent','uncertain','failed')),attempts INTEGER NOT NULL CHECK(attempts>=0),created_at TEXT NOT NULL,next_attempt_at TEXT NOT NULL,sent_at TEXT,expires_at TEXT,error_code TEXT,retention_until TEXT,provider_message_id TEXT,delivery_status TEXT)",
    "CREATE INDEX idx_outbox_due ON outbox(status,next_attempt_at,created_at)",
    "CREATE TABLE order_rounds(order_id TEXT NOT NULL REFERENCES orders(order_id),round_no INTEGER NOT NULL CHECK(round_no>0),phase TEXT NOT NULL CHECK(phase IN ('cashier','escalation')),opened_at TEXT NOT NULL,deadline TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('active','closed')),PRIMARY KEY(order_id,round_no))",
    "CREATE TABLE order_recipients(order_id TEXT NOT NULL,round_no INTEGER NOT NULL,staff_phone TEXT NOT NULL REFERENCES staff(phone),role TEXT NOT NULL CHECK(role IN ('admin','cashier')),notified_at TEXT NOT NULL,response_at TEXT,result TEXT CHECK(result IN ('accepted','passed','timed_out','rejected')),PRIMARY KEY(order_id,round_no,staff_phone),FOREIGN KEY(order_id,round_no) REFERENCES order_rounds(order_id,round_no))",
    "CREATE INDEX idx_order_recipients_staff ON order_recipients(staff_phone,response_at)",
    _ASSISTANT_JOBS_TABLE,
    _CATALOG_PRICE_OVERRIDES_TABLE,
    "CREATE UNIQUE INDEX idx_assistant_jobs_pending_phone ON assistant_jobs(sender_phone) WHERE status IN ('queued','running','cancel_requested')",
    "CREATE INDEX idx_assistant_jobs_queue ON assistant_jobs(status,queue_sequence)",
)
