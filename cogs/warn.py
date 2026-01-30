import discord
from discord import app_commands
from discord.ext import commands, tasks
import sqlite3
import os
from datetime import datetime, timedelta, timezone
import time
from discord.utils import utcnow

DB_PATH = os.path.expanduser("~/botdata/database.db")
class RaisonrefuserModal(discord.ui.Modal, title="Raison"):
    raison = discord.ui.TextInput(
        label="Raison du refus",
        placeholder="Je trouve que ce warn est mérité car ...",
        min_length=10,
        max_length=1092,
        style=discord.TextStyle.paragraph,
        required=True
    )

    def __init__(self, membre):
        super().__init__()
        self.membre = membre

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
        for b in self.children:
            b.disabled = True
        await interaction.message.edit(view=self)


try:
    class RefuseroracceptercontestationView(discord.ui.View):
        def __init__(self, membre, bot, warn):
            super().__init__(timeout=None)
            self.membre = membre
            self.bot = bot
            self.warn = warn  # tuple (id, raison, created_at...)

        @discord.ui.button(label="Accepter", style=discord.ButtonStyle.green, custom_id="warn:accepter")
        async def accepter(self, interaction: discord.Interaction, button: discord.ui.Button):
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()

            cur.execute(
                "SELECT warn FROM utilisateurs WHERE user_id = ?",
                (self.membre.id,)
            )
            row = cur.fetchone()
            warn_actuel = row[0] if row else 0
            warn_ap = max(warn_actuel - 1, 0)

            cur.execute(
                "UPDATE utilisateurs SET warn = ? WHERE user_id = ?",
                (warn_ap, self.membre.id)
            )

            cur.execute(
                "DELETE FROM warns WHERE id = ?",
                (self.warn[0],)
            )

            conn.commit()
            conn.close()

            embed = discord.Embed(
                title="Contestation acceptée",
                description="Ton warn a été retiré",
                color=discord.Color.green()
            )
            embed.add_field(name="Modérateur :", value=interaction.user.mention, inline=False)

            try:
                await self.membre.send(embed=embed)
            except discord.Forbidden:
                pass

            await interaction.response.send_message("Sanction retirée ✅", ephemeral=True)

            for b in self.children:
                b.disabled = True
            await interaction.message.edit(view=self)

        @discord.ui.button(label="Refuser", style=discord.ButtonStyle.red, custom_id="warn:refuser")
        async def refuser(self, interaction: discord.Interaction, button: discord.ui.Button):
            await interaction.response.send_modal(RaisonrefuserModal(self.membre))
            button.disabled = True
            await interaction.message.edit(view=self)

except Exception as e:
    print(e)





class ContestationModal(discord.ui.Modal, title="Contestation"):
    raison = discord.ui.TextInput(
        style=discord.TextStyle.paragraph,
        placeholder="Je trouve ce warn injuste car ...",
        min_length=100,
        max_length=1092,
        label="Explique pourquoi tu trouve ce warn injuste",
        required=True
    )
    def __init__ (self, bot, membre, warn):
        super().__init__()
        self.bot = bot
        self.membre = membre
        self.warn = warn
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("Merci, tu recevera une réponse dans les prochaines 24h")
        channel = self.bot.get_channel(1405117622063857805)
        embed = discord.Embed(title="Contestation", color=discord.Color.green(), description="Nouvelle contestation !")
        embed.add_field(name="Membre :", value=interaction.user.mention, inline=False)
        embed.add_field(name="Raison : ", value= self.raison.value, inline=False)
        await channel.send(embed=embed, view=RefuseroracceptercontestationView(self.membre, self.bot, self.warn))

