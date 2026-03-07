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
from cogs.recrutement import ConditionsSelect, RoleSelectView

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
            self.bot.add_view(TicketCreateView())
            self.bot.add_view(FermerView())
            self.bot.add_view(ModoView())
            self.bot.add_view(AvisView())
            self.bot.add_view(PartenariatCommencerView())
            self.bot.add_view(ConditionsPartenariatView())
            self.bot.add_view(MentionPartenariatView())
            self.bot.add_view(SatisfactionView())
            self.bot.add_view(ConditionsPartenariatView())
            # self.bot.add_view(RoleSelectView())
        except Exception as e:
            print(e)

    @commands.Cog.listener()
    async def on_member_mention(self, member):
        responses = [
            "Salut, moi c'est Pixel Bot!",
            "Quelqu’un m’a mentionné ici ?",
            "Pixel Bot, toujours prêt à vous répondre !",
            "Hello ! Je suis Pixel Bot.",
            "Salut ! Comment puis-je vous aider ?",
            "Je suis Pixel Bot, votre bot Discord.",
            "Mentionnez-moi quand vous voulez !",
            "Pixel Bot est en ligne et prêt à répondre.",
            "Salut ! Je suis Pixel Bot, votre assistant Discord.",
            "Je suis là pour vous aider. Mentionnez-moi !",
            "Pixel Bot a détecté une mention ! Comment puis-je vous aider ?",
            "Salut, je suis Pixel Bot. Que puis-je faire pour vous?",
            "Je suis Pixel Bot, votre bot Discord personnel.",
            "Mentionnez-moi et je réponds !",
            "Pixel Bot est actif. Dites-moi ce que vous voulez.",
            "Salut ! Je suis Pixel Bot, prêt à répondre à vos questions.",
            "Je suis Pixel Bot. Comment puis-je vous aider aujourd'hui ?",
            "Pixel Bot a été mentionné ! Je suis prêt à répondre.",
            "Salut, je suis Pixel Bot. Dites-moi ce dont vous avez besoin !",
            "Pixel Bot est en ligne et prêt à vous aider. Mentionnez-moi !"
        ]
        response = random.choice(responses)
        await member.send(response)
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

