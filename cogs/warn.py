import discord
from discord import app_commands
from discord.ext import commands, tasks
import sqlite3
import os
from datetime import datetime, timezone
import time
from dotenv import load_dotenv
load_dotenv()
from utils.setupdatabase import DB_PATH
from utils.sanctions import apply_warn_sanction, get_modo_channel


class RaisonrefuserModal(discord.ui.Modal, title="Raison"):
    raison = discord.ui.TextInput(
        label="Raison du refus",
        placeholder="Je trouve que ce warn est mérité car ...",
        min_length=10,
        max_length=1092,
        style=discord.TextStyle.paragraph,
        required=True
    )

    def __init__(self, membre, message_id):
        super().__init__()
        self.membre = membre
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Refus envoyé au membre ❌",
            ephemeral=True
        )

        embed = discord.Embed(
            title="Contestation refusée",
            description="Ta contestation a été refusée",
            color=discord.Color.red()
        )
        embed.add_field(name="Modérateur", value=interaction.user.mention, inline=False)
        embed.add_field(name="Raison", value=self.raison.value, inline=False)

        try:
            await self.membre.send(embed=embed)
        except discord.Forbidden:
            pass

        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM contestations WHERE message_id = ?", (self.message_id,))
            conn.commit()


class RefuseroracceptercontestationView(discord.ui.View):
    """Vue persistante et sans état : les infos de la contestation (membre, warn concerné)
    sont retrouvées dans la table `contestations` à partir de l'id du message cliqué,
    au lieu d'être stockées sur l'instance (ce qui casse dès qu'elle est enregistrée
    globalement via bot.add_view)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Accepter", style=discord.ButtonStyle.green, custom_id="warn:accepter")
    async def accepter(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            with sqlite3.connect(DB_PATH) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT membre_id, warn_id FROM contestations WHERE message_id = ?",
                    (interaction.message.id,)
                )
                row = cur.fetchone()

            if row is None:
                await interaction.response.send_message("❌ Contestation introuvable (déjà traitée ?).", ephemeral=True)
                return

            membre_id, warn_id = row
            guild = interaction.guild
            membre = guild.get_member(membre_id)
            if membre is None:
                try:
                    membre = await guild.fetch_member(membre_id)
                except discord.NotFound:
                    membre = None

            for b in self.children:
                b.disabled = True
            await interaction.response.edit_message(view=self)

            with sqlite3.connect(DB_PATH) as conn:
                cur = conn.cursor()

                cur.execute("SELECT warn FROM utilisateurs WHERE user_id = ?", (membre_id,))
                wrow = cur.fetchone()
                warn_actuel = wrow[0] if wrow and wrow[0] is not None else 0
                warn_ap = max(warn_actuel - 1, 0)

                cur.execute("UPDATE utilisateurs SET warn = ? WHERE user_id = ?", (warn_ap, membre_id))

                if warn_id is not None:
                    cur.execute("DELETE FROM warns WHERE id = ?", (warn_id,))

                cur.execute("DELETE FROM contestations WHERE message_id = ?", (interaction.message.id,))

                conn.commit()

            if membre is not None:
                embed = discord.Embed(
                    title="Contestation acceptée",
                    description="Ton warn a été retiré",
                    color=discord.Color.green()
                )
                embed.add_field(name="Modérateur :", value=interaction.user.mention, inline=False)

                try:
                    await membre.send(embed=embed)
                except discord.Forbidden:
                    pass

            await interaction.followup.send("Sanction retirée ✅", ephemeral=True)
        except Exception as e:
            print(f"[warn:accepter] {e}")

    @discord.ui.button(label="Refuser", style=discord.ButtonStyle.red, custom_id="warn:refuser")
    async def refuser(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            with sqlite3.connect(DB_PATH) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT membre_id FROM contestations WHERE message_id = ?",
                    (interaction.message.id,)
                )
                row = cur.fetchone()

            if row is None:
                await interaction.response.send_message("❌ Contestation introuvable (déjà traitée ?).", ephemeral=True)
                return

            membre_id = row[0]
            guild = interaction.guild
            membre = guild.get_member(membre_id)
            if membre is None:
                try:
                    membre = await guild.fetch_member(membre_id)
                except discord.NotFound:
                    membre = None

            if membre is None:
                await interaction.response.send_message("❌ Ce membre n'est plus sur le serveur.", ephemeral=True)
                return

            await interaction.response.send_modal(RaisonrefuserModal(membre, interaction.message.id))
            for child in self.children:
                child.disabled = True
            await interaction.message.edit(view=self)
        except Exception as e:
            print(f"[warn:refuser] {e}")


class ContestationModal(discord.ui.Modal, title="Contestation"):
    raison = discord.ui.TextInput(
        style=discord.TextStyle.paragraph,
        placeholder="Je trouve ce warn injuste car ...",
        min_length=100,
        max_length=1092,
        label="Explique pourquoi tu trouve ce warn injuste",
        required=True
    )

    def __init__(self, bot, membre, warn):
        super().__init__()
        self.bot = bot
        self.membre = membre
        self.warn = warn  # tuple (id, raison, created_at) ou None

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("Merci, tu recevera une réponse dans les prochaines 24h", ephemeral=True)

        channel = await get_modo_channel(self.bot)
        if channel is None:
            return

        embed = discord.Embed(title="Contestation", color=discord.Color.green(), description="Nouvelle contestation !")
        embed.add_field(name="Membre :", value=interaction.user.mention, inline=False)
        embed.add_field(name="Raison : ", value=self.raison.value, inline=False)

        msg = await channel.send(embed=embed, view=RefuseroracceptercontestationView())

        warn_id = self.warn[0] if self.warn else None
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO contestations (message_id, membre_id, warn_id) VALUES (?, ?, ?)",
                (msg.id, self.membre.id, warn_id)
            )
            conn.commit()


class ContestationView(discord.ui.View):
    def __init__(self, membre, bot, warn):
        super().__init__(timeout=None)
        self.membre = membre
        self.bot = bot
        self.warn = warn

    @discord.ui.button(label="Contestation", style=discord.ButtonStyle.red, custom_id="contest", emoji="❌")
    async def contest(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ContestationModal(self.bot, self.membre, self.warn))
        button.disabled = True
        await interaction.message.edit(view=self)


class Warn(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_tempbans.start()

    def cog_unload(self):
        self.check_tempbans.cancel()

    @tasks.loop(seconds=30)
    async def check_tempbans(self):
        now = int(time.time())

        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute(
                "SELECT user_id FROM temp_bans WHERE unban_at <= ?",
                (now,)
            )
            bans = c.fetchall()

        if not bans:
            return

        guild = self.bot.get_guild(int(os.getenv("GUILD_ID")))
        if not guild:
            return

        for (user_id,) in bans:
            try:
                user = await self.bot.fetch_user(user_id)
                await guild.unban(user, reason="Fin du ban temporaire")
            except (discord.NotFound, discord.Forbidden):
                pass

            with sqlite3.connect(DB_PATH) as conn:
                c = conn.cursor()
                c.execute(
                    "DELETE FROM temp_bans WHERE user_id = ?",
                    (user_id,)
                )
                conn.commit()

    @check_tempbans.before_loop
    async def before_tempbans(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="warn", description="Averti un membre")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def warn(self, interaction: discord.Interaction, user: discord.Member, raison: str):
        await interaction.response.defer(ephemeral=True)

        if interaction.guild is None:
            embed = discord.Embed(
                title="Les messages privées...",
                description="Cette commande est indisponible en MP en raison d'optimisation de mon code... Mais tu peut aller dans <@> pour cela !",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return

        modo = interaction.user
        membre = user

        try:
            with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
                c = conn.cursor()
                c.execute(
                    "SELECT warn FROM utilisateurs WHERE user_id = ?",
                    (membre.id,)
                )
                result = c.fetchone()

                if result is None:
                    warn_count = 1
                    c.execute(
                        "INSERT INTO utilisateurs (user_id, warn) VALUES (?, ?)",
                        (membre.id, warn_count)
                    )
                elif result[0] is None:
                    warn_count = 1
                    c.execute("UPDATE utilisateurs SET warn = 1 WHERE user_id = ?", (membre.id,))
                else:
                    warn_count = result[0] + 1
                    c.execute(
                        "UPDATE utilisateurs SET warn = ? WHERE user_id = ?",
                        (warn_count, membre.id)
                    )

                timestamp = int(time.time())
                iso_time = datetime.now(timezone.utc).isoformat()

                c.execute(
                    """
                    INSERT INTO warns (user_id, modo_id, raison, created_at, created_at_iso)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (membre.id, modo.id, raison, timestamp, iso_time)
                )
                warn_id = c.lastrowid

                conn.commit()
        except sqlite3.OperationalError as e:
            print(e)
            await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
            return

        channel = await get_modo_channel(self.bot, interaction.guild)
        await apply_warn_sanction(interaction.guild, membre, channel, warn_count)

        await interaction.followup.send(
            "Le membre viens d'etre averti en MP, merci !",
            ephemeral=True
        )

        embed = discord.Embed(
            title="Tu viens d'etre avertit",
            description="Tu t'est mal comporté sur Pixel Party donc un avertissement vient de tomber",
            color=discord.Color.red()
        )
        embed.add_field(name="Moderateur : ", value=modo.mention, inline=False)
        embed.add_field(name="Raison : ", value=raison, inline=False)
        embed.add_field(
            name="C'est une erreur ?",
            value="Clique sur le boutton ci-dessous pour contester ta sanction"
        )
        embed.set_footer(text=f"ID du warn : {warn_id}")

        try:
            await membre.send(embed=embed, view=ContestationView(membre, self.bot, (warn_id, raison, timestamp)))
        except discord.Forbidden:
            pass


async def setup(bot):
    await bot.add_cog(Warn(bot))
