import logging
import re
import time
from discord.ext import commands
import aiomysql
import random
import discord
from cogs.tickets import TicketCreateView, FermerView, ModoView, AvisView, PartenariatCommencerView, ConditionsPartenariatView, MentionPartenariatView, SatisfactionView, ConfirmationClotureView
from cogs.trade import TradeView, TradePanelView
from cogs.warn import RefuseroracceptercontestationView, ContestationView
from cogs.recrutement import ConditionsSelect, FormulaireBouton, Accepterview, DecisionPeriodeTest
from dotenv import load_dotenv
load_dotenv()

from utils.database import connexion
from utils import cache
from utils.config import get_config
from utils.autorisations import serveur_autorise

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
            self.bot.add_view(TradePanelView())
            self.bot.add_view(RefuseroracceptercontestationView())
            self.bot.add_view(ContestationView())
            self.bot.add_view(Accepterview())
            # Boutons de fin de période de test (voir staff_test_watcher dans
            # start.py) : membre et rôle portés par le custom_id.
            self.bot.add_dynamic_items(DecisionPeriodeTest)
        except Exception as e:
            logger.error(f"[on_ready] Erreur lors de l'enregistrement des vues persistantes : {e}")

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        # Uniquement pour le serveur configuré : quitter un autre serveur où se
        # trouve aussi le bot ne doit pas effacer l'argent et l'XP du membre ici.
        if not serveur_autorise(member.guild.id):
            return
        try:
            async with connexion() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("DELETE FROM utilisateurs WHERE user_id = %s", (member.id,))
                await conn.commit()
            cache.invalidate_xp(member.id)
        except aiomysql.Error as e:
            # Journalisé (alerte MP à l'owner, voir utils/error_handler.py) plutôt
            # que posté dans le salon des commandes, visible de tous les membres.
            logger.critical(f"Erreur de base de donnée quand {member.id} a quitté le serveur : {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return

        # Ni XP, ni réponse, ni suivi de ticket hors du serveur configuré (MP
        # compris, où rien de tout ça ne s'applique).
        if message.guild is None or not serveur_autorise(message.guild.id):
            return

        # Réagit quand le bot est mentionné dans le texte du message. Pas via
        # message.mentions : il contient aussi l'auteur du message auquel on
        # répond, et le bot répondait alors à chaque réponse faite à l'un de ses
        # messages (annonce de niveau, ticket...).
        if re.search(rf"<@!?{self.bot.user.id}>", message.content):
            try:
                await message.reply(random.choice(MENTION_RESPONSES), mention_author=False)
            except discord.HTTPException:
                pass

        if message.channel.type == discord.ChannelType.private_thread:
            # Activité du membre dans son ticket : repousse la fermeture pour
            # inactivité (voir ticket_watcher dans start.py). Un seul UPDATE
            # conditionné à l'auteur du ticket (un modérateur qui écrit ne compte
            # pas comme une réponse du membre).
            try:
                async with connexion() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "UPDATE ticket SET last_message = %s, warn_12h = NULL "
                            "WHERE thread_id = %s AND membre_id = %s AND statut != 3",
                            (int(time.time()), message.channel.id, message.author.id)
                        )
                    await conn.commit()
            except aiomysql.Error as e:
                logger.error(f"[on_message] Erreur DB au suivi du ticket {message.channel.id} : {e}")

        # L'économie (argent/XP) ne s'applique qu'aux messages envoyés sur le serveur.
        # Lecture de l'XP actuelle via le cache mémoire (utils/cache.py) au lieu d'un
        # SELECT à chaque message : la valeur ne change qu'à un message de ce membre
        # ou à un achat en boutique, pas besoin de la relire en base à chaque fois.
        if message.guild is not None:
            try:
                xp_actuel = await cache.get_xp(message.author.id)
            except aiomysql.Error as e:
                logger.error(f"[on_message] Erreur DB en lisant l'XP de {message.author.id} : {e}")
                return
            level_avant = self.get_level(xp_actuel)

            xp_gain = random.randint(1, 10)
            argent_gain = random.randint(5, 15)

            # Cache mis à jour tout de suite (synchrone, avant le premier await
            # ci-dessous) : si un autre message du même membre est traité entre
            # temps, il verra déjà cette valeur au lieu d'une valeur périmée.
            xp_apres = cache.bump_xp(message.author.id, xp_gain)
            level_apres = self.get_level(xp_apres)

            # Écriture DB juste après le bump du cache, avant tout autre await
            # (notamment l'annonce de niveau ci-dessous) : sinon une erreur ou une
            # latence sur cet envoi Discord retarderait d'autant la persistance du
            # gain, et surtout empêcherait process_commands de s'exécuter pour ce
            # message si l'envoi lève une exception non interceptée.
            # Incrément relatif (et non une valeur absolue) : reste correct même si
            # deux messages du même membre finissent par s'exécuter en parallèle.
            try:
                async with connexion() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute(
                            "UPDATE utilisateurs SET xp = xp + %s, argent = argent + %s WHERE user_id = %s",
                            (xp_gain, argent_gain, message.author.id)
                        )
                    await conn.commit()
            except aiomysql.Error as e:
                # Le cache a déjà pris en compte ce gain (bump_xp ci-dessus) mais
                # l'écriture en base a échoué : on invalide l'entrée pour forcer une
                # relecture depuis la DB au prochain message, plutôt que de laisser
                # le cache indéfiniment en avance sur une valeur jamais persistée.
                logger.error(f"[on_message] Erreur DB lors du gain d'XP/argent de {message.author.id} : {e}")
                cache.invalidate_xp(message.author.id)

            if level_apres > level_avant:
                channel_id = get_config("CHANNEL_COMMANDE_ID")
                channel = message.guild.get_channel(int(channel_id)) if channel_id else None
                if channel:
                    try:
                        await channel.send(
                            f"🎉 {message.author.mention} vient d'atteindre le **niveau {level_apres}** !"
                        )
                    except discord.HTTPException as e:
                        logger.warning(f"[on_message] Impossible d'annoncer le niveau de {message.author.id} : {e}")

        # IMPORTANT pour les commandes
        await self.bot.process_commands(message)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        # L'accueil (questions/rôles) passe désormais par l'onboarding natif Discord
        # (Server Settings > Onboarding) — plus d'envoi de MP ici, qui échouait
        # silencieusement pour les membres ayant fermé leurs messages privés.
        if member.id in BLACKLIST and serveur_autorise(member.guild.id):
            try:
                await member.send("Tu as été blacklisté du serveur. Kick immédiat.")
            except discord.HTTPException:
                pass
            try:
                await member.kick(reason="Membre blacklisté")
            except discord.HTTPException as e:
                logger.error(f"[on_member_join] Impossible d'expulser le membre blacklisté {member.id} : {e}")


async def setup(bot):
    await bot.add_cog(Events(bot))
