"""Run a paced Groq intent screen with colloquial, abbreviated Spanish only."""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import statistics
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from coffee_house.hackathon2.model import GroqSettings, GroqToolRouter

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "evaluation" / "t9" / "groq-colloquial-stress-2026-10-09.json"
INTER_REQUEST_SECONDS = 15.0
_threshold_spec = importlib.util.spec_from_file_location(
    "groq_intent_threshold", ROOT / "evaluation" / "t9" / "groq_intent_threshold.py"
)
if _threshold_spec is None or _threshold_spec.loader is None:
    raise RuntimeError("No se pudo cargar el evaluador base de intenciones.")
_threshold = importlib.util.module_from_spec(_threshold_spec)
_threshold_spec.loader.exec_module(_threshold)
clear = _threshold.clear
evaluate_case = _threshold.evaluate_case

CASES = [
    clear("col_menu_latte", "ola, qué latte hay?", "search_menu", query_groups=[["latte"]]),
    clear("col_menu_frappe_oreo", "q tipos de frapes tienen?", "search_menu", query_groups=[["frappe"], ["frappes"]]),
    clear("col_menu_extras", "q extras le puedo echar al café?", "search_menu", query_groups=[["extra"], ["extras"]]),
    clear("col_stock_latte_hot", "tienes latte medianito calientito?", "check_availability", product_groups=[["latte"]], size="mediano"),
    clear("col_stock_oreo_latte_typos", "ola, tiene baso mediano de horeo latttee?", "check_availability", product_groups=[["oreo", "latte"]], size="mediano"),
    clear("col_price_latte_large", "cuánto sale el latte grand?", "check_availability", product_groups=[["latte"]], size="grande"),
    clear("col_price_manzanilla", "me dices a cómo está el té de manzanilla", "check_availability", product_groups=[["manzanilla"]], size=None),
    clear("col_stock_oreo_cold", "hay oreo latte frío mediano?", "check_availability", product_groups=[["oreo", "latte"]], size="mediano"),
    clear("col_stock_matcha_frappe", "el frappé matcha grande todavía hay?", "check_availability", product_groups=[["frappe", "matcha"]], size="grande"),
    clear("col_stock_oat_latte", "me revisas un latte mediano con lechita de avena", "check_availability", product_groups=[["latte"]], size="mediano", modifier_groups=[["leche", "avena"]]),
    clear("col_order_latte_typo", "quiero pedir un latte mediani caliente con leche de avena", "prepare_order", items=[{"product_groups": [["latte"]], "size": "mediano", "quantity": 1, "modifier_groups": [["leche", "avena"]]}]),
    clear("col_order_two_matcha", "ponme dos matchas helados grandes", "prepare_order", items=[{"product_groups": [["matcha"]], "size": "grande", "quantity": 2, "modifier_groups": []}]),
    clear("col_order_oreo_cold", "me das el oreo latte grand pero frío porfa", "prepare_order", items=[{"product_groups": [["oreo", "latte"]], "size": "grande", "quantity": 1, "modifier_groups": []}]),
    clear("col_order_chai_hot", "un chai latte mediano caliente, xfa", "prepare_order", items=[{"product_groups": [["chai", "latte"]], "size": "mediano", "quantity": 1, "modifier_groups": []}]),
    clear("col_order_unknown_extra", "hazme un latte grande con crema", "prepare_order", items=[{"product_groups": [["latte"]], "size": "grande", "quantity": 1, "modifier_groups": [["crema"]]}]),
    clear("col_price_almond_latte", "a cuánto queda un latte mediano con leche de almendra?", "check_availability", product_groups=[["latte"]], size="mediano", modifier_groups=[["leche", "almendra"]]),
    clear("col_order_two_chocolates", "quiero dos chocolates calientes medianos", "prepare_order", items=[{"product_groups": [["chocolate"]], "size": "mediano", "quantity": 2, "modifier_groups": []}]),
    clear("col_stock_iced_oreo", "todavía hay del iced oreo latte grande?", "check_availability", product_groups=[["oreo", "latte"]], size="grande"),
    clear("col_price_matcha_frappe", "cuánto cuesta grande frappe matcha", "check_availability", product_groups=[["frappe", "matcha"]], size="grande"),
    clear("col_stock_mango_tea", "hay té verde mango helado?", "check_availability", product_groups=[["té", "verde", "mango"], ["te", "verde", "mango"]], size=None),
    clear("col_order_two_drinks", "ponme un latte caliente mediano y un iced caramel macchiato grande", "prepare_order", items=[
        {"product_groups": [["latte"]], "size": "mediano", "quantity": 1, "modifier_groups": []},
        {"product_groups": [["caramel", "macchiato"]], "size": "grande", "quantity": 1, "modifier_groups": []},
    ]),
    clear("col_order_two_oreo_frappes", "ponme dos frapes de oreo medianos", "prepare_order", items=[{"product_groups": [["frappe", "oreo"]], "size": "mediano", "quantity": 2, "modifier_groups": []}]),
]

