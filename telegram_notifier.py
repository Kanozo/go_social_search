from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Union
#import socks


from telethon import TelegramClient
from telethon.sessions import StringSession # Importante para usar SESSION_STRING
from telethon.errors import (
    FloodWaitError,
    AuthKeyUnregisteredError,
    RPCError,
    SessionPasswordNeededError,
)

logger = logging.getLogger(__name__)

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
        #PROXY = (socks.HTTP, '192.168.30.120', 3128, True)
        self._client = TelegramClient(session_storage, api_id, api_hash)#, proxy=PROXY)
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