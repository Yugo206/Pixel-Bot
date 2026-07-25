import sqlite3

from utils.setupdatabase import DB_PATH

RARETES_VALIDES = {
    "commun", "rare", "epique", "mytique", "legendaire", "secret"
}


def ajouter_rarete(user_id: int, rarete: str):
    if rarete not in RARETES_VALIDES:
        raise ValueError("Rareté invalide")

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        # Crée l'utilisateur s'il n'existe pas (la table est créée par utils.setupdatabase.init_db)
        cursor.execute(
            "INSERT OR IGNORE INTO utilisateurs (user_id) VALUES (?)",
            (user_id,)
        )

        # Incrémente la rareté (nom de colonne validé au-dessus, donc sûr à interpoler)
        cursor.execute(
            f"UPDATE utilisateurs SET {rarete} = {rarete} + 1 WHERE user_id = ?",
            (user_id,)
        )

        conn.commit()
