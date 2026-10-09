"""Measure the approved Qwen/Groq tool router on fixed synthetic Spanish intents.

The runner sends only the cases in this file, never reads real conversations, and
records tool summaries/usage rather than free-form provider text or credentials.
"""
from __future__ import annotations

import asyncio
import json
import re
import statistics
import subprocess
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from coffee_house.hackathon2.model import GroqSettings, GroqToolRouter, ModelUnavailable

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "evaluation" / "t9" / "groq-intent-threshold-2026-10-09-after.json"
INTER_REQUEST_SECONDS = 9.0
MAX_RATE_LIMIT_RETRIES = 2


def clear(case_id: str, text: str, tool: str, **expect: object) -> dict:
    return {"id": case_id, "text": text, "expected_tool": tool, "expect": expect}


CLEAR_CASES = [
    clear("menu_latte", "¿Qué tipos de latte tienen?", "search_menu", query_groups=[["latte"]]),
    clear("menu_frappe", "Muéstrame los frappés, por favor.", "search_menu", query_groups=[["frappe"], ["frappes"]]),
    clear("menu_tea", "¿Qué tés fríos venden?", "search_menu", query_groups=[["te"], ["tes"]]),
    clear("menu_matcha", "¿Qué bebidas de matcha ofrecen?", "search_menu", query_groups=[["matcha"]]),
    clear("menu_extras", "¿Qué extras puedo agregar a un latte?", "search_menu", query_groups=[["extras", "latte"], ["extra", "latte"]]),
    clear("stock_hot_latte", "¿Hay latte caliente mediano?", "check_availability", product_groups=[["latte"]], size="mediano"),
    clear("stock_iced_oreo", "¿Tienen Iced Oreo Latte grande?", "check_availability", product_groups=[["oreo", "latte"]], size="grande"),
    clear("stock_oat_latte", "¿Hay leche de avena para un latte mediano?", "check_availability", product_groups=[["latte"]], size="mediano", modifier_groups=[["leche", "avena"]]),
    clear("stock_frappe_mocca", "¿Todavía hay Frappé Mocca grande?", "check_availability", product_groups=[["frappe", "mocca"], ["frappe", "moka"]], size="grande"),
    clear("stock_matcha", "¿Queda matcha frío?", "check_availability", product_groups=[["matcha"]], size=None),
    clear("order_hot_latte", "Quiero pedir un latte caliente mediano con leche de avena.", "prepare_order", items=[{"product_groups": [["latte"]], "size": "mediano", "quantity": 1, "modifier_groups": [["leche", "avena"]]}]),
    clear("order_two_oreo_frappes", "Me das dos Frappé Oreo grandes.", "prepare_order", items=[{"product_groups": [["frappe", "oreo"]], "size": "grande", "quantity": 2, "modifier_groups": []}]),
    clear("order_iced_matcha", "Ponme un Iced Matcha Latte grande con leche de almendras.", "prepare_order", items=[{"product_groups": [["matcha", "latte"]], "size": "grande", "quantity": 1, "modifier_groups": [["leche", "almendras"]]}]),
    clear("order_hot_chocolate", "Quiero un chocolate caliente mediano.", "prepare_order", items=[{"product_groups": [["chocolate"]], "size": "mediano", "quantity": 1, "modifier_groups": []}]),
    clear("order_caramel_macchiato", "Quiero ordenar un Iced Caramel Macchiato grande.", "prepare_order", items=[{"product_groups": [["caramel", "macchiato"]], "size": "grande", "quantity": 1, "modifier_groups": []}]),
    clear("price_hot_latte", "¿Cuánto cuesta el latte mediano caliente?", "check_availability", product_groups=[["latte"]], size="mediano"),
    clear("price_frappe_matcha", "Precio del Frappé Matcha grande, por favor.", "check_availability", product_groups=[["frappe", "matcha"]], size="grande"),
    clear("price_iced_caramel", "Dime cuánto sale un Iced Caramel Macchiato grande.", "check_availability", product_groups=[["caramel", "macchiato"]], size="grande"),
    clear("price_hot_chocolate", "¿Cuánto cuesta el chocolate caliente mediano?", "check_availability", product_groups=[["chocolate"]], size="mediano"),
    clear("price_manzanilla", "¿A qué precio está el té de manzanilla?", "check_availability", product_groups=[["manzanilla"]], size=None),
]

AMBIGUOUS_CASES = [
    ("ambiguous_recommendation", "¿Me recomiendas algo?"),
    ("ambiguous_size_only", "Quiero uno grande."),
    ("ambiguous_price_only", "¿Cuánto sale?"),
    ("ambiguous_reference", "¿Hay de ese?"),
    ("ambiguous_add", "Quiero que lo agregues."),
]


