import discord
from discord import app_commands
from discord.ext import commands
from utils.database import get_pool

OWNER_ID = 1377571267108143194  # 🔒 TON ID

# ======================
# DB HELPERS
# ======================
def split_message(text: str, limit: int = 1900):
    return [text[i:i+limit] for i in range(0, len(text), limit)]


async def get_tables():
    pool = get_pool()
    async with pool.acquire() as db:
        async with db.cursor() as c:
            await c.execute("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE()")
            return [r[0] for r in await c.fetchall()]

async def get_columns(table: str):
    pool = get_pool()
    async with pool.acquire() as db:
        async with db.cursor() as c:
            await c.execute(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                (table,)
            )
            return [r[0] for r in await c.fetchall()]

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
            for t in await get_tables()
            if current.lower() in t.lower()
        ][:25]

    # ======================
    # AUTOCOMPLETE COLUMN
    # ======================
    async def column_autocomplete(self, interaction: discord.Interaction, current: str):
        table = getattr(interaction.namespace, "table", None)
        if not table:
            return []

        return [
            app_commands.Choice(name=c, value=c)
            for c in await get_columns(table)
            if current.lower() in c.lower()
        ][:25]

    async def action_autocomplete(self, interaction: discord.Interaction, current: str):
        actions = ["Modifier", "Ajouter", "Detruire"]
        return [
            app_commands.Choice(name=a, value=a)
            for a in actions
            if current.lower() in a.lower()
        ]

    # ======================
    # 👀 VOIR
    # ======================
    @app_commands.command(name="db_view", description="Voir des données avancées de la DB")
    @app_commands.checks.has_permissions(administrator=True)
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

        if table not in await get_tables():
            await interaction.response.send_message("❌ Table invalide.", ephemeral=True)
            return

        if column_filter and column_filter not in await get_columns(table):
            await interaction.response.send_message("❌ Colonne de filtre invalide.", ephemeral=True)
            return

        pool = get_pool()
        async with pool.acquire() as db:
            async with db.cursor() as c:
                # Requête dynamique (table/colonne validées ci-dessus contre le schéma réel)
                if column_filter and filter_value:
                    query = f"""
                    SELECT * FROM {table}
                    WHERE {column_filter} = %s
                    """
                    await c.execute(query, (filter_value,))
                else:
                    query = f"SELECT * FROM {table} LIMIT 20"
                    await c.execute(query)

                rows = await c.fetchall()
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
        full_text = "\n\n".join(messages)
        chunks = split_message(full_text)

        await interaction.response.send_message(
            f"📊 Résultats ({len(chunks)} page(s)) :",
            ephemeral=True
        )

        for chunk in chunks:
            await interaction.followup.send(
                f"```{chunk}```",
                ephemeral=True
            )

    # ======================
    # ✏️ MODIFIER
    # ======================
    @app_commands.command(name="db_edit", description="Modifier la base de données")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.autocomplete(
        table=table_autocomplete,
        column_set=column_autocomplete,
        column_where=column_autocomplete,
        action=action_autocomplete
    )
    async def db_edit(
            self,
            interaction: discord.Interaction,
            table: str,
            column_set: str,
            value_set: str,
            column_where: str,
            value_where: str,
            action: str
    ):
        # 🔒 Sécurité
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("❌ Accès refusé.", ephemeral=True)
            return

        if table not in await get_tables():
            await interaction.response.send_message("❌ Table invalide.", ephemeral=True)
            return

        columns = await get_columns(table)
        if column_set not in columns or column_where not in columns:
            await interaction.response.send_message("❌ Colonne invalide.", ephemeral=True)
            return

        try:
            pool = get_pool()
            async with pool.acquire() as con:
                async with con.cursor() as cur:
                    if action == "Modifier":
                        query = f"""
                        UPDATE {table}
                        SET {column_set} = %s
                        WHERE {column_where} = %s
                        """
                        await cur.execute(query, (value_set, value_where))
                        await con.commit()

                        await interaction.response.send_message(
                            f"✅ {cur.rowcount} ligne(s) modifiée(s)\n"
                            f"`{table}.{column_set}` ← `{value_set}`\n"
                            f"Condition : `{column_where} = {value_where}`",
                            ephemeral=True
                        )

                    elif action == "Ajouter":
                        query = f"INSERT INTO {table} ({column_set}) VALUES (%s)"
                        await cur.execute(query, (value_set,))
                        await con.commit()

                        await interaction.response.send_message(
                            f"✅ Ligne ajoutée dans `{table}`\n"
                            f"`{column_set}` = `{value_set}`",
                            ephemeral=True
                        )

                    elif action == "Detruire":
                        query = f"DELETE FROM {table} WHERE {column_where} = %s"
                        await cur.execute(query, (value_where,))
                        await con.commit()

                        await interaction.response.send_message(
                            f"🗑️ {cur.rowcount} ligne(s) supprimée(s)\n"
                            f"Condition : `{column_where} = {value_where}`",
                            ephemeral=True
                        )

                    else:
                        await interaction.response.send_message("❌ Action invalide.", ephemeral=True)

        except Exception as e:
            await interaction.response.send_message(f"❌ Erreur SQL : {e}", ephemeral=True)


async def setup(bot):
    await bot.add_cog(DatabaseCog(bot))
