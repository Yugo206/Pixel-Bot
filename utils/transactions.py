"""Historique des mouvements d'argent (table `transactions`) — alimenté depuis
cogs/boutique.py (achat) et cogs/profile.py (don, /daily), consulté via le select
"Actions" attaché à /profil (voir ProfilActionsSelect dans cogs/profile.py).

Pur journal : jamais relu pour recalculer un solde, seulement pour affichage.
"""
import time


async def log_transaction(cursor, user_id: int, type_: str, montant: int, detail: str) -> None:
    """Enregistre une ligne d'historique. `cursor` doit être ouvert sur la même
    transaction DB que le mouvement d'argent qu'elle journalise, et appelé avant
    le commit englobant : écriture et mouvement restent ainsi atomiques ensemble."""
    await cursor.execute(
        "INSERT INTO transactions (user_id, type, montant, detail, created_at) VALUES (%s, %s, %s, %s, %s)",
        (user_id, type_, montant, detail, int(time.time()))
    )
