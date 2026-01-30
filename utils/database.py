import discord
from discord.ext import commands
from discord import app_commands
import random
import sqlite3

FILE = "database.db"

def ajouter_rarete(user_id: int, rarete: str):
    RARETES_VALIDES = {
        "commun", "rare", "epique", "mytique", "legendaire", "secret"
    }

    if rarete not in RARETES_VALIDES:
        raise ValueError("Rareté invalide")
    conn = sqlite3.connect(FILE)
    cursor = conn.cursor()

    # On verifie si la table existe
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS utilisateurs (
            user_id INTEGER PRIMARY KEY,
            commun INTEGER DEFAULT 0,
            rare INTEGER DEFAULT 0,
            epique INTEGER DEFAULT 0,
            mytique INTEGER DEFAULT 0,
            legendaire INTEGER DEFAULT 0,
            secret INTEGER DEFAULT 0
        )
        """)

    # Crée l'utilisateur s'il n'existe pas
    cursor.execute("""
    INSERT OR IGNORE INTO utilisateurs (user_id)
    VALUES (?)
    """, (user_id,))

    # Incrémente la rareté
    cursor.execute(f"""
    UPDATE utilisateurs
    SET {rarete} = {rarete} + 1
    WHERE user_id = ?
    """, (user_id,))

    conn.commit()
    conn.close()
