"""Run the isolated H2 WhatsApp service on loopback only."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from coffee_house.hackathon2.image_ocr import TesseractEngine
from coffee_house.hackathon2.runtime import create_h2_app
from coffee_house.hackathon2.whatsapp import WhatsAppSettings


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    # Local development can use the ignored .env file; VPS services should prefer
    # their private systemd EnvironmentFile, whose values take precedence.
    load_dotenv(root / ".env", override=False)
    state_dir = Path(os.getenv("H2_STATE_DIR", str(root / "state"))).expanduser().absolute()
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    pepper = os.getenv("H2_OTP_PEPPER", "")
    if len(pepper) < 32:
        raise SystemExit("Configura H2_OTP_PEPPER con al menos 32 caracteres aleatorios en el entorno privado.")
    try:
        whatsapp = WhatsAppSettings.from_env()
    except ValueError as exc:
        raise SystemExit("Configura PHONE_NUMBER_ID y META_GRAPH_API_VERSION para el servicio H2.") from exc
    if not whatsapp.verify_token or not whatsapp.app_secret:
        raise SystemExit("Configura WHATSAPP_VERIFY_TOKEN y META_APP_SECRET en el entorno privado.")
    initial_admin = os.getenv("H2_INITIAL_ADMIN_PHONE", "").strip() or None
    catalog_path = Path(os.getenv("H2_CATALOG_PATH", str(root / "data" / "catalog.json"))).expanduser()
    fixture_dir = Path(os.getenv("H2_DEMO_FIXTURE_DIR", str(root / "data" / "demo-hackathon2"))).expanduser()
    guard_url = os.getenv("QWEN_GUARD_URL", "http://127.0.0.1:19091")
    allow_external_text = os.getenv("H2_ALLOW_EXTERNAL_TEXT", "false").strip().casefold() == "true"
    enable_delivery = os.getenv("H2_ENABLE_META_DELIVERY", "false").strip().casefold() == "true"
    if enable_delivery and not whatsapp.access_token:
        raise SystemExit("H2_ENABLE_META_DELIVERY requiere WHATSAPP_ACCESS_TOKEN.")
    ticket_engine = None
    if os.getenv("H2_ENABLE_TICKET_OCR", "false").strip().casefold() == "true":
        try:
            ticket_engine = TesseractEngine()
        except RuntimeError as exc:
            raise SystemExit("H2_ENABLE_TICKET_OCR está activo, pero falta el motor OCR local.") from exc
    app = create_h2_app(
        catalog_path=catalog_path, fixture_dir=fixture_dir,
        database_path=state_dir / "hackathon2.sqlite3", whatsapp=whatsapp,
        guard_url=guard_url, otp_pepper=pepper, initial_admin_phone=initial_admin,
        allow_external_text=allow_external_text, enable_meta_delivery=enable_delivery,
        ticket_engine=ticket_engine,
    )
    host = os.getenv("H2_BIND_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "::1"}:
        raise SystemExit("El servicio H2 solo puede escuchar en loopback; configura H2_BIND_HOST=127.0.0.1.")
    try:
        port = int(os.getenv("H2_PORT", "18795"))
    except ValueError as exc:
        raise SystemExit("H2_PORT debe ser un puerto numérico.") from exc
    if not 1 <= port <= 65535:
        raise SystemExit("H2_PORT está fuera del rango permitido.")
    logging.basicConfig(level=os.getenv("H2_LOG_LEVEL", "INFO").upper())
    uvicorn.run(app, host=host, port=port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
