import logging
import time
from discord.ext import commands
import aiomysql
import random
import os
import discord
from cogs.setupticket import TicketCreateView
from cogs.tickets import FermerView, ModoView, AvisView, PartenariatCommencerView, ConditionsPartenariatView, MentionPartenariatView, SatisfactionView, ConfirmationClotureView
from cogs.trade import TradeView
from cogs.visite import VisiteGuidee
from cogs.warn import RefuseroracceptercontestationView
from cogs.recrutement import ConditionsSelect, FormulaireBouton
from dotenv import load_dotenv
load_dotenv()

from utils.database import get_pool

logger = logging.getLogger(__name__)

MENTION_RESPONSES = [
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

BLACKLIST = [1322202659461271623]


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
        logger.info("Bot démarré")
        try:
            self.bot.add_view(TicketCreateView())
            self.bot.add_view(FermerView())
            self.bot.add_view(ModoView())
            self.bot.add_view(AvisView())
            self.bot.add_view(PartenariatCommencerView())
            self.bot.add_view(ConditionsPartenariatView())
            self.bot.add_view(MentionPartenariatView())
            self.bot.add_view(SatisfactionView())
            self.bot.add_view(ConfirmationClotureView())
            self.bot.add_view(ConditionsSelect())
            self.bot.add_view(FormulaireBouton())
            self.bot.add_view(TradeView())
            self.bot.add_view(RefuseroracceptercontestationView())
            # Les custom_id des boutons de VisiteGuidee dépendent de l'étape :
            # on enregistre les 3 configurations possibles pour couvrir tous les boutons.
            self.bot.add_view(VisiteGuidee(1))
            self.bot.add_view(VisiteGuidee(2))
            self.bot.add_view(VisiteGuidee(5))
        except Exception as e:
            logger.error(f"[on_ready] Erreur lors de l'enregistrement des vues persistantes : {e}")

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("DELETE FROM utilisateurs WHERE user_id = %s", (member.id,))
                await conn.commit()
        except aiomysql.Error as e:
            channel_id = os.getenv("CHANNEL_COMMANDE_ID")
            if not channel_id:
                logger.critical(f"Erreur de base de donnée quand {member.id} a quitté le serveur : {e}", exc_info=True)
                return
            guild = member.guild
            channel = guild.get_channel(int(channel_id))
            if channel:
                await channel.send(f"Erreur de base de donnée quand **{member.id}** a quitté le serveur : {e}")

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return

        # Réagit quand le bot est mentionné dans un salon du serveur.
        if message.guild is not None and self.bot.user in message.mentions:
            await message.reply(random.choice(MENTION_RESPONSES), mention_author=False)

        pool = get_pool()

        if message.channel.type == discord.ChannelType.private_thread:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT membre_id FROM ticket WHERE thread_id = %s", (message.channel.id,))
                    rppw = await cur.fetchone()
                    if rppw is not None and message.author.id == rppw[0]:
                        await cur.execute(
                            "UPDATE ticket SET last_message = %s WHERE thread_id = %s",
                            (int(time.time()), message.channel.id)
                        )
                        await cur.execute("SELECT warn_12h FROM ticket WHERE thread_id = %s", (message.channel.id,))
                        row = await cur.fetchone()
                        if row is not None and row[0] is not None:
                            await cur.execute(
                                "UPDATE ticket SET warn_12h = NULL WHERE thread_id = %s",
                                (message.channel.id,)
                            )
                await conn.commit()

        # L'économie (argent/XP) ne s'applique qu'aux messages envoyés sur le serveur.
        if message.guild is not None:
            async with pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "INSERT IGNORE INTO utilisateurs (user_id, xp) VALUES (%s, 40)",
                        (message.author.id,)
                    )

                    await cursor.execute("SELECT xp FROM utilisateurs WHERE user_id = %s", (message.author.id,))
                    xp_actuel = (await cursor.fetchone())[0]

                    level_avant = self.get_level(xp_actuel)

                    xp_gain = random.randint(1, 10)
                    argent_gain = random.randint(5, 15)

                    xp_apres = xp_actuel + xp_gain
                    level_apres = self.get_level(xp_apres)

                    if level_apres > level_avant:
                        channel_id = os.getenv("CHANNEL_COMMANDE_ID")
                        channel = message.guild.get_channel(int(channel_id)) if channel_id else None
                        if channel:
                            await channel.send(
                                f"🎉 {message.author.mention} est passé **niveau {level_apres}** avec {xp_gain} XP !"
                            )

                    await cursor.execute(
                        "UPDATE utilisateurs SET xp = %s, argent = argent + %s WHERE user_id = %s",
                        (xp_apres, argent_gain, message.author.id)
                    )

                await conn.commit()

        # IMPORTANT pour les commandes
        await self.bot.process_commands(message)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if member.id in BLACKLIST:
            try:
                await member.send("Tu as été blacklisté du serveur. Kick immediat.")
            except discord.Forbidden:
                pass
            await member.kick(reason="Membre blacklisté")
            return

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
