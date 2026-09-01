"""Cache mémoire minimal pour éviter de refaire un aller-retour DB à chaque
message pour lire l'XP d'un membre (voir on_message dans cogs/events.py).

Volontairement minimaliste : un simple dict {user_id: xp}, pas de TTL/LRU — la
taille reste bornée par le nombre de membres actifs d'un serveur Discord (donc
négligeable en mémoire), et une entrée est supprimée dès que le membre quitte
le serveur (voir invalidate_xp, appelé depuis on_member_remove).

Toute écriture SQL directe sur utilisateurs.xp ailleurs dans le code (ex: achat
en boutique qui donne de l'XP) DOIT appeler invalidate_xp() juste après, pour
ne pas laisser le cache dériver par rapport à la base.
"""

import aiomysql

_xp_cache: dict[int, int] = {}


async def get_xp(pool: aiomysql.Pool, user_id: int) -> int:
    """Renvoie l'XP actuelle d'un membre depuis le cache si possible, sinon va la
    chercher en base (et crée la ligne si elle n'existe pas encore — même
    comportement que l'ancien INSERT IGNORE + SELECT, mais un seul aller-retour
    DB au lieu de deux, et plus aucun après la première fois)."""
    if user_id in _xp_cache:
        return _xp_cache[user_id]

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT IGNORE INTO utilisateurs (user_id, xp) VALUES (%s, 40)",
                (user_id,)
            )
            await cur.execute("SELECT xp FROM utilisateurs WHERE user_id = %s", (user_id,))
            xp = (await cur.fetchone())[0]
        await conn.commit()

    # setdefault (pas une affectation directe) : si un autre appel concurrent pour
    # ce même membre a rempli le cache pendant cet aller-retour DB (et l'a déjà
    # fait avancer via bump_xp, ex: deux premiers messages très rapprochés d'un
    # membre pas encore en cache), on ne veut pas écraser cette valeur plus
    # fraîche avec celle, désormais périmée, qu'on vient de lire.
    _xp_cache.setdefault(user_id, xp)
    return _xp_cache[user_id]


def bump_xp(user_id: int, gain: int) -> int:
    """Incrémente le cache de `gain` (peut être négatif) et renvoie la nouvelle
    valeur. À appeler de façon synchrone, sans await entre la lecture (get_xp)
    et cet appel, pour qu'aucun autre message du même membre ne puisse
    s'intercaler avec une valeur périmée."""
    nouvelle_valeur = _xp_cache.get(user_id, 0) + gain
    _xp_cache[user_id] = nouvelle_valeur
    return nouvelle_valeur


def invalidate_xp(user_id: int) -> None:
    """À appeler après toute écriture SQL directe sur utilisateurs.xp qui ne
    passe pas par bump_xp (ex: achat boutique), ou quand le membre quitte le
    serveur — force une relecture DB au prochain accès plutôt que de garder
    une valeur périmée en mémoire."""
    _xp_cache.pop(user_id, None)
