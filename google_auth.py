#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Utilidad para autenticación manual en Google y persistencia de cookies/storage.

USO:
  1. Ejecutar `python google_auth.py` → abre Firefox, loguearse manualmente
  2. Las cookies y localStorage quedan en 'google_session.json'
  3. El scraper carga ese archivo antes de navegar a google.com

Nota: Google detecta automatización incluso con cookies válidas si el
perfil del navegador no es consistente. Esta utilidad usa el mismo
user_agent y viewport que el scraper para maximizar consistencia.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Final

from playwright.async_api import (
    async_playwright,
    BrowserContext,
    Page,
    StorageState,
)

logger: Final = logging.getLogger("GoogleAuth")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# Debe coincidir exactamente con ScraperConfig para evitar fingerprint mismatch
USER_AGENT: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
    "Gecko/20100101 Firefox/126.0"
)
VIEWPORT: Final = {"width": 1280, "height": 800}
SESSION_FILE: Final = Path("google_session.json")


async def save_google_session(session_path: Path = SESSION_FILE) -> bool:
    """
    Abre Firefox, espera login manual del usuario y persiste cookies + storage.

    El flujo es intencionalmente interactivo: el usuario hace login
    normalmente en el navegador visible. Una vez que detectamos que
    la sesión está activa (presencia de la cookie 'SID' o 'SAPISID'),
    guardamos el estado completo.

    Args:
        session_path: Ruta donde guardar el archivo JSON de sesión.

    Returns:
        True si la sesión fue guardada correctamente, False en caso de error.
    """
    async with async_playwright() as p:
        browser = await p.firefox.launch(
            headless=False,
            firefox_user_prefs={
                "dom.webdriver.enabled": False,
                "useAutomationExtension": False,
            },
        )

        context: BrowserContext = await browser.new_context(
            viewport=VIEWPORT,
            user_agent=USER_AGENT,
            locale="es-CU",
        )

        page: Page = await context.new_page()

        logger.info("🌐 Navegando a login de Google...")
        await page.goto(
            "https://accounts.google.com/signin",
            wait_until="domcontentloaded",
            timeout=30_000,
        )

        logger.info(
            "👤 Completa el login manualmente en el navegador.\n"
            "   El script detectará automáticamente cuando la sesión esté activa."
        )

        # Polling: esperar hasta que las cookies de sesión estén presentes
        session_cookies = {"SID", "SAPISID", "SSID", "APISID", "__Secure-1PSID"}
        max_wait_seconds = 300  # 5 minutos máximo
        poll_interval = 3
        elapsed = 0

        while elapsed < max_wait_seconds:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            cookies = await context.cookies("https://google.com")
            cookie_names = {c["name"] for c in cookies}
            found = session_cookies & cookie_names

            if found:
                logger.info(f"✅ Sesión detectada (cookies: {found})")
                break

            if elapsed % 30 == 0:
                logger.info(f"⏳ Esperando login... ({elapsed}s/{max_wait_seconds}s)")
        else:
            logger.error("❌ Timeout esperando login. Abortando.")
            await browser.close()
            return False

        # Pequeña pausa para que Google termine de escribir todas las cookies
        await asyncio.sleep(3.0)

        # Guardar estado completo: cookies + localStorage + sessionStorage
        storage_state: StorageState = await context.storage_state()

        session_path.write_text(
            json.dumps(storage_state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        cookie_count = len(storage_state.get("cookies", []))
        origin_count = len(storage_state.get("origins", []))
        logger.info(
            f"💾 Sesión guardada en '{session_path}' "
            f"({cookie_count} cookies, {origin_count} origins)"
        )

        await context.close()
        await browser.close()
        return True


def load_session_if_exists(session_path: Path = SESSION_FILE) -> Dict[str, Any]:
    """
    Carga el archivo de sesión JSON si existe y no está vacío.

    Args:
        session_path: Ruta al archivo de sesión generado por save_google_session.

    Returns:
        Dict con el storage_state listo para pasar a browser.new_context(),
        o dict vacío si el archivo no existe o está corrupto.
    """
    if not session_path.exists():
        logger.info(f"ℹ️ No existe sesión guardada en '{session_path}'")
        return {}

    try:
        content = session_path.read_text(encoding="utf-8")
        state = json.loads(content)

        cookie_count = len(state.get("cookies", []))
        if cookie_count == 0:
            logger.warning(f"⚠️ Archivo de sesión '{session_path}' sin cookies")
            return {}

        logger.info(f"✅ Sesión cargada desde '{session_path}' ({cookie_count} cookies)")
        return state

    except (json.JSONDecodeError, OSError) as exc:
        logger.error(f"❌ Error leyendo sesión '{session_path}': {exc}")
        return {}


if __name__ == "__main__":
    success = asyncio.run(save_google_session())
    sys.exit(0 if success else 1)