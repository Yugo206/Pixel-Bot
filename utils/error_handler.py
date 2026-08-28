import asyncio
import logging
import os
import time
from datetime import datetime, timezone

import discord

from utils.database import get_pool

# Anti-spam : au plus un MP par (module, niveau) toutes les X secondes, pour éviter
# de se faire flood si une erreur se répète en boucle (ex: ticket_watcher toutes les 2 min).
DM_COOLDOWN = int(os.getenv("DM_ERROR_COOLDOWN", "300"))


class DiscordErrorHandler(logging.Handler):
    """Branché sur le logger racine (voir start.py) : sur toute erreur (ERROR ou plus),
    prévient le propriétaire du bot en MP (avec anti-spam). Si c'est CRITICAL, garde
    aussi une trace dans la table `error` pour investigation ultérieure.

    Ne doit jamais lever ni relogger d'exception : un handler de logging qui échoue
    en boucle sur lui-même serait pire que l'absence d'alerte.
    """

    def __init__(self, bot: discord.Client, owner_id: int):
        super().__init__(level=logging.ERROR)
        self.bot = bot
        self.owner_id = owner_id
        self._last_sent: dict[tuple[str, int], float] = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            asyncio.get_running_loop().create_task(self._handle(record))
        except RuntimeError:
            pass  # Pas de boucle asyncio active (ex: erreur avant le démarrage du bot).

    async def _handle(self, record: logging.LogRecord) -> None:
        try:
            key = (record.name, record.levelno)
            now = time.monotonic()
            if now - self._last_sent.get(key, 0) >= DM_COOLDOWN:
                self._last_sent[key] = now
                await self._notify_owner(record)

            if record.levelno >= logging.CRITICAL:
                await self._store_error(record)
        except Exception:
            pass

    async def _notify_owner(self, record: logging.LogRecord) -> None:
        user = self.bot.get_user(self.owner_id) or await self.bot.fetch_user(self.owner_id)
        text = f"🚨 **[{record.levelname}] {record.name}**\n```{record.getMessage()[:1900]}```"
        await user.send(text)

    async def _store_error(self, record: logging.LogRecord) -> None:
        try:
            pool = get_pool()
        except RuntimeError:
            return  # Pool pas encore prêt (erreur survenue avant create_pool()).

        async with pool.acquire() as conn:
            async with conn.cursor() as c:
                await c.execute(
                    "INSERT INTO error (created_at, created_at_iso, level, source, message, traceback) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        int(time.time()),
                        datetime.now(timezone.utc).isoformat(),
                        record.levelname,
                        record.name,
                        record.getMessage(),
                        self._format_traceback(record),
                    ),
                )
            await conn.commit()

    @staticmethod
    def _format_traceback(record: logging.LogRecord) -> str | None:
        if record.exc_info:
            return logging.Formatter().formatException(record.exc_info)
        return None
