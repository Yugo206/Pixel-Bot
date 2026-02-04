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
        print("Line 9")

    class AchatSelect(discord.ui.Select):
        def __init__(self):
            print("line 13")
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            print("line 17")
            try:
                items = []
                cursor.execute("SELECT valeur, name, price, type FROM shop")
                items = cursor.fetchall()
            except sqlite3.OperationalError as e:
                print(e)
            print("line 20")
            conn.close()

            if not items:
                print("Line 24")
                options = [
                    discord.SelectOption(
                        label="Boutique vide",
                        description="Aucun objet disponible",
                        value="0"
                    )
                ]
            else:
                print("Line 33")
                try:
                    options = [
                        discord.SelectOption(
                            label=name,
                            description=f"{price} €",
                            value=str(value)
                        )
                        for value, name, price, type in items
                    ]
                except Exception as e:
                    print(e)


            super().__init__(
                placeholder="🛒 Choisis un objet",
                min_values=1,
                max_values=1,
                options=options
            )
            print("Line 49")

        async def callback(self, interaction: discord.Interaction):
            item_id = int(self.values[0])
            print("Item sélectionné :", item_id)

            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            cursor.execute(
                "SELECT valeur, name, price, type, duration FROM shop WHERE valeur = ?",
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

            valeur, name, price, type, duration = result

            if type == 1:
                role = interaction.guild.get_role(valeur)
                if role is None:
                    await interaction.response.send_message(
                        "❌ Rôle introuvable.",
                        ephemeral=True
                    )
                    return
                if time is not None:
                    try:
                        expires_at = int(time.time()) + duration

                        await interaction.user.add_roles(role)

                        conn = sqlite3.connect(DB_PATH)
                        cursor = conn.cursor()
                        cursor.execute(
                            "INSERT INTO roles_temp (user_id, role_id, expires_at) VALUES (?, ?, ?)",
                            (interaction.user.id, role.id, expires_at)
                        )
                        conn.commit()
                        conn.close()
                    except (discord.Forbidden, sqlite3.OperationalError) as e:
                        interaction.response.send_message(f"Erreur ! : {e}")

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
            print("Line 74")

    @app_commands.command(name="boutique", description="Regarde la boutique")
    async def boutique(self, interaction: discord.Interaction):
        print("Line 78")
        await interaction.response.defer()
        print("COMMANDE /boutique")

        embed = discord.Embed(
            title="🛍 Boutique",
            color=discord.Color.green()
        )

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        try:
            print("Line 81")
            cursor.execute("SELECT name, price FROM shop")
            items = cursor.fetchall()
            conn.close()
        except sqlite3.OperationalError as e:
            print("ERREUR DE SQL !!! L'erreur est : ", e)
        print("Line 94")

        if not items:
            embed.description = "❌ Boutique vide"
            print("Line 98")
        else:
            print("Line 100")
            for name, price in items:
                embed.add_field(name=name, value=f"{price} €", inline=False)

        await interaction.followup.send(embed=embed, view=BoutiqueCog.BoutiqueView())
        print("Line 105")


async def setup(bot):
    await bot.add_cog(BoutiqueCog(bot))

