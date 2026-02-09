import sqlite3
import discord
from discord import app_commands
from discord.ext import commands
from utils.setupdatabase import DB_PATH

OWNER_ID = 1377571267108143194  # 🔒 TON ID

# ======================
# DB HELPERS
# ======================

def get_tables():
    with sqlite3.connect(DB_PATH) as db:
        c = db.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return [r[0] for r in c.fetchall()]

def get_columns(table: str):
    with sqlite3.connect(DB_PATH) as db:
        c = db.cursor()
        c.execute(f"PRAGMA table_info({table})")
        return [r[1] for r in c.fetchall()]

# ======================
# COG
# ======================
class DatabaseCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ======================
    # AUTOCOMPLETE TABLE
    # ======================
    async def table_autocomplete(self, interaction: discord.Interaction, current: str):
        return [
            app_commands.Choice(name=t, value=t)
            for t in get_tables()
            if current.lower() in t.lower()
        ][:25]

    # ======================
    # AUTOCOMPLETE COLUMN
    # ======================
    async def column_autocomplete(self, interaction: discord.Interaction, current: str):
        table = interaction.namespace.table
        if not table:
            return []

        return [
            app_commands.Choice(name=c, value=c)
            for c in get_columns(table)
            if current.lower() in c.lower()
        ][:25]

    # ======================
    # 👀 VOIR
    # ======================
    @app_commands.command(name="db_view", description="Voir des données avancées de la DB")
    @app_commands.autocomplete(
        table=table_autocomplete,
        column_info=column_autocomplete,
        column_filter=column_autocomplete
    )
    async def db_view(
            self,
            interaction: discord.Interaction,
            table: str,
            column_info: str,
            column_filter: str | None = None,
            filter_value: str | None = None
    ):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("❌ Accès refusé", ephemeral=True)
            return

        with sqlite3.connect(DB_PATH) as db:
            c = db.cursor()

            # Requête dynamique
            if column_filter and filter_value:
                query = f"""
                SELECT * FROM {table}
                WHERE {column_filter} = ?
                """
                c.execute(query, (filter_value,))
            else:
                query = f"SELECT * FROM {table} LIMIT 20"
                c.execute(query)

            rows = c.fetchall()
            columns = [desc[0] for desc in c.description]

        if not rows:
            await interaction.response.send_message(
                "📭 Aucun résultat.",
                ephemeral=True
            )
            return

        # Construction réponse lisible
        messages = []
        for row in rows:
            data = dict(zip(columns, row))

            bloc = []
            bloc.append(f"🧾 **Entrée `{table}`**")

            for col, val in data.items():
                bloc.append(f"• `{col}` : `{val}`")

            messages.append("\n".join(bloc))

        # Discord limite 2000 caractères
        output = "\n\n".join(messages)
        output = output[:1900]

        await interaction.response.send_message(
            output,
            ephemeral=True
        )

    # ======================
    # ✏️ MODIFIER
    # ======================
    async def table_autocomplete(self, interaction: discord.Interaction, current: str):
        return [
            app_commands.Choice(name=t, value=t)
            for t in get_tables()
            if current.lower() in t.lower()
        ][:25]

    # ======================
    # AUTOCOMPLETE COLUMN
    # ======================
    async def column_autocomplete(self, interaction: discord.Interaction, current: str):
        table = interaction.namespace.table
        if not table:
            return []

        return [
            app_commands.Choice(name=c, value=c)
            for c in get_columns(table)
            if current.lower() in c.lower()
        ][:25]
    @app_commands.command(name="db_edit", description="Modifier une valeur dans la base de données")
    @app_commands.autocomplete(
        table=table_autocomplete,
        column_set=column_autocomplete,
        column_where=column_autocomplete
    )
    async def db_edit(
            self,
            interaction: discord.Interaction,
            table: str,
            column_set: str,
            value_set: str,
            column_where: str,
            value_where: str
    ):
        # 🔒 Sécurité : toi uniquement
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("❌ Accès refusé.", ephemeral=True)
            return

        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()

        try:
            query = f"""
                UPDATE {table}
                SET {column_set} = ?
                WHERE {column_where} = ?
                """
            cur.execute(query, (value_set, value_where))
            con.commit()

            await interaction.response.send_message(
                f"✅ {cur.rowcount} ligne(s) modifiée(s)\n"
                f"`{table}.{column_set}` ← `{value_set}`\n"
                f"Condition : `{column_where} = {value_where}`",
                ephemeral=True
            )

        except Exception as e:
            await interaction.response.send_message(f"❌ Erreur SQL : {e}", ephemeral=True)

        finally:
            con.close()


async def setup(bot):
    await bot.add_cog(DatabaseCog(bot))





