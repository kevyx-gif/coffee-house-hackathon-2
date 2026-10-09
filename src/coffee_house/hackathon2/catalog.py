"""Approved H1 catalog with H2 price overrides and live availability alternatives."""
from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
from typing import Any

from .storage import H2Store

ALIASES = {
    "frappe de moka": "frappe_mocca",
    "frappe moka": "frappe_mocca",
    "frappe de moca": "frappe_mocca",
    "frappe de oreo": "frappe_oreo",
    "iced latte": "iced_latte",
    "latte frio": "iced_latte",
    "latte caliente": "hot_latte",
}
SIZE_ALIASES = {
    "mediano": "mediano", "mediana": "mediano", "med": "mediano", "medium": "mediano",
    "medianito": "mediano", "medianita": "mediano", "mediani": "mediano",
    "grande": "grande", "grand": "grande", "large": "grande", "presentacion unica": "presentacion_unica",
}
QUERY_ALIASES = {
    "bebidas frias con cafe": "iced coffee",
    "cafe frio": "iced coffee",
    "cafes frios": "iced coffee",
    "tes": "te",
}


class CatalogService:
    def __init__(self, path: str | Path, store: H2Store):
        self.path = Path(path)
        self.store = store
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("No se pudo leer el catálogo aprobado.") from exc
        if not isinstance(document, dict) or document.get("schema_version") != 2 or document.get("currency") != "MXN":
            raise ValueError("El catálogo no cumple el esquema esperado.")
        products = document.get("products")
        extras = document.get("extras")
        if not isinstance(products, list) or not products or not isinstance(extras, list):
            raise ValueError("El catálogo está incompleto.")
        self.currency = document["currency"]
        self._base_products = products
        self._base_extras = extras
        self._base_by_id = {product.get("id"): product for product in products if isinstance(product, dict)}
        if len(self._base_by_id) != len(products) or None in self._base_by_id:
            raise ValueError("El catálogo contiene identificadores duplicados o inválidos.")

    @property
    def products(self) -> list[dict[str, Any]]:
        products = deepcopy(self._base_products)
        overrides = self.store.catalog_price_overrides()
        for product in products:
            for variant in product.get("variants", []):
                override = overrides.get(("variant", product["id"], variant.get("size", "")))
                if override:
                    variant["price_cents"] = override["price_cents"]
                    variant["price_status"] = override["price_status"]
        return products

    @property
    def extras(self) -> list[dict[str, Any]]:
        extras = deepcopy(self._base_extras)
        overrides = self.store.catalog_price_overrides()
        for extra in extras:
            override = overrides.get(("extra", extra["id"], ""))
            if override:
                extra["price_cents"] = override["price_cents"]
                extra["price_status"] = override["price_status"]
        return extras

    def search(self, query: str, *, limit: int = 12) -> list[dict[str, Any]]:
        normalized = _normalize(query)
        if not normalized or len(normalized) > 128:
            return []
        category_alias = QUERY_ALIASES.get(normalized)
        if category_alias:
            matches = [
                product for product in self.products
                if _normalize(str(product.get("category", ""))) == category_alias
                or _normalize(str(product.get("category", ""))).startswith(category_alias + " ")
            ]
            return [self._public_product(product) for product in matches[:limit]]
        explicit = ALIASES.get(normalized)
        if explicit:
            product = self.product_by_id(explicit)
            return [self._public_product(product)] if product else []
        scored = []
        query_tokens = set(normalized.split())
        for product in self.products:
            name = _normalize(str(product.get("name", "")))
            category = _normalize(str(product.get("category", "")))
            product_id = _normalize(str(product.get("id", "")).replace("_", " "))
            if not name:
                continue
            if normalized == name or normalized == category or normalized == product_id:
                score = 1.0
            elif len(normalized) >= 4 and normalized in name:
                score = 0.98
            elif name in normalized:
                # A shorter product name inside a longer request is only a weak
                # candidate; do not let “Oreo Latte” collapse to plain “Latte”.
                score = 0.80
            elif normalized in category:
                # A broad one-word query like “latte” should list drinks whose
                # names contain it, not every item filed under a “Lattes” heading.
                score = 0.75
            else:
                fields = [name, category, product_id]
                best = max(SequenceMatcher(None, normalized, field).ratio() for field in fields)
                overlap = len(query_tokens & set((name + " " + category).split()))
                score = min(0.93, best + (0.06 if overlap == len(query_tokens) and query_tokens else 0))
            if score >= 0.62:
                scored.append((score, product))
        if not scored:
            return []
        scored.sort(key=lambda item: (-item[0], str(item[1].get("name", ""))))
        best_score = scored[0][0]
        selected = [product for score, product in scored if score >= max(0.62, best_score - 0.12)]
        if len(selected) == 1 and best_score < 0.82:
            return []
        return [self._public_product(product) for product in selected[:limit]]

    def resolve_product(self, query: str) -> tuple[dict | None, list[dict]]:
        candidates = self.search(query, limit=8)
        if len(candidates) != 1:
            return None, candidates
        return self.product_by_id(candidates[0]["id"]), []

    def resolve_explicit_alias_mention(self, text: str, *, model_query: str | None = None) -> dict | None:
        """Resolve one unambiguous approved alias stated in the customer's own text."""
        normalized = _normalize(text)
        matched_ids = {
            product_id
            for alias, product_id in ALIASES.items()
            if re.search(
                rf"(?<![a-z0-9]){re.escape(_normalize(alias))}(?![a-z0-9])",
                normalized,
            )
        }
        if len(matched_ids) != 1:
            return None
        product_id = next(iter(matched_ids))
        product = self.product_by_id(product_id)
        if product is None or model_query is None:
            return product
        query = _normalize(model_query)
        approved_queries = {
            _normalize(str(product.get("name", ""))),
            _normalize(product_id.replace("_", " ")),
            *(_normalize(alias) for alias, target in ALIASES.items() if target == product_id),
        }
        return product if query in approved_queries else None

    def product_by_id(self, product_id: str) -> dict | None:
        """Resolve an exact canonical product identifier for trusted workflows."""
        if not isinstance(product_id, str) or len(product_id) > 128:
            return None
        return next((product for product in self.products if product.get("id") == product_id), None)

    def set_variant_price(self, actor_phone: str, product_id: str, size: str, price_cents: int, *, now: str | None = None) -> None:
        product = self.product_by_id(product_id)
        if product is None:
            raise ValueError("No encontré ese producto del menú.")
        variant = next((item for item in product.get("variants", []) if item.get("size") == size), None)
        if variant is None:
            raise ValueError("Ese tamaño no está en el menú.")
        self.store.set_catalog_price(
            "variant", product_id, size, price_cents, actor_phone,
            previous_price_cents=variant.get("price_cents"), now=now,
        )

    def set_extra_price(self, actor_phone: str, extra_id: str, price_cents: int, *, now: str | None = None) -> None:
        if extra_id not in {extra.get("id") for extra in self._base_extras}:
            raise ValueError("No encontré ese extra del menú.")
        extra = next(item for item in self._base_extras if item["id"] == extra_id)
        current = self.store.catalog_price_overrides().get(("extra", extra_id, ""), {})
        previous = current.get("price_cents", extra.get("price_cents"))
        self.store.set_catalog_price(
            "extra", extra_id, "", price_cents, actor_phone,
            previous_price_cents=previous, now=now,
        )

    def resolve_extras(self, names: list[str]) -> tuple[list[dict] | None, str | None]:
        resolved = []
        for name in names:
            if not isinstance(name, str) or len(name) > 128:
                return None, "No pude identificar uno de los extras."
            key = _normalize(name)
            matches = [extra for extra in self.extras if key in {
                _normalize(str(extra.get("id", ""))), _normalize(str(extra.get("name", "")))
            }]
            if len(matches) != 1:
                return None, "No tengo ese extra confirmado. El personal te lo puede confirmar."
            resolved.append(matches[0])
        if len({extra["id"] for extra in resolved}) != len(resolved):
            return None, "No puedo contar dos veces el mismo extra."
        groups = {extra.get("exclusive_group") for extra in resolved if extra.get("exclusive_group")}
        for group in groups:
            if sum(extra.get("exclusive_group") == group for extra in resolved) > 1:
                return None, "Solo se puede elegir una leche vegetal. ¿Cuál prefieres?"
        return resolved, None

    def format_menu_results(self, products: list[dict]) -> str:
        if not products:
            return "No encontré una bebida con ese nombre. ¿Me dices cuál te interesa?"
        lines = ["Estas son las opciones que encontré:"]
        for product in products:
            lines.append(self._format_product(product, include_stock=True))
        return "\n".join(lines)

    def check_availability(self, product: dict, size: str | None, extras: list[dict]) -> str:
        variants = product.get("variants", [])
        if size:
            normalized_size = SIZE_ALIASES.get(_normalize(size))
            if normalized_size is None:
                return "¿Qué tamaño prefieres: mediano o grande?"
            variants = [variant for variant in variants if variant.get("size") == normalized_size]
        if not variants:
            sizes = sorted({variant.get("size") for variant in product.get("variants", []) if variant.get("size")})
            if sizes:
                options = " o ".join(_display_size(value) for value in sizes)
                return f"Por ahora tengo {options}. ¿Cuál prefieres?"
            return "No tengo una presentación confirmada para esa bebida. El personal te lo puede confirmar."
        if len(variants) > 1:
            options = " o ".join(_display_size(variant["size"]) for variant in variants)
            return f"¿La quieres {options}?"
        variant = variants[0]
        item_name = f"{product['name']} {self._format_variant_size(variant)}"
        product_group = product.get("extra_group")
        if product_group:
            incompatible = [extra for extra in extras if product_group not in extra.get("groups", [])]
            if incompatible:
                return "Ese extra no corresponde a esa bebida. ¿Quieres que te sugiera opciones disponibles?"
        modifier_ids = [extra["id"] for extra in extras]
        available = self.store.available_portions(product["id"], variant["size"], modifier_ids)
        base_available = self.store.available_portions(product["id"], variant["size"])
        if available is None:
            status = f"No tengo confirmada la disponibilidad del {item_name}; el personal te la puede confirmar."
        elif available <= 0:
            if base_available == 0:
                status = f"Por el momento no tenemos el {item_name}."
            elif base_available is not None and extras:
                unavailable_extras = [
                    extra["name"] for extra in extras
                    if self.store.available_portions(product["id"], variant["size"], [extra["id"]]) == 0
                ]
                if unavailable_extras:
                    names = ", ".join(unavailable_extras)
                    status = f"Sí tenemos el {item_name}, pero por ahora no contamos con {names}."
                else:
                    status = f"Sí tenemos el {item_name}, pero no puedo confirmar esa combinación de extras."
            elif base_available is None:
                status = f"No tengo confirmada la disponibilidad del {item_name}; el personal te la puede confirmar."
            else:
                status = f"Por el momento no tenemos el {item_name}."
        else:
            status = f"Sí, todavía tenemos el {item_name}."
        details = self._format_price_and_size(product, variant).split(": ", 1)[-1]
        if extras:
            base_price = variant.get("price_cents")
            extra_prices = [extra.get("price_cents") for extra in extras]
            extra_names = ", ".join(str(extra["name"]) for extra in extras)
            if type(base_price) is int and base_price >= 0 and all(
                type(price) is int and price >= 0 for price in extra_prices
            ):
                total = base_price + sum(extra_prices)
                total_label = f"${total / 100:,.2f} MXN"
                if variant.get("price_status") != "documentado":
                    total_label += " (total provisional)"
                details += f". Con {extra_names}, el total sería {total_label}"
            else:
                details += f". Incluye {extra_names}; el total está por confirmar"
        reply = f"{status} {details.rstrip('. ')}."
        if available == 0:
            alternatives = self.available_alternatives(product["id"], variant["size"], modifier_ids)
            if alternatives:
                options = "\n".join(f"• {item['label']}" for item in alternatives)
                return f"{reply}\n\nEn su lugar, sí hay:\n{options}\n¿Te gustaría alguna?"
            return f"{reply} Por ahora no tengo otra opción confirmada; el personal te puede decir qué hay disponible."
        return reply

    def available_alternatives(
        self,
        product_id: str,
        size: str,
        modifier_ids: list[str],
        *,
        quantity: int = 1,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Suggest only menu combinations whose current demo stock covers the request."""
        if (
            not isinstance(product_id, str) or not isinstance(size, str)
            or not isinstance(modifier_ids, list) or any(not isinstance(value, str) for value in modifier_ids)
            or type(quantity) is not int or not 1 <= quantity <= 50
            or type(limit) is not int or not 1 <= limit <= 10
            or len(modifier_ids) > 3 or len(modifier_ids) != len(set(modifier_ids))
        ):
            return []
        original = self.product_by_id(product_id)
        if original is None:
            return []
        all_extras = {extra["id"]: extra for extra in self.extras}
        selected = [all_extras.get(extra_id) for extra_id in modifier_ids]
        if any(extra is None for extra in selected):
            return []

        modifier_sets: list[list[str]] = []

        def add_modifier_set(values: list[str]) -> None:
            if values not in modifier_sets:
                modifier_sets.append(values)

        add_modifier_set(list(modifier_ids))
        for index, extra in enumerate(selected):
            exclusive_group = extra.get("exclusive_group")
            if not exclusive_group:
                continue
            for replacement in all_extras.values():
                if replacement["id"] == extra["id"] or replacement.get("exclusive_group") != exclusive_group:
                    continue
                replaced = list(modifier_ids)
                replaced[index] = replacement["id"]
                add_modifier_set(replaced)
        for kept_count in range(len(modifier_ids) - 1, -1, -1):
            for subset in combinations(modifier_ids, kept_count):
                add_modifier_set(list(subset))

        source_group = original.get("extra_group")
        source_category = original.get("category")
        candidates = []
        for product in self.products:
            same_product = product["id"] == product_id
            if same_product:
                compatible_group = True
            elif source_group:
                compatible_group = product.get("extra_group") == source_group
            else:
                compatible_group = product.get("category") == source_category
            if not compatible_group:
                continue
            for variant in product.get("variants", []):
                candidates.append((
                    0 if variant.get("size") == size else 1,
                    0 if same_product else 1,
                    0 if product.get("category") == source_category else 1,
                    str(product.get("name", "")),
                    product,
                    variant,
                ))

        suggestions = []
        seen: set[tuple[str, str, tuple[str, ...]]] = set()
        for _, _, _, _, product, variant in sorted(candidates, key=lambda item: item[:4]):
            variant_size = variant.get("size")
            if not isinstance(variant_size, str):
                continue
            for proposed_ids in modifier_sets:
                key = (product["id"], variant_size, tuple(proposed_ids))
                if key in seen or (product["id"] == product_id and variant_size == size and proposed_ids == modifier_ids):
                    continue
                extras, error = self.resolve_extras([all_extras[value]["name"] for value in proposed_ids])
                if error or extras is None or [extra["id"] for extra in extras] != proposed_ids:
                    continue
                group = product.get("extra_group")
                if group and any(group not in extra.get("groups", []) for extra in extras):
                    continue
                available = self.store.available_portions(product["id"], variant_size, proposed_ids)
                if available is None or available < quantity:
                    continue
                base_price = variant.get("price_cents")
                extra_prices = [extra.get("price_cents") for extra in extras]
                if type(base_price) is not int or base_price < 0 or any(type(price) is not int or price < 0 for price in extra_prices):
                    continue
                unit_price = base_price + sum(extra_prices)
                provisional = variant.get("price_status") != "documentado" or any(
                    extra.get("price_status", "documentado") != "documentado" for extra in extras
                )
                price_text = f"${unit_price / 100:,.2f} MXN"
                if provisional:
                    price_text += " (provisional)"
                label = f"{product['name']} {self._format_variant_size(variant)}"
                if extras:
                    label += f" con {', '.join(extra['name'] for extra in extras)}"
                label += f": {price_text}"
                if quantity > 1:
                    label += f" cada uno; total para {quantity}: ${unit_price * quantity / 100:,.2f} MXN"
                suggestions.append({
                    "product_id": product["id"], "size": variant_size,
                    "modifier_ids": list(proposed_ids), "available_portions": available,
                    "price_cents": unit_price, "provisional": provisional, "label": label,
                })
                seen.add(key)
                if len(suggestions) >= limit:
                    return suggestions
        return suggestions

    def _format_product(self, product: dict, *, include_stock: bool) -> str:
        variants = product.get("variants", [])
        prices = []
        for variant in variants:
            price = self._format_price_and_size(product, variant)
            if include_stock:
                available = self.store.available_portions(product["id"], variant.get("size", ""))
                if available is None:
                    status = "disponibilidad por confirmar"
                elif available == 0:
                    status = "por ahora no tenemos"
                else:
                    status = "todavía tenemos"
                price += f" ({status})"
            prices.append(price)
        result = f"• {product['name']}: " + "; ".join(prices)
        if product.get("note"):
            result += f" ({product['note']})"
        return result

    @staticmethod
    def _format_price_and_size(product: dict, variant: dict) -> str:
        size = CatalogService._format_variant_size(variant)
        price = variant.get("price_cents")
        if type(price) is not int or price < 0:
            price_label = "precio por confirmar"
        else:
            price_label = f"${price / 100:,.2f} MXN"
            if variant.get("price_status") != "documentado":
                price_label += " (provisional)"
        return f"{size}: {price_label}"

    @staticmethod
    def _format_variant_size(variant: dict) -> str:
        size = _display_size(variant.get("size", ""))
        volume = variant.get("volume_ml")
        volume_oz = variant.get("volume_oz")
        if volume and volume_oz:
            return f"{size} ({volume} ml / {volume_oz} oz)"
        if volume:
            return f"{size} ({volume} ml)"
        if volume_oz:
            return f"{size} ({volume_oz} oz)"
        return size

    @staticmethod
    def _public_product(product: dict) -> dict:
        # Return only catalog-approved fields; never pass private source file metadata to the model.
        return {
            "id": product["id"], "name": product["name"], "category": product.get("category"),
            "variants": product.get("variants", []), "note": product.get("note"),
        }


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    no_marks = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", no_marks)).strip()


def _display_size(value: str) -> str:
    return {"mediano": "mediano", "grande": "grande", "presentacion_unica": "presentación única"}.get(value, value)