AMBIGUOUS = [
    ("col_ambiguous_price", "cuánto cuesta eso?"),
    ("col_ambiguous_previous", "me das el mismo de ayer?"),
    ("col_ambiguous_size", "uno grande porfa"),
    ("col_ambiguous_add", "agrégalo a mi pedido"),
    ("col_ambiguous_ack", "ta bueno"),
]


async def main(*, only: set[str] | None = None, output: Path = OUTPUT) -> int:
    cases = [case for case in CASES if only is None or case["id"] in only]
    unknown = (only or set()) - {case["id"] for case in CASES}
    if unknown:
        print("IDs de caso no encontrados: " + ", ".join(sorted(unknown)))
        return 2
    ambiguous = [] if only is not None else AMBIGUOUS
    load_dotenv(ROOT / ".env")
    settings = GroqSettings.from_env()
    if not settings.api_key:
        print("GROQ_API_KEY no está configurada en el entorno privado; no se ejecutó la prueba.")
        return 2

    router = GroqToolRouter(settings)
    results: list[dict] = []
    stopped = False
    total = len(cases) + len(ambiguous)
    try:
        for index, case in enumerate(cases):
            result = await evaluate_case(router, case)
            result.pop("remaining_tokens", None)
            result.pop("remaining_requests", None)
            results.append(result)
            print(f"[{index + 1}/{total}] {case['id']}: route={int(result['route_correct'])} args={int(bool(result['arguments_correct']))} latency_ms={result['latency_ms']}", flush=True)
            if result.get("failure_code"):
                stopped = True
                break
            if index + 1 < total:
                await asyncio.sleep(INTER_REQUEST_SECONDS)

        if not stopped:
            offset = len(cases)
            for index, (case_id, text) in enumerate(ambiguous):
                result = await evaluate_case(router, {"id": case_id, "text": text}, ambiguous=True)
                result.pop("remaining_tokens", None)
                result.pop("remaining_requests", None)
                results.append(result)
                print(f"[{offset + index + 1}/{total}] {case_id}: clarification={int(result['route_correct'])} latency_ms={result['latency_ms']}", flush=True)
                if result.get("failure_code"):
                    stopped = True
                    break
                if offset + index + 1 < total:
                    await asyncio.sleep(INTER_REQUEST_SECONDS)
    finally:
        await router.aclose()

    clear_results = results[:len(cases)]
    ambiguous_results = results[len(cases):]
    latencies = [row["latency_ms"] for row in results if isinstance(row.get("latency_ms"), int)]
    clear_passed = sum(bool(row.get("passed")) for row in clear_results)
    ambiguous_passed = sum(bool(row.get("route_correct")) for row in ambiguous_results)
    summary = {
        "date": datetime.now(UTC).date().isoformat(),
        "model": settings.model,
        "suite": "colloquial-typos-abbreviations-multi-item",
        "selected_case_ids": [case["id"] for case in cases],
        "clear_cases_expected": len(cases),
        "clear_cases_run": len(clear_results),
        "clear_routes_correct": sum(bool(row.get("route_correct")) for row in clear_results),
        "complete_clear_proposals_correct": clear_passed,
        "clear_proposal_pct": round(100 * clear_passed / len(cases), 1) if cases else 100.0,
        "ambiguous_cases_expected": len(ambiguous),
        "ambiguous_cases_run": len(ambiguous_results),
        "ambiguous_without_tool": ambiguous_passed,
        "ambiguous_without_tool_pct": round(100 * ambiguous_passed / len(ambiguous), 1) if ambiguous else None,
        "complete": len(clear_results) == len(cases) and len(ambiguous_results) == len(ambiguous) and not stopped,
        "rate_limit_retries": sum(row.get("rate_limit_retries", 0) for row in results),
        "failed_requests": sum(bool(row.get("failure_code")) for row in results),
        "total_input_tokens": sum(row.get("input_tokens") or 0 for row in results),
        "total_output_tokens": sum(row.get("output_tokens") or 0 for row in results),
        "median_latency_ms": round(statistics.median(latencies)) if latencies else None,
        "max_latency_ms": max(latencies) if latencies else None,
        "cases": results,
    }
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    print("SUMMARY " + json.dumps({key: value for key, value in summary.items() if key != "cases"}, ensure_ascii=False))
    ambiguity_passed = summary["ambiguous_without_tool_pct"] in (None, 100.0)
    return 0 if summary["complete"] and summary["clear_proposal_pct"] >= 90 and ambiguity_passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", action="append", help="ID sintético que se volverá a probar; repítelo para varios.")
    parser.add_argument("--output", type=Path, default=OUTPUT, help="Ruta del JSON saneado de resultados.")
    arguments = parser.parse_args()
    selected = set(arguments.only) if arguments.only else None
    raise SystemExit(asyncio.run(main(only=selected, output=arguments.output)))