class ContestationView(discord.ui.View):
    def __init__(self, membre, bot, warn):
        super().__init__()
        self.membre = membre
        self.bot = bot
        self.warn = warn
        

    @discord.ui.button(label="Contestation", style=discord.ButtonStyle.red, custom_id="contest", emoji="❌")
    async def contest(self, interaction: discord.Interaction, button: discord.ui.Button):
        membre = self.membre
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
        print("🔁 tempban check")
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

        guild = self.bot.get_guild(1405117064909291600)
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
        print("✅ tempban loop ready")

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
            await interaction.response.send_message(embed=embed)
            return

        print("line 24")
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
                print(result)

                if result is None:
                    warn_count = 1
                    c.execute(
                        "INSERT INTO utilisateurs (user_id, warn) VALUES (?, ?)",
                        (membre.id, warn_count)
                    )
                    conn.commit()

                    timestamp = int(time.time())
                    iso_time = datetime.now(timezone.utc).isoformat()

                    c.execute(
                        """
                        INSERT INTO warns (user_id, modo_id, raison, created_at, created_at_iso)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (user.id, modo.id, raison, timestamp, iso_time)
                    )
                    conn.commit()
                else:
                    print("line 52")
                    try:
                        warn_count = result[0] + 1
                        c.execute(
                            "UPDATE utilisateurs SET warn = ? WHERE user_id = ?",
                            (warn_count, membre.id)
                        )
                        print("line 58")

                        timestamp = int(time.time())
                        iso_time = datetime.now(timezone.utc).isoformat()

                        c.execute(
                            """
                            INSERT INTO warns (user_id, modo_id, raison, created_at, created_at_iso)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (user.id, modo.id, raison, timestamp, iso_time)
                        )
                        conn.commit()
                        print("line 59")
                    except Exception as e:
                        print(e)
        except Exception as e:
            print(e)

        try:
            warn_count = result[0] + 1

            if warn_count == 3 or warn_count == 5 or warn_count == 10:
                if warn_count == 3:
                    duration = 172800
                    until = utcnow() + timedelta(hours=48)
                    channel = interaction.guild.get_channel(1405119711624171645)

                    try:
                        await membre.timeout(until, reason="3 avertissements")
                    except discord.Forbidden:
                        await channel.send(
                            f"Erreur lors du mute de {membre.mention} car il n'est pas ici !"
                        )
                    except discord.HTTPException as e:
                        await channel.send(
                            f"Impossible de mute {membre.mention} : {e}"
                        )

                    embed = discord.Embed(
                        title="Tu viens d'être mute",
                        description="Tu as reçu 3 avertissements, tu viens donc d'etre mute 48h sur **Pixel Partie**. Prends le temps de reflechir pendant ton mute, ça evitera le ban 😆"
                    )
                    try:
                        await membre.send(embed=embed)
                    except discord.Forbidden:
                        pass

                elif warn_count == 5:
                    duration = 0.001
                    until = utcnow() + timedelta(days=7)
                    channel = interaction.guild.get_channel(1405119711624171645)

                    try:
                        await membre.timeout(until, reason="5 avertissements")
                    except discord.Forbidden:
                        await channel.send(
                            f"Erreur lors du mute de {membre.mention} car il n'est pas ici !"
                        )
                    except discord.HTTPException as e:
                        await channel.send(
                            f"Impossible de mute {membre.mention} : {e}"
                        )

                    embed = discord.Embed(
                        title="Tu viens d'être mute",
                        description="Tu as reçu 5 avertissements, tu viens donc d'etre mute 7 Jours sur **Pixel Partie**. Prends le temps de reflechir pendant ton mute, ça evitera le ban 😆"
                    )
                    try:
                        await membre.send(embed=embed)
                    except discord.Forbidden:
                        pass

                elif warn_count == 10:
                    duration = 0.001

                    embed3 = discord.Embed(
                        title="Tu viens d'etre ban",
                        description="Tu a reçu 10 avertissement, donc tu viens d'etre banni de Pixel Party."
                    )
                    embed3.add_field(name="Raison : ", value="10 avertissements", inline=False)
                    embed3.add_field(name="Moderateur : ", value=interaction.user.mention, inline=False)

                    try:
                        await membre.send(embed=embed3)
                    except discord.Forbidden:
                        print(f"Impossible de ban {membre.name}")

                    channel = interaction.guild.get_channel(1405119711624171645)
                    unban_at = int(time.time()) + duration * 86400

                    await interaction.guild.ban(membre, reason="10 avertissements")

                    with sqlite3.connect(DB_PATH) as conn:
                        c = conn.cursor()
                        c.execute(
                            "INSERT INTO temp_bans (user_id, unban_at) VALUES (?, ?)",
                            (membre.id, unban_at)
                        )
                        conn.commit()

                    await channel.send(
                        f"🔨 {membre.mention} banni pour **30 jour(s)**.\nRaison : 10 avertissements"
                    )

            await interaction.followup.send(
                "Le membre viens d'etre averti en MP, merci !",
                ephemeral=True
            )
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute(" SELECT id, raison, created_at FROM warns WHERE user_id = ? ORDER BY created_at DESC LIMIT 1", (user.id,))
            warnes = cur.fetchone()
            conn.close()

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
            embed.set_footer(text=f"ID du warn : {warnes[0]}")

            try:
                await membre.send(embed=embed, view=ContestationView(user, self.bot, warnes))
            except discord.Forbidden:
                pass

        except Exception as e:
            print(e)


async def setup(bot):
    await bot.add_cog(Warn(bot))
