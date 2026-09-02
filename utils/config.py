"""Cache en mémoire de la table `config`, qui porte depuis la migration
_migrate_env_to_config (voir utils/setupdatabase.py) la plupart des réglages
autrefois en .env : IDs de rôles/salons, cooldowns, montants — tout ce qui n'a
pas besoin d'exister avant que le pool de connexions ne soit créé (contrairement
à DISCORD_TOKEN ou aux DB_*, qui restent dans .env).

Chargé une seule fois au démarrage (voir load_config, appelé dans start.py après
create_pool()+init_db() et avant le chargement des cogs) : get_config() est
ensuite un simple accès dict, synchrone, utilisable même au niveau module d'un
cog (à l'import), là où une requête DB ne le serait pas.
"""
import logging

logger = logging.getLogger(__name__)

_config: dict[str, str] = {}


async def load_config(pool) -> None:
    """Charge la table `config` en mémoire. À appeler une seule fois au démarrage,
    avant tout code qui lit get_config()."""
    global _config
    async with pool.acquire() as conn:
        async with conn.cursor() as c:
            await c.execute("SELECT cle, valeur FROM config")
            _config = {cle: valeur for cle, valeur in await c.fetchall()}
    logger.info(f"[config] {len(_config)} réglage(s) chargé(s) depuis la base.")


def get_config(cle: str, default=None):
    """Équivalent de os.getenv(cle, default), mais lit le cache chargé par
    load_config() au lieu de l'environnement."""
    return _config.get(cle, default)