def normalize(value: object) -> str:
    folded = unicodedata.normalize("NFD", str(value).casefold().replace("_", " "))
    text = "".join(char for char in folded if unicodedata.category(char) != "Mn")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def matches_groups(value: object, groups: list[list[str]]) -> bool:
    actual = f" {normalize(value)} "
    return any(all(f" {normalize(token)} " in actual for token in group) for group in groups)


def validate_arguments(case: dict, arguments: dict) -> bool:
    expect = case["expect"]
    tool = case["expected_tool"]
    if tool == "search_menu":
        query = arguments.get("query")
        return isinstance(query, str) and matches_groups(query, expect["query_groups"])
    if tool == "check_availability":
        product = arguments.get("product_name")
        size = arguments.get("size")
        modifiers = arguments.get("modifiers")
        if not isinstance(product, str) or not matches_groups(product, expect["product_groups"]):
            return False
        if normalize(size or "") != normalize(expect["size"] or ""):
            return False
        if not isinstance(modifiers, list):
            return False
        if len(modifiers) != len(expect.get("modifier_groups", [])):
            return False
        actual_modifiers = " | ".join(str(value) for value in modifiers)
        return all(matches_groups(actual_modifiers, [group]) for group in expect.get("modifier_groups", []))
    if tool == "prepare_order":
        items = arguments.get("items")
        expected_items = expect["items"]
        if not isinstance(items, list) or len(items) != len(expected_items):
            return False
        for item, wanted in zip(items, expected_items, strict=True):
            if not isinstance(item, dict):
                return False
            if not isinstance(item.get("product_name"), str) or not matches_groups(item["product_name"], wanted["product_groups"]):
                return False
            if normalize(item.get("size") or "") != normalize(wanted["size"] or ""):
                return False
            if item.get("quantity") != wanted["quantity"]:
                return False
            modifiers = item.get("modifiers")
            if not isinstance(modifiers, list):
                return False
            if len(modifiers) != len(wanted["modifier_groups"]):
                return False
            actual_modifiers = " | ".join(str(value) for value in modifiers)
            if not all(matches_groups(actual_modifiers, [group]) for group in wanted["modifier_groups"]):
                return False
        return True
    return False


def safe_arguments(tool: str | None, arguments: dict | None) -> dict | None:
    if tool is None or not isinstance(arguments, dict):
        return None
    if tool == "search_menu":
        value = arguments.get("query")
        return {"query": value[:128]} if isinstance(value, str) else {"query": None}
    if tool == "check_availability":
        product = arguments.get("product_name")
        size = arguments.get("size")
        modifiers = arguments.get("modifiers")
        return {
            "product_name": product[:128] if isinstance(product, str) else None,
            "size": size[:64] if isinstance(size, str) else size if size is None else None,
            "modifiers": [value[:128] for value in modifiers[:3] if isinstance(value, str)] if isinstance(modifiers, list) else None,
        }
    if tool == "prepare_order":
        items = arguments.get("items")
        if not isinstance(items, list):
            return {"items": None}
        return {"items": [
            {
                "product_name": item.get("product_name", "")[:128] if isinstance(item, dict) and isinstance(item.get("product_name"), str) else None,
                "size": item.get("size") if isinstance(item, dict) and item.get("size") is None else item.get("size", "")[:64] if isinstance(item, dict) and isinstance(item.get("size"), str) else None,
                "quantity": item.get("quantity") if isinstance(item, dict) and type(item.get("quantity")) is int else None,
                "modifiers": [value[:128] for value in item.get("modifiers", [])[:3] if isinstance(value, str)] if isinstance(item, dict) and isinstance(item.get("modifiers"), list) else None,
            }
            for item in items[:10]
        ]}
    return None


def dotenv_is_tracked() -> bool:
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"], cwd=ROOT, capture_output=True, check=False
    )
    return result.returncode == 0


async def evaluate_case(router: GroqToolRouter, case: dict, *, ambiguous: bool = False) -> dict:
    retries = 0
    started = time.perf_counter()
    while True:
        try:
            result = await router.route(case["text"])
            break
        except ModelUnavailable as exc:
            if exc.code == "rate_limited" and retries < MAX_RATE_LIMIT_RETRIES:
                retries += 1
                await asyncio.sleep(max(exc.retry_after or 15.0, 10.0))
                continue
            return {
                "id": case["id"], "passed": False, "route_correct": False,
                "arguments_correct": False if not ambiguous else None,
                "failure_code": exc.code, "http_status": exc.status_code,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "rate_limit_retries": retries,
            }
    route = result.name if result else None
    arguments = result.arguments if result else None
    route_correct = route is None if ambiguous else route == case["expected_tool"]
    args_correct = None if ambiguous or result is None or not route_correct else validate_arguments(case, arguments)
    passed = route_correct if ambiguous else route_correct and args_correct
    return {
        "id": case["id"], "passed": passed, "route_correct": route_correct,
        "arguments_correct": args_correct, "expected_tool": None if ambiguous else case["expected_tool"],
        "actual_tool": route, "arguments": safe_arguments(route, arguments),
        "latency_ms": result.latency_ms if result else round((time.perf_counter() - started) * 1000),
        "input_tokens": result.input_tokens if result else None,
        "output_tokens": result.output_tokens if result else None,
        "rate_limit_retries": retries,
    }


