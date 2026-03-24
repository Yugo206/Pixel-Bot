import discord
from discord.ext import commands
from discord import app_commands
import sqlite3
import time
from dotenv import load_dotenv
load_dotenv()
from utils.setupdatabase import DB_PATH


class BoutiqueCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    class AchatSelect(discord.ui.Select):
        def __init__(self):
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            try:
                items = []
                cursor.execute("SELECT id, name, price, type, valeur, duration FROM shop")
                items = cursor.fetchall()
            except sqlite3.OperationalError as e:
                print(e)
            conn.close()

            if not items:
                options = [
                    discord.SelectOption(
                        label="Boutique vide",
                        description="Aucun objet disponible",
                        value="0"
                    )
                ]
            else:
                try:
                    options = [
                        discord.SelectOption(
                            label=name,
                            description=f"{price} €",
                            value=str(item_id)
                        )
                        for item_id, name, price, type, valeur, duration in items
                    ]
                except Exception as e:
                    print(e)


            super().__init__(
                placeholder="🛒 Choisis un objet",
                min_values=1,
                max_values=1,
                options=options
            )

        async def callback(self, interaction: discord.Interaction):
            item_id = int(self.values[0])
            print("Item sélectionné :", item_id)

            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            cursor.execute(
                "SELECT id, name, price, type, valeur, duration FROM shop WHERE id = ?",
                (item_id,)
            )

            result = cursor.fetchone()
            conn.close()

            if not result:
                await interaction.response.send_message(
                    "❌ Objet introuvable.",
                    ephemeral=True
                )
                return

            item_id, name, price, type, valeur, duration = result

            if type == 1:
                role = interaction.guild.fetch_role(int(valeur))
                await interaction.response.send_message(f"Objet acheté : {name} ({price})", ephemeral=True)
                if role is None:
                    await interaction.response.send_message("❌ Rôle introuvable.", ephemeral=True)
                    return

                await interaction.user.add_roles(role)

                if duration is not None:
                    expires_at = int(time.time()) + (duration * 86400)

                    conn = sqlite3.connect(DB_PATH)
                    cursor = conn.cursor()
                    cursor.execute(
                        "INSERT INTO roles_temp (user_id, role_id, end_time) VALUES (?, ?, ?)",
                        (interaction.user.id, role.id, expires_at)
                    )
                    conn.commit()
                    conn.close()

            elif type == 2:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()

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
                conn.close()

            elif type == 3:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()

                cursor.execute(
                    "UPDATE utilisateurs SET xp = xp + ? WHERE user_id = ?",
                    (valeur, interaction.user.id)
                )

                conn.commit()
                conn.close()

            await interaction.response.send_message(
                f"✅ Tu as acheté **{name}** pour **{price} €**",
                ephemeral=True
            )

            self.disabled = True
            self.placeholder = "Objet acheté ✔"
            await interaction.message.edit(view=self.view)

    class BoutiqueView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.add_item(BoutiqueCog.AchatSelect())

    @app_commands.command(name="boutique", description="Regarde la boutique")
    async def boutique(self, interaction: discord.Interaction):
        await interaction.response.defer()

        embed = discord.Embed(
            title="🛍 Boutique",
            color=discord.Color.green()
        )

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        try:
            cursor.execute("SELECT name, price, type, duration FROM shop")
            items = cursor.fetchall()
            conn.close()
        except sqlite3.OperationalError as e:
            print("ERREUR DE SQL !!! L'erreur est : ", e)

        if not items:
            embed.description = "❌ Boutique vide"
        else:
            for name, price, type, duration in items:
                if type == 1:
                    type_str = "Rôle"
                    if duration is not None:
                        type_str += " temporaire"
                    else:
                        type_str += " permanent"
                elif type == 2:
                    type_str = "Objet d'inventaire"
                elif type == 3:
                    type_str = "XP"
                else:
                    type_str = "Inconnu"

                desc = f"💰 {price} €\n📦 Type: {type_str}"
                if duration is not None and type == 1:
                    desc += f"\n⏳ Durée: {duration} jours"

                embed.add_field(name=name, value=desc, inline=False)

        await interaction.followup.send(embed=embed, view=BoutiqueCog.BoutiqueView())


async def setup(bot):
    await bot.add_cog(BoutiqueCog(bot))