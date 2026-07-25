import discord
from discord.ext import commands, tasks
from discord import app_commands
import sqlite3
import time
import os
from dotenv import load_dotenv
load_dotenv()
from utils.setupdatabase import DB_PATH


class AchatSelect(discord.ui.Select):
    def __init__(self):
        items = []
        try:
            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.cursor()
                # La boutique est indexée par "name" (clé primaire de la table shop),
                # il n'y a pas de colonne "id".
                cursor.execute("SELECT name, price, type, valeur, duration FROM shop")
                items = cursor.fetchall()
        except sqlite3.OperationalError as e:
            print(e)

        if not items:
            options = [
                discord.SelectOption(
                    label="Boutique vide",
                    description="Aucun objet disponible",
                    value="__empty__"
                )
            ]
        else:
            options = [
                discord.SelectOption(
                    label=name,
                    description=f"{price} €",
                    value=name
                )
                for name, price, item_type, valeur, duration in items
            ]

        super().__init__(
            placeholder="🛒 Choisis un objet",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        item_name = self.values[0]

        if item_name == "__empty__":
            await interaction.followup.send("❌ La boutique est vide.", ephemeral=True)
            return

        try:
            with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
                cursor = conn.cursor()

                cursor.execute(
                    "SELECT name, price, type, valeur, duration FROM shop WHERE name = ?",
                    (item_name,)
                )
                result = cursor.fetchone()

                if not result:
                    await interaction.followup.send("❌ Objet introuvable.", ephemeral=True)
                    return

                name, price, item_type, valeur, duration = result

                # Déduction atomique et conditionnelle : n'a d'effet que si le solde est
                # suffisant, ce qui évite les doubles achats en cas de clics rapides.
                cursor.execute(
                    "UPDATE utilisateurs SET argent = argent - ? WHERE user_id = ? AND argent >= ?",
                    (price, interaction.user.id, price)
                )

                if cursor.rowcount == 0:
                    conn.rollback()
                    cursor.execute("SELECT argent FROM utilisateurs WHERE user_id = ?", (interaction.user.id,))
                    row = cursor.fetchone()
                    argent = row[0] if row and row[0] is not None else 0
                    await interaction.followup.send(
                        f"❌ Tu n'as pas assez d'argent.\n💰 Prix : {price} € | 💸 Ton solde : {argent} €",
                        ephemeral=True
                    )
                    return

                try:
                    if item_type == 1:
                        role = interaction.guild.get_role(int(valeur))
                        if role is None:
                            try:
                                role = await interaction.guild.fetch_role(int(valeur))
                            except discord.HTTPException:
                                role = None
                        if role is None:
                            conn.rollback()
                            await interaction.followup.send("❌ Rôle introuvable.", ephemeral=True)
                            return

                        await interaction.user.add_roles(role)

                        if duration is not None:
                            expires_at = int(time.time()) + (duration * 86400)
                            cursor.execute(
                                "INSERT INTO shop_temp_roles (user_id, role_id, end_time) VALUES (?, ?, ?)",
                                (interaction.user.id, role.id, expires_at)
                            )
                            print(f"[DB] Rôle temporaire ajouté : user={interaction.user.id}, role={role.id}, expires={expires_at}")

                        conn.commit()
                        await interaction.followup.send(
                            f"🛒 **Achat réussi !**\n\n🎭 Rôle : **{role.name}**\n💰 Prix : **{price} €**",
                            ephemeral=True
                        )

                    elif item_type == 2:
                        cursor.execute(
                            "SELECT quantite FROM inventaire WHERE user_id = ? AND item_id = ?",
                            (interaction.user.id, valeur)
                        )
                        result_inv = cursor.fetchone()

                        if result_inv:
                            cursor.execute(
                                "UPDATE inventaire SET quantite = quantite + 1 WHERE user_id = ? AND item_id = ?",
                                (interaction.user.id, valeur)
                            )
                        else:
                            cursor.execute(
                                "INSERT INTO inventaire (user_id, item_id, quantite) VALUES (?, ?, 1)",
                                (interaction.user.id, valeur)
                            )

                        conn.commit()
                        await interaction.followup.send(
                            f"🛒 **Achat réussi !**\n\n📦 Objet : **{name}**\n💰 Prix : **{price} €**",
                            ephemeral=True
                        )

                    elif item_type == 3:
                        cursor.execute(
                            "UPDATE utilisateurs SET xp = xp + ? WHERE user_id = ?",
                            (valeur, interaction.user.id)
                        )
                        conn.commit()
                        await interaction.followup.send(
                            f"🛒 **Achat réussi !**\n\n📦 Objet : **{name}**\n💰 Prix : **{price} €**",
                            ephemeral=True
                        )

                    else:
                        conn.rollback()
                        await interaction.followup.send("❌ Type d'objet inconnu dans la boutique.", ephemeral=True)
                        return

                except discord.HTTPException as e:
                    conn.rollback()
                    await interaction.followup.send(f"❌ Erreur lors de l'achat : {e}", ephemeral=True)
                    return

        except sqlite3.OperationalError as e:
            print(f"Erreur SQLite achat: {e}")
            await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
            return

        self.disabled = True
        self.placeholder = "Objet acheté ✔"
        await interaction.message.edit(view=self.view)


class BoutiqueCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_temp_roles.start()

    def cog_unload(self):
        self.check_temp_roles.cancel()

    class BoutiqueView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.add_item(AchatSelect())

    @tasks.loop(minutes=5)
    async def check_temp_roles(self):
        """Retire automatiquement les rôles temporaires achetés en boutique une fois expirés."""
        now = int(time.time())

        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, role_id FROM shop_temp_roles WHERE end_time <= ?", (now,))
            expired = c.fetchall()

        if not expired:
            return

        guild_id = os.getenv("GUILD_ID")
        guild = None
        if guild_id:
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                try:
                    guild = await self.bot.fetch_guild(int(guild_id))
                except discord.HTTPException:
                    guild = None

        for row_id, user_id, role_id in expired:
            try:
                if guild is not None:
                    member = guild.get_member(user_id)
                    if member is None:
                        try:
                            member = await guild.fetch_member(user_id)
                        except discord.NotFound:
                            member = None

                    if member is not None:
                        role = guild.get_role(role_id)
                        if role is not None:
                            try:
                                await member.remove_roles(role, reason="Rôle temporaire de boutique expiré")
                            except discord.Forbidden:
                                pass
            except Exception as e:
                print(f"[check_temp_roles] Erreur pour user={user_id} role={role_id} : {e}")
            finally:
                with sqlite3.connect(DB_PATH) as conn:
                    c = conn.cursor()
                    c.execute("DELETE FROM shop_temp_roles WHERE id = ?", (row_id,))
                    conn.commit()

    @check_temp_roles.before_loop
    async def before_check_temp_roles(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="boutique", description="Regarde la boutique")
    async def boutique(self, interaction: discord.Interaction):
        await interaction.response.defer()

        embed = discord.Embed(
            title="🛍 Boutique",
            color=discord.Color.green()
        )

        items = []
        try:
            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name, price, type, duration FROM shop")
                items = cursor.fetchall()
        except sqlite3.OperationalError as e:
            print("ERREUR DE SQL !!! L'erreur est : ", e)

        if not items:
            embed.description = "❌ Boutique vide"
        else:
            for name, price, item_type, duration in items:
                if item_type == 1:
                    type_str = "Rôle"
                    if duration is not None:
                        type_str += " temporaire"
                    else:
                        type_str += " permanent"
                elif item_type == 2:
                    type_str = "Objet d'inventaire"
                elif item_type == 3:
                    type_str = "XP"
                else:
                    type_str = "Inconnu"

                desc = f" **Prix :** {price} €\n **Type :** {type_str}"
                if duration is not None and item_type == 1:
                    desc += f"\n **Durée :** {duration} jours"

                embed.add_field(name=name, value=desc, inline=False)

        await interaction.followup.send(embed=embed, view=self.BoutiqueView())


async def setup(bot):
    await bot.add_cog(BoutiqueCog(bot))
