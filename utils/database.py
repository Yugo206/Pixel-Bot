import os

import aiomysql
from pymysql.constants import CLIENT

_pool: aiomysql.Pool | None = None


async def create_pool() -> aiomysql.Pool:
    """Crée le pool de connexions MariaDB. À appeler une seule fois au démarrage du bot."""
    global _pool
    if _pool is not None:
        return _pool

    _pool = await aiomysql.create_pool(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        db=os.getenv("DB_NAME"),
        charset="utf8mb4",
        autocommit=False,
        minsize=1,
        maxsize=10,
        # FOUND_ROWS : cursor.rowcount reflète les lignes correspondant au WHERE
        # (comme sqlite3), et non seulement celles dont la valeur a effectivement changé.
        # Important pour les UPDATE conditionnels utilisés comme vérification atomique
        # (ex: déduction d'argent en boutique).
        client_flag=CLIENT.FOUND_ROWS,
    )
    return _pool


def get_pool() -> aiomysql.Pool:
    """Renvoie le pool déjà créé par create_pool(). Lève une erreur claire si appelé trop tôt."""
    if _pool is None:
        raise RuntimeError(
            "Le pool de connexions MariaDB n'est pas initialisé : create_pool() doit être "
            "appelé (et attendu) au démarrage du bot avant toute requête."
        )
    return _pool


async def close_pool():
    global _pool
    if _pool is not None:
        _pool.close()
        await _pool.wait_closed()
        _pool = None


RARETES_VALIDES = {
    "commun", "rare", "epique", "mytique", "legendaire", "secret"
}


async def ajouter_rarete(user_id: int, rarete: str):
    if rarete not in RARETES_VALIDES:
        raise ValueError("Rareté invalide")

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT IGNORE INTO utilisateurs (user_id) VALUES (%s)",
                (user_id,)
            )
            # Nom de colonne validé ci-dessus (dans RARETES_VALIDES) : sûr à interpoler.
            await cur.execute(
                f"UPDATE utilisateurs SET {rarete} = {rarete} + 1 WHERE user_id = %s",
                (user_id,)
            )
        await conn.commit()
