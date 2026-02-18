import time
from discord.ext import commands
import sqlite3
import random
import os
import discord
from cogs.setupticket import TicketCreateView
from cogs.tickets import FermerView, ModoView, AvisView, PartenariatCommencerView, ConditionsPartenariatView, MentionPartenariatView, SatisfactionView
from dotenv import load_dotenv
load_dotenv()

from utils.setupdatabase import DB_PATH

class Events(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def get_level(self, xp: int) -> int:
        level = 1
        xp_needed = 10

        while xp >= xp_needed:
            xp -= xp_needed
            xp_needed *= 2
            level += 1

        return level

    @commands.Cog.listener()
    async def on_ready(self):
        print("Bot démarré")
        try:
            self.bot.add_view(TicketCreateView(self.bot))
            self.bot.add_view(FermerView())
            self.bot.add_view(ModoView())
            self.bot.add_view(AvisView(self.bot))
            self.bot.add_view(PartenariatCommencerView(self.bot))
            self.bot.add_view(ConditionsPartenariatView(self.bot))
            self.bot.add_view(MentionPartenariatView(self.bot))
            self.bot.add_view(SatisfactionView())
        except Exception as e:
            print(e)


    @commands.Cog.listener()
    async def on_member_remove(self, member):
        try:
            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                DELETE FROM utilisateurs WHERE user_id = ?
                """, (member.id,))
                conn.commit()
        except sqlite3.OperationalError as e:
            guild = member.guild
            channel = guild.get_channel(int(os.getenv("CHANNEL_COMMANDER_ID")))
            await channel.send(f"Erreur de base de donnée quand **{member.id}** as quitté le serveur : {e}")
    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return

        if message.channel.type == discord.ChannelType.private_thread:
            with sqlite3.connect(DB_PATH) as conn:
                cur = conn.cursor()
                cur.execute("""SELECT membre_id FROM ticket WHERE thread_id = ?""", (message.channel.id,))
                rppw = cur.fetchone()
                if message.author.id == rppw[0]:
                    cur.execute("UPDATE ticket SET last_message = ? WHERE thread_id = ?", (time.time(), message.channel.id))
                    conn.commit()
                    cur.execute("SELECT warn_12h FROM ticket WHERE thread_id = ?", (message.channel.id,))
                    row = cur.fetchone()
                    if row[0] is None:
                        return
                    elif row[0] is not None:
                        cur.execute("UPDATE ticket SET warn_12h = NULL WHERE thread_id = ?", (message.channel.id,))
                        conn.commit()




        print("MESSAGE DETECTEE")
        print("DB path :", os.path.abspath(DB_PATH))

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS utilisateurs (
            user_id INTEGER PRIMARY KEY,
            argent INTEGER DEFAULT 0,
            xp INTEGER DEFAULT 0
        )
        """)

        # User
        cursor.execute(
            "INSERT OR IGNORE INTO utilisateurs (user_id) VALUES (?)",
            (message.author.id,)
        )

        # XP actuel
        cursor.execute(
            "SELECT xp FROM utilisateurs WHERE user_id = ?",
            (message.author.id,)
        )
        xp_actuel = cursor.fetchone()[0]

        level_avant = self.get_level(xp_actuel)

        # Gains
        xp_gain = random.randint(1, 10)
        argent_gain = random.randint(5, 15)

        xp_apres = xp_actuel + xp_gain
        level_apres = self.get_level(xp_apres)

        # Level up
        if level_apres > level_avant:
            channel = message.guild.get_channel(int(os.getenv("CHANNEL_COMMANDE_ID")))
            if channel:
                await channel.send(
                    f"🎉 {message.author.mention} est passé **niveau {level_apres}** avec {xp_gain} XP !"
                )

        # Update
        cursor.execute(
            "UPDATE utilisateurs SET xp = ?, argent = argent + ? WHERE user_id = ?",
            (xp_apres, argent_gain, message.author.id)
        )

        conn.commit()
        conn.close()

        print(f"+{xp_gain} XP | +{argent_gain}€ pour {message.author}")

        # IMPORTANT pour les commandes
        await self.bot.process_commands(message)


    @commands.Cog.listener()
    async def on_member_join(self, member):
        from cogs.visite import VisiteGuidee  # éviter import circulaire
        blacklist = [1322202659461271623]
        if member.id in blacklist:
            try:
                await member.send("Tu as été blacklisté du serveur. Kick immediat.")
            except discord.Forbidden:
                pass
            await member.kick()

        embed = discord.Embed(
            title="👋 Bienvenue !",
            description=f"Salut {member.mention} ! Prêt pour la visite du serveur ?",
            color=discord.Color.blurple(),
        )

        view = VisiteGuidee(1)

        try:
            await member.send(embed=embed, view=view)
        except discord.Forbidden:
            pass


async def setup(bot):
    await bot.add_cog(Events(bot))

