from __future__ import annotations

import asyncio
import logging
import socks
from pathlib import Path
from typing import Any, Union

from database import PostRepository, SQLiteManager
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from telethon import TelegramClient
from telethon.sessions import StringSession # Importante para usar SESSION_STRING
from telethon.errors import (
    FloodWaitError,
    AuthKeyUnregisteredError,
    RPCError,
    SessionPasswordNeededError,
)

USE_PROXY = False

class TelegramNotifier:
    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session: str | Path,
        group_target: str | int,
    ) -> None:
        # Detectar si es una StringSession o una ruta de archivo
        # Las StringSession de Telethon suelen empezar con '1' o ser muy largas
        session_storage: Union[StringSession, str]
        if isinstance(session, str) and len(session) > 100:
            session_storage = StringSession(session)
        else:
            session_storage = str(Path(session))

        PROXY = (socks.HTTP, '192.168.30.120', 3128, True) if USE_PROXY else None
        self._client = TelegramClient(session_storage, api_id, api_hash, proxy=PROXY)
        self._group_target = group_target
        self._is_connected = False

    async def start(self) -> None:
        try:
            await self._client.connect() # Usar connect() en lugar de start() si ya tienes sesión
            if not await self._client.is_user_authorized():
                 # Si la sesión del string no es válida, esto fallará limpiamente
                 raise RuntimeError("La sesión proporcionada no es válida o ha expirado.")
            
            self._is_connected = True
            logger.info("Cliente de Telegram conectado vía StringSession.")
        except Exception as exc:
            logger.error(f"Error crítico al conectar: {exc}")
            raise

    async def send_message(self, message: str) -> bool:
        if not self._is_connected:
            raise RuntimeError("Cliente no conectado.")

        try:
            await self._client.send_message(self._group_target, message)
            return True
        except FloodWaitError as exc:
            logger.warning(f"Rate limit: esperar {exc.seconds}s.")
            return False
        except RPCError as exc:
            logger.error(f"Error RPC: {exc}")
            return False

    async def disconnect(self) -> None:
        if self._is_connected:
            await self._client.disconnect()
            self._is_connected = False

    async def __aenter__(self) -> TelegramNotifier:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.disconnect()

async def main() -> None:
    # Simulación de carga de variables (usar .env en realidad)
    API_ID = 12880411
    API_HASH = "fac717534b467665c05b1d417df5f30d"
    SESSION_STRING = "1AZWarzoBuwsp1OJGdT3yKstph4RBFSb9-gGWzLZKcD_RvSm7l0YPu0T-f7KiSF_aTXHo70JpIFAYGvRJDCR1pshDYKRRFnb45RuDPziOAwCJ4vctZhwfl0tt5PG3bjn3W30bEYB91qz77aKtBpCKeFCiu4HZCzZeJRBikw2hQSwzmZKyJQ25aPLKWF0RGUKRnhvp3ngCiC3kfKqeVD77Wm69smhrZcTXfbXlPa6Dg0XCB5VPPALDemafPFHarJWLPckvOErDOzt4H-zm7QL2AopPGrKYYXzHdQD495m-ONWxf7mWDx2JV0JNHgU4_LELOIGP7IF6HIUYY77Y9U8hIxehc97v1NI="
    GROUP_TARGET = -1003857299252

    manager = SQLiteManager("url_scraper.db")

    await manager.connect()

    repo = PostRepository(manager)

    try:
        async with TelegramNotifier(API_ID, API_HASH, SESSION_STRING, GROUP_TARGET) as bot:
            while True: 
                pending = await repo.get_pending_send()
                for item in pending:
                    if await bot.send_message(item.url):
                        await repo.mark_sent(item.url)
                        logger.info(f"Notificado y actualizado: {item.url}")
                        await asyncio.sleep(2)
                    else:
                        logger.warning(f"No se pudo enviar: {item.url}")
                await asyncio.sleep(120)

    except Exception as e:
        logger.error(f"Error en el proceso: {e}")
    finally:
        await manager.disconnect()
        logger.info("Conexión a Base de datos cerrada.")

if __name__ == "__main__":
    asyncio.run(main())