import os
import ssl

import aiomysql
from pymysql.constants import CLIENT

_pool: aiomysql.Pool | None = None


def _build_ssl_context() -> ssl.SSLContext | None:
    """Contexte SSL pour la connexion à MariaDB, activé uniquement si DB_SSL=true.

    Désactivé par défaut : une base locale ou sur le même serveur que le bot
    (réseau interne / socket) n'en a généralement pas besoin. À activer
    explicitement pour un hébergeur distant qui l'exige (ex: alwaysdata),
    sans rien changer au code — juste la variable d'env DB_SSL. DB_SSL_CA
    permet de fournir un certificat CA custom si l'hébergeur en a besoin ;
    sinon le magasin de confiance système est utilisé.
    """
    if os.getenv("DB_SSL", "false").strip().lower() not in ("1", "true", "yes", "on"):
        return None

    ca_path = os.getenv("DB_SSL_CA")
    return ssl.create_default_context(cafile=ca_path) if ca_path else ssl.create_default_context()


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
        # minsize=1 : aucune connexion inactive superflue au repos. maxsize abaissé
        # de 10 (défaut d'origine, surdimensionné pour ce bot) à 5 : réduit le nombre
        # maximal de connexions ouvertes simultanément (mémoire côté process ET côté
        # serveur MariaDB) sous charge, tout en restant largement suffisant vu le
        # volume de requêtes concurrentes réel du bot. Réglable via DB_POOL_MINSIZE/
        # DB_POOL_MAXSIZE si besoin (ex: plus de marge sur un futur VPS dédié).
        minsize=int(os.getenv("DB_POOL_MINSIZE", "1")),
        maxsize=int(os.getenv("DB_POOL_MAXSIZE", "5")),
        ssl=_build_ssl_context(),
        # Recycle les connexions inactives avant que le serveur (souvent plus
        # agressif sur de l'hébergement mutualisé) ne les ferme lui-même côté
        # TCP — évite les "MySQL server has gone away" sur un bot qui tourne
        # longtemps sans requêtes. Réglable via DB_POOL_RECYCLE (secondes).
        pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "1800")),
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


async def increment_warn(conn, user_id: int) -> int:
    """Incrémente utilisateurs.warn de 1 pour user_id (crée la ligne si besoin) et
    renvoie la nouvelle valeur, de façon atomique.

    Remplace le pattern SELECT puis UPDATE utilisé auparavant pour /warn, la
    satisfaction de ticket et l'acceptation de contestation : deux warns posés au
    même moment pour le même membre lisaient tous les deux l'ancienne valeur et
    écrasaient le résultat l'un de l'autre, perdant un incrément. Ici, le verrou de
    ligne pris par INSERT ... ON DUPLICATE KEY UPDATE est conservé jusqu'au commit
    (autocommit=False), donc le SELECT qui suit sur la même connexion voit toujours
    la valeur qu'on vient d'écrire.

    Prend une connexion déjà acquise par l'appelant et ne commit PAS : tous les
    appelants font suivre cet appel d'une écriture liée (INSERT/DELETE sur warns
    ou contestations) — committer ici séparément romprait l'atomicité entre le
    compteur et cette écriture si l'une des deux échoue."""
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO utilisateurs (user_id, warn) VALUES (%s, 1) "
            "ON DUPLICATE KEY UPDATE warn = COALESCE(warn, 0) + 1",
            (user_id,)
        )
        await cur.execute("SELECT warn FROM utilisateurs WHERE user_id = %s", (user_id,))
        (new_warn,) = await cur.fetchone()
    return new_warn


async def decrement_warn(conn, user_id: int) -> int:
    """Décrémente utilisateurs.warn de 1 pour user_id (sans descendre sous 0) et
    renvoie la nouvelle valeur, de façon atomique — même principe qu'increment_warn
    (connexion fournie par l'appelant, pas de commit ici)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE utilisateurs SET warn = GREATEST(COALESCE(warn, 0) - 1, 0) WHERE user_id = %s",
            (user_id,)
        )
        await cur.execute("SELECT warn FROM utilisateurs WHERE user_id = %s", (user_id,))
        row = await cur.fetchone()
    return row[0] if row and row[0] is not None else 0


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