async def main() -> int:
    load_dotenv(ROOT / ".env")
    settings = GroqSettings.from_env()
    if not settings.api_key:
        print("GROQ_API_KEY no está configurada en el entorno privado; no se ejecutó la evaluación.")
        return 2
    if dotenv_is_tracked():
        print("La evaluación se detuvo porque .env aparece versionado.")
        return 2

    router = GroqToolRouter(settings)
    results = []
    halted = False
    try:
        total = len(CLEAR_CASES) + len(AMBIGUOUS_CASES)
        for index, case in enumerate(CLEAR_CASES):
            result = await evaluate_case(router, case)
            results.append(result)
            print(f"[{index + 1}/{total}] {case['id']}: route={int(result['route_correct'])} args={int(bool(result['arguments_correct']))} latency_ms={result['latency_ms']}", flush=True)
            if result.get("failure_code"):
                halted = True
                break
            if index + 1 < total:
                await asyncio.sleep(INTER_REQUEST_SECONDS)
        if not halted:
            for offset, (case_id, text) in enumerate(AMBIGUOUS_CASES, start=len(CLEAR_CASES)):
                result = await evaluate_case(router, {"id": case_id, "text": text}, ambiguous=True)
                results.append(result)
                print(f"[{offset + 1}/{total}] {case_id}: clarification={int(result['route_correct'])} latency_ms={result['latency_ms']}", flush=True)
                if result.get("failure_code"):
                    halted = True
                    break
                if offset + 1 < total:
                    await asyncio.sleep(INTER_REQUEST_SECONDS)
    finally:
        await router.aclose()

    clear_results = results[:len(CLEAR_CASES)]
    ambiguous_results = results[len(CLEAR_CASES):]
    latencies = [row["latency_ms"] for row in results if isinstance(row.get("latency_ms"), int)]
    summary = {
        "date": datetime.now(UTC).date().isoformat(),
        "model": settings.model,
        "clear_cases_expected": len(CLEAR_CASES),
        "clear_cases_run": len(clear_results),
        "clear_routes_correct": sum(row["route_correct"] for row in clear_results),
        "clear_route_pct": round(100 * sum(row["route_correct"] for row in clear_results) / len(clear_results), 1) if clear_results else 0.0,
        "complete_clear_proposals_correct": sum(bool(row["passed"]) for row in clear_results),
        "complete_clear_proposal_pct": round(100 * sum(bool(row["passed"]) for row in clear_results) / len(clear_results), 1) if clear_results else 0.0,
        "ambiguous_cases_expected": len(AMBIGUOUS_CASES),
        "ambiguous_cases_run": len(ambiguous_results),
        "ambiguous_without_tool": sum(row["route_correct"] for row in ambiguous_results),
        "ambiguous_without_tool_pct": round(100 * sum(row["route_correct"] for row in ambiguous_results) / len(ambiguous_results), 1) if ambiguous_results else 0.0,
        "complete": len(clear_results) == len(CLEAR_CASES) and len(ambiguous_results) == len(AMBIGUOUS_CASES) and not halted,
        "rate_limit_retries": sum(row.get("rate_limit_retries", 0) for row in results),
        "failed_requests": sum(bool(row.get("failure_code")) for row in results),
        "total_input_tokens": sum(row.get("input_tokens") or 0 for row in results),
        "total_output_tokens": sum(row.get("output_tokens") or 0 for row in results),
        "median_latency_ms": round(statistics.median(latencies)) if latencies else None,
        "max_latency_ms": max(latencies) if latencies else None,
        "cases": results,
    }
    temp = OUTPUT.with_suffix(".json.tmp")
    temp.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(OUTPUT)
    print("SUMMARY " + json.dumps({key: value for key, value in summary.items() if key != "cases"}, ensure_ascii=False))
    await asyncio.sleep(0)
    passed = (
        summary["complete"]
        and summary["clear_route_pct"] >= 90.0
        and summary["complete_clear_proposal_pct"] >= 90.0
        and summary["ambiguous_without_tool_pct"] == 100.0
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
