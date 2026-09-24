import discord
from discord.ext import commands
from dotenv import load_dotenv
import logging
import aiomysql
load_dotenv()
from utils.database import connexion
from utils.sanctions import get_modo_channel
from utils.config import get_config
from utils.autorisations import (
    MSG_DEJA_TRAITE, MSG_ERREUR, est_staff, membre_du_serveur, refuser, repondre,
    verifier_mp, verifier_serveur, verifier_staff,
)
from utils.views import Modale, VuePersistante, copie_vue, liberer_clic, reserver_clic
import time

logger = logging.getLogger(__name__)

DUREE_PERIODE_TEST = 7 * 24 * 3600

# Réponses du formulaire : chacune finit dans un champ d'embed (1024 caractères
# au plus côté Discord). Sans limite, la saisie par défaut d'un champ paragraphe
# monte à 4000 caractères et l'envoi de la candidature au staff échouait.
_MAX_REPONSE = 1000


async def _refus_candidature(membre: discord.Member) -> str | None:
    """Message expliquant pourquoi `membre` ne peut pas postuler (déjà staff,
    candidature en cours, moins d'un mois sur le serveur, plus de 3
    avertissements), ou None s'il le peut. Vérifié au clic sur « Commencer »,
    puis de nouveau dans le MP (bouton et envoi du formulaire) : ce bouton
    reste utilisable longtemps, et la situation du membre a pu changer depuis
    (warns reçus, entrée dans le staff). Peut lever aiomysql.Error."""
    if est_staff(membre):
        return "❌ Tu fais déjà partie du staff."

    async with connexion() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM role_special WHERE user_id = %s", (membre.id,))
            en_cours = await cur.fetchone() is not None
            await cur.execute("SELECT COUNT(*) FROM warns WHERE user_id = %s", (membre.id,))
            warn_count = (await cur.fetchone())[0]
    if en_cours:
        return "❌ Tu as déjà une candidature en cours !"

    joined_at = getattr(membre, "joined_at", None)
    if joined_at is None:
        return "❌ Impossible de vérifier ton ancienneté."
    if (discord.utils.utcnow() - joined_at).days < 30:
        return "❌ Tu dois être sur le serveur depuis au moins un mois."

    if warn_count > 3:
        return "❌ Tu as trop d'avertissements pour postuler."
    return None


async def _candidature_du_message(message_id: int) -> int | None:
    """Candidat (user_id) de la candidature affichée par ce message du salon
    modération, ou None si elle a déjà été traitée."""
    async with connexion() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT user_id FROM role_special WHERE message_accepter_id = %s", (message_id,))
            row = await cursor.fetchone()
    return row[0] if row else None


async def _marquer_candidature(message: discord.Message, statut: str | None = None,
                               couleur: discord.Color | None = None) -> None:
    """Réédite le message de candidature (salon modération) une fois traitée :
    boutons désactivés et décision affichée dans l'embed, pour qu'aucun autre
    modérateur ne tente de la traiter à nouveau. Sans `statut`, désactive
    seulement les boutons (candidature traitée par quelqu'un d'autre, qui
    affiche lui-même sa décision)."""
    kwargs = {"view": copie_vue(Accepterview, message)}
    if statut and message.embeds:
        embed = message.embeds[0].copy()
        embed.color = couleur
        embed.add_field(name="Statut", value=statut, inline=False)
        kwargs["embed"] = embed
    try:
        await message.edit(**kwargs)
    except discord.HTTPException as e:
        logger.warning(f"[recrutement] Impossible de mettre à jour la candidature {message.id} : {e}")


class RaisonModal(Modale, title="Raison du refus"):
    raison = discord.ui.TextInput(
        style=discord.TextStyle.paragraph,
        placeholder="Cette candidature n'est pas retenue car ...",
        required=True,
        label="Pourquoi refuser cette candidature ?",
        max_length=500,
        min_length=10
    )

    def __init__(self, message: discord.Message, membre: discord.Member):
        super().__init__()
        self.message = message
        self.membre = membre

    async def on_submit(self, interaction: discord.Interaction):
        membre = self.membre
        await interaction.response.defer()

        # Réservation atomique de la décision : DELETE conditionné au message +
        # rowcount. Deux modérateurs qui refusent (ou l'un accepte pendant que
        # l'autre remplit ce formulaire) ne peuvent pas traiter deux fois la même
        # candidature — le second est prévenu et rien n'est envoyé au membre.
        try:
            async with connexion() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "DELETE FROM role_special WHERE message_accepter_id = %s AND user_id = %s",
                        (self.message.id, membre.id)
                    )
                    gagne = cursor.rowcount == 1
                await conn.commit()
        except aiomysql.Error as e:
            logger.error(f"[recrutement] Erreur DB au refus de la candidature de {membre.id} : {e}")
            await repondre(interaction, "❌ Erreur de base de données, la candidature n'a pas été refusée. Réessaie.")
            return

        if not gagne:
            await repondre(interaction, "⚠️ Cette candidature a déjà été traitée par un autre modérateur.")
            await _marquer_candidature(self.message)
            return

        embed = discord.Embed(title="Candidature refusée",
                              description="Ta candidature pour devenir modérateur sur Pixel Party n'a malheureusement pas été retenue. N'hésite pas à retenter ta chance plus tard !",
                              color=discord.Color.red())
        icon = interaction.guild.icon.url if interaction.guild and interaction.guild.icon else None
        embed.set_footer(text="Pixel Party - Système de recrutement", icon_url=icon)
        embed.add_field(name="Raison du refus :", value=self.raison.value, inline=False)

        mp_envoye = True
        try:
            await membre.send(embed=embed)
        except discord.HTTPException:
            mp_envoye = False

        note = "" if mp_envoye else "\n⚠️ Ses messages privés sont fermés : il n'a pas été prévenu."
        await interaction.followup.send(
            f"La candidature de {membre.mention} a été refusée par {interaction.user.mention}.\n"
            f"Raison : {self.raison.value}{note}",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await _marquer_candidature(self.message, f"❌ Refusée par {interaction.user.mention}", discord.Color.red())


class Accepterview(VuePersistante):
    """Boutons Accepter/Refuser d'une candidature, dans le salon modération.
    Réservés au staff, et jamais à la personne qui a postulé (un membre devenu
    modérateur entre-temps ne peut pas valider sa propre candidature)."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await verifier_staff(interaction, "❌ Seul le staff peut traiter une candidature.")

    @discord.ui.button(label="Accepter", style=discord.ButtonStyle.green, emoji="✅", custom_id="recrutement:accepter")
    async def accepter(self, interaction: discord.Interaction, button: discord.ui.Button):
        message = interaction.message

        # Le rôle de test est vérifié AVANT de consommer la candidature : une
        # configuration manquante ne doit pas la faire disparaître sans que le
        # candidat ait reçu son rôle.
        raw_role_id = get_config("ROLE_RECRUTEMENT")
        role = interaction.guild.get_role(int(raw_role_id)) if raw_role_id else None
        if role is None:
            logger.error("[recrutement] ROLE_RECRUTEMENT absent de `config` ou rôle supprimé : candidature non traitée.")
            await repondre(
                interaction,
                "❌ Le rôle de modérateur test (ROLE_RECRUTEMENT) est introuvable : la candidature n'a pas été traitée. "
                "Préviens un administrateur."
            )
            return

        try:
            user_id = await _candidature_du_message(message.id)
        except aiomysql.Error as e:
            logger.error(f"[recrutement] Erreur DB en lisant la candidature du message {message.id} : {e}")
            await repondre(interaction, "❌ Erreur de base de données, réessaie dans un instant.")
            return

        if user_id is None:
            await repondre(interaction, "⚠️ Cette candidature a déjà été traitée.")
            await _marquer_candidature(message)
            return
        if user_id == interaction.user.id:
            await repondre(interaction, "❌ Tu ne peux pas traiter ta propre candidature.")
            return

        await interaction.response.defer()

        membre = interaction.guild.get_member(user_id)
        if membre is None:
            try:
                membre = await interaction.guild.fetch_member(user_id)
            except discord.NotFound:
                membre = None

        # Réservation + attribution du rôle dans une seule transaction : la
        # ligne n'est supprimée (et la période de test enregistrée) que si le
        # rôle a bien été ajouté. Un second clic simultané attend la fin de
        # cette transaction (verrou de ligne), puis ne supprime plus rien.
        try:
            async with connexion() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "DELETE FROM role_special WHERE message_accepter_id = %s AND user_id = %s",
                        (message.id, user_id)
                    )
                    if cursor.rowcount != 1:
                        await conn.rollback()
                        await repondre(interaction, "⚠️ Cette candidature vient d'être traitée par un autre modérateur.")
                        return

                    if membre is None:
                        # Nettoyage quand même : sinon sa candidature reste bloquée
                        # "en cours" à vie (voir ConditionsSelect.commencer).
                        await conn.commit()
                        await interaction.followup.send(
                            "❌ Ce membre n'est plus sur le serveur, sa candidature a été retirée.", ephemeral=True
                        )
                        await _marquer_candidature(message, "Candidat parti du serveur", discord.Color.dark_grey())
                        return

                    await cursor.execute(
                        "INSERT INTO temp_roles (user_id, role_id, end_time, origin) VALUES (%s, %s, %s, 'staff_test')",
                        (user_id, role.id, int(time.time()) + DUREE_PERIODE_TEST)
                    )
                    try:
                        await membre.add_roles(role, reason=f"Candidature acceptée par {interaction.user}")
                    except discord.HTTPException as e:
                        await conn.rollback()
                        logger.warning(f"[recrutement] Impossible d'ajouter le rôle de test à {user_id} : {e}")
                        await interaction.followup.send(
                            f"❌ Impossible d'ajouter le rôle {role.mention} à {membre.mention} : vérifie que le rôle "
                            "du bot est placé au-dessus. La candidature reste en attente.",
                            ephemeral=True
                        )
                        return
                await conn.commit()
        except aiomysql.Error as e:
            logger.error(f"[recrutement] Erreur DB à l'acceptation de la candidature de {user_id} : {e}")
            await interaction.followup.send("❌ Erreur de base de données, la candidature reste en attente.", ephemeral=True)
            return

        embed = discord.Embed(title="Candidature acceptée",
                              description=f"Félicitations {membre.mention} ! Ta candidature pour devenir modérateur sur Pixel Party est acceptée !", color=discord.Color.green())
        embed.add_field(name="Les étapes suivantes :", value="Tu vas passer modérateur test : tu auras accès à des salons privés et tu traverseras une période d'essai pour montrer tes compétences et ton implication. À l'issue de cette période, selon ta performance, tu rejoindras officiellement le staff ou tu redeviendras simple membre.", inline=False)
        embed.add_field(name="Durée :", value="La période de test dure une semaine.", inline=False)
        icon = interaction.guild.icon.url if interaction.guild.icon else None
        embed.set_footer(text="Pixel Party - Système de recrutement", icon_url=icon)

        mp_envoye = True
        try:
            await membre.send(embed=embed)
        except discord.HTTPException:
            mp_envoye = False

        note = "" if mp_envoye else "\n⚠️ Ses messages privés sont fermés : il n'a pas été prévenu."
        await interaction.followup.send(
            f"La candidature de {membre.mention} a été acceptée par {interaction.user.mention} ✅{note}",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await _marquer_candidature(message, f"✅ Acceptée par {interaction.user.mention}", discord.Color.green())

    @discord.ui.button(label="Refuser", style=discord.ButtonStyle.red, emoji="❌", custom_id="recrutement:refuser")
    async def refuser(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            user_id = await _candidature_du_message(interaction.message.id)
        except aiomysql.Error as e:
            logger.error(f"[recrutement] Erreur DB en lisant la candidature du message {interaction.message.id} : {e}")
            await repondre(interaction, "❌ Erreur de base de données, réessaie dans un instant.")
            return

        if user_id is None:
            await repondre(interaction, "⚠️ Cette candidature a déjà été traitée.")
            await _marquer_candidature(interaction.message)
            return
        if user_id == interaction.user.id:
            await repondre(interaction, "❌ Tu ne peux pas traiter ta propre candidature.")
            return

        membre = interaction.guild.get_member(user_id)
        if membre is None:
            try:
                membre = await interaction.guild.fetch_member(user_id)
            except discord.NotFound:
                # Même nettoyage que accepter() dans ce cas : sinon, avec la
                # contrainte UNIQUE(user_id) sur role_special, ce membre ne pourrait
                # plus jamais repostuler même en revenant sur le serveur.
                try:
                    async with connexion() as conn:
                        async with conn.cursor() as cursor:
                            await cursor.execute(
                                "DELETE FROM role_special WHERE message_accepter_id = %s", (interaction.message.id,)
                            )
                        await conn.commit()
                except aiomysql.Error as e:
                    logger.error(f"[recrutement] Erreur DB au retrait de la candidature de {user_id} : {e}")
                    await repondre(interaction, "❌ Erreur de base de données, réessaie dans un instant.")
                    return
                await repondre(interaction, "❌ Ce membre n'est plus sur le serveur, sa candidature a été retirée.")
                await _marquer_candidature(interaction.message, "Candidat parti du serveur", discord.Color.dark_grey())
                return

        await interaction.response.send_modal(RaisonModal(interaction.message, membre))


class RecrutementModal(Modale, title="Formulaire de recrutement"):
    question1 = discord.ui.TextInput(label="Pourquoi devenir modérateur ?", placeholder="Pourquoi veux-tu devenir modérateur sur Pixel Party ?", style=discord.TextStyle.paragraph, required=True, min_length=50, max_length=_MAX_REPONSE)
    question2 = discord.ui.TextInput(label="Réaction face à une insulte ?", placeholder="Un membre insulte un autre membre. Que fais-tu ?", style=discord.TextStyle.paragraph, required=True, min_length=50, max_length=_MAX_REPONSE)
    question3 = discord.ui.TextInput(label="Si un ami enfreint une règle ?", placeholder="Si un de tes amis enfreint une règle, que fais-tu ?", style=discord.TextStyle.paragraph, required=True, min_length=50, max_length=_MAX_REPONSE)
    question4 = discord.ui.TextInput(label="Temps disponible par semaine ?", placeholder="Combien de temps peux-tu consacrer au serveur par semaine ?", style=discord.TextStyle.paragraph, required=True, max_length=300)
    question5 = discord.ui.TextInput(label="Qu'est-ce qu'un mauvais modérateur ?", placeholder="Selon toi, qu'est-ce qu'un mauvais modérateur ?", style=discord.TextStyle.paragraph, required=True, min_length=50, max_length=_MAX_REPONSE)

    def _ajouter_reponses(self, embed: discord.Embed) -> discord.Embed:
        embed.add_field(name="Pourquoi devenir modérateur ?", value=self.question1.value, inline=False)
        embed.add_field(name="Réaction face à une insulte ?", value=self.question2.value, inline=False)
        embed.add_field(name="Si un ami enfreint une règle ?", value=self.question3.value, inline=False)
        embed.add_field(name="Temps disponible par semaine ?", value=self.question4.value, inline=False)
        embed.add_field(name="Mauvais modérateur ?", value=self.question5.value, inline=False)
        return embed

    async def on_submit(self, interaction: discord.Interaction):
        # Formulaire envoyé depuis un message privé : l'interaction ne porte aucun
        # serveur, on revérifie donc ici que le candidat en est toujours membre
        # et peut toujours postuler au moment de l'envoi.
        await interaction.response.defer(ephemeral=True, thinking=True)

        membre = await membre_du_serveur(interaction.client, interaction.user.id)
        if membre is None:
            await interaction.followup.send("❌ Tu dois être membre du serveur Pixel Party pour postuler.", ephemeral=True)
            return
        try:
            erreur = await _refus_candidature(membre)
        except aiomysql.Error as e:
            logger.error(f"[recrutement] Erreur DB aux vérifications de {interaction.user.id} : {e}")
            await interaction.followup.send(MSG_ERREUR, ephemeral=True)
            return
        if erreur:
            await interaction.followup.send(erreur, ephemeral=True)
            return

        channel = await get_modo_channel(interaction.client)
        if channel is None:
            logger.error("[recrutement] Salon de modération (CHANNEL_MODO_ID) introuvable : candidature impossible.")
            await interaction.followup.send(
                "❌ Le recrutement est momentanément indisponible. Réessaie plus tard, ou préviens un modérateur.",
                ephemeral=True
            )
            return

        # Réservation d'abord (contrainte UNIQUE(user_id) sur role_special, voir
        # utils/setupdatabase.py), envoi au staff ensuite : un double envoi du
        # formulaire (deux MP « Commencer ») ne crée jamais deux candidatures,
        # et aucun message orphelin n'apparaît dans le salon modération.
        try:
            async with connexion() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "INSERT INTO role_special (user_id, status) VALUES (%s, %s)",
                        (interaction.user.id, 1)
                    )
                await conn.commit()
        except aiomysql.IntegrityError:
            await interaction.followup.send("❌ Tu as déjà une candidature en cours.", ephemeral=True)
            return

        embed_staff = self._ajouter_reponses(discord.Embed(
            title="Nouvelle candidature",
            description=f"Candidature de {interaction.user.mention} (`{interaction.user.id}`)",
            color=discord.Color.blue()
        ))
        try:
            msg = await channel.send(embed=embed_staff, view=copie_vue(Accepterview, None, set()))
            async with connexion() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "UPDATE role_special SET message_accepter_id = %s WHERE user_id = %s",
                        (msg.id, interaction.user.id)
                    )
                await conn.commit()
        except Exception as e:
            logger.error(f"[recrutement] Échec de l'envoi de la candidature de {interaction.user.id} : {e}")
            try:
                async with connexion() as conn:
                    async with conn.cursor() as c:
                        await c.execute("DELETE FROM role_special WHERE user_id = %s", (interaction.user.id,))
                    await conn.commit()
            except aiomysql.Error:
                pass
            await interaction.followup.send(
                "❌ Ta candidature n'a pas pu être envoyée au staff. Réessaie dans un instant.", ephemeral=True
            )
            return

        # Bouton du MP désactivé seulement maintenant, une fois la candidature
        # enregistrée : fermer le formulaire sans l'envoyer (ou un échec) laisse
        # le candidat le rouvrir.
        if interaction.message is not None:
            try:
                await interaction.message.edit(view=copie_vue(FormulaireBouton, interaction.message))
            except discord.HTTPException:
                pass

        embed = self._ajouter_reponses(discord.Embed(
            title="Formulaire reçu",
            description=f"Merci {interaction.user.mention} pour tes réponses !",
            color=discord.Color.green()
        ))
        await interaction.followup.send("✅ Formulaire envoyé au staff !", embed=embed, ephemeral=True)


class FormulaireBouton(VuePersistante):
    """Bouton du MP de recrutement (voir ConditionsSelect.commencer). Seul le
    destinataire du MP peut cliquer dessus : on vérifie simplement qu'on est
    bien en message privé."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await verifier_mp(interaction)

    @discord.ui.button(label="Remplir le formulaire", style=discord.ButtonStyle.green, custom_id="recrutement:remplir:formulaire")
    async def remplir_formulaire(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Vérifié avant d'ouvrir le formulaire (et de nouveau à l'envoi, voir
        # RecrutementModal) : le candidat n'écrit pas cinq réponses pour
        # apprendre ensuite qu'il ne peut pas postuler.
        membre = await membre_du_serveur(interaction.client, interaction.user.id)
        if membre is None:
            await repondre(interaction, "❌ Tu dois être membre du serveur Pixel Party pour postuler.")
            return
        try:
            erreur = await _refus_candidature(membre)
        except aiomysql.Error as e:
            logger.error(f"[recrutement] Erreur DB aux vérifications de {interaction.user.id} : {e}")
            await repondre(interaction, MSG_ERREUR)
            return
        if erreur:
            await repondre(interaction, erreur)
            return
        await interaction.response.send_modal(RecrutementModal())


class ConditionsSelect(VuePersistante):
    """Panneau public de recrutement (posté via /creer-message)."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await verifier_serveur(interaction)

    # ajoute un boutton pour commencer le recrutement après la description des rôles
    @discord.ui.button(label="Commencer", style=discord.ButtonStyle.green, custom_id="recrutement:commenciation")
    async def commencer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        # 🔒 Vérifications avant recrutement (voir _refus_candidature).
        try:
            erreur = await _refus_candidature(interaction.user)
        except aiomysql.Error as e:
            logger.error(f"[recrutement] Erreur DB aux vérifications de {interaction.user.id} : {e}")
            await interaction.followup.send(MSG_ERREUR, ephemeral=True)
            return
        if erreur:
            await interaction.followup.send(erreur, ephemeral=True)
            return

        embed =discord.Embed(title="Commencer le recrutement", description=f"Bienvenue dans le système de recrutement, {interaction.user.display_name} !", colour=discord.Colour.blue())
        embed.add_field(name="Étape 1 :", value="Remplis le formulaire ci-dessous pour donner tes informations au staff", inline=False)
        embed.add_field(name="Étape 2 :", value="Tu passes un entretien vocal avec un administrateur", inline=False)
        embed.add_field(name="Étape 3 :", value="Tu rejoins (ou non) le staff, en phase de **test**", inline=False)
        embed.add_field(name="Ensuite ?", value="En fonction de ton activité et de tes compétences, tu rejoins officiellement le staff à la fin de la période de test, ou tu redeviens membre.", inline=False)
        embed.add_field(name="Tu es prêt.e ?", value="Clique sur « Remplir le formulaire » ci-dessous. Attention : une fois le formulaire envoyé, tu t'engages et il n'y a pas de retour en arrière possible. Tout abus sera sanctionné.", inline=False)
        icon = interaction.guild.icon.url if interaction.guild.icon else None
        embed.set_footer(text="Pixel Party - Système de recrutement", icon_url=icon)
        embed2 = discord.Embed(title="La suite dans les messages privés", description="Pour que tu puisses mieux t'y retrouver, la suite du recrutement se déroule en messages privés.", colour=discord.Colour.blue())
        try:
            await interaction.user.send(embed=embed, view=copie_vue(FormulaireBouton, None, set()))
        except discord.HTTPException:
            await interaction.followup.send(
                "Tes messages privés sont désactivés. Active-les : c'est **obligatoire** pour continuer le recrutement.",
                ephemeral=True
            )
            return
        await interaction.followup.send(embed=embed2, ephemeral=True)


# ---------------------------------------------------------
# 👮 FIN DE PÉRIODE DE TEST STAFF
# Boutons du message envoyé par staff_test_watcher (start.py) au bout des 7
# jours : garder le membre dans le staff, ou lui retirer le rôle de test.
# Le membre et le rôle sont portés par le custom_id (DynamicItem) : les boutons
# restent utilisables après un redémarrage, sans table dédiée.
# ---------------------------------------------------------
class DecisionPeriodeTest(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"fin_test:(?P<action>garder|retirer):(?P<user_id>[0-9]+):(?P<role_id>[0-9]+)",
):
    def __init__(self, action: str, user_id: int, role_id: int):
        if action == "garder":
            bouton = discord.ui.Button(label="Garder dans le staff", style=discord.ButtonStyle.green, emoji="✅")
        else:
            bouton = discord.ui.Button(label="Retirer le rôle", style=discord.ButtonStyle.red, emoji="🚫")
        bouton.custom_id = f"fin_test:{action}:{user_id}:{role_id}"
        super().__init__(bouton)
        self.action = action
        self.user_id = user_id
        self.role_id = role_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(match["action"], int(match["user_id"]), int(match["role_id"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await verifier_staff(interaction, "❌ Seul le staff peut décider de la fin d'une période de test."):
            return False
        if interaction.user.id == self.user_id:
            return await refuser(interaction, "❌ Tu ne peux pas décider de ta propre période de test.")
        return True

    async def callback(self, interaction: discord.Interaction):
        # Un seul des deux boutons peut être traité, une seule fois (clé commune).
        if not reserver_clic(interaction, "fin_test"):
            await repondre(interaction, MSG_DEJA_TRAITE)
            return
        try:
            await self._decider(interaction)
        except Exception as e:
            liberer_clic(interaction, "fin_test")
            logger.error(f"[recrutement] Erreur sur la fin de période de test de {self.user_id} : {e}", exc_info=e)
            await repondre(interaction, MSG_ERREUR)

    async def _decider(self, interaction: discord.Interaction):
        guild = interaction.guild
        membre = guild.get_member(self.user_id)
        if membre is None:
            try:
                membre = await guild.fetch_member(self.user_id)
            except discord.NotFound:
                membre = None

        if self.action == "garder":
            if membre is None:
                statut = f"⚠️ <@{self.user_id}> a quitté le serveur."
            else:
                statut = f"✅ {membre.mention} reste dans le staff (décision de {interaction.user.mention})."
        else:
            role = guild.get_role(self.role_id)
            if membre is not None and role is not None and role in membre.roles:
                try:
                    await membre.remove_roles(role, reason=f"Fin de période de test, décision de {interaction.user}")
                except discord.HTTPException as e:
                    liberer_clic(interaction, "fin_test")
                    logger.warning(f"[recrutement] Impossible de retirer le rôle {self.role_id} à {self.user_id} : {e}")
                    await repondre(
                        interaction,
                        "❌ Impossible de retirer le rôle : vérifie que le rôle du bot est placé au-dessus."
                    )
                    return
            if membre is None:
                statut = f"⚠️ <@{self.user_id}> a quitté le serveur."
            else:
                statut = f"🚫 Rôle retiré à {membre.mention} (décision de {interaction.user.mention})."

        kwargs = {"view": vue_fin_periode_test(self.user_id, self.role_id, desactivee=True)}
        if interaction.message.embeds:
            embed = interaction.message.embeds[0].copy()
            embed.color = discord.Color.green() if self.action == "garder" else discord.Color.red()
            embed.add_field(name="Décision", value=statut, inline=False)
            kwargs["embed"] = embed
        await interaction.response.edit_message(**kwargs)


def vue_fin_periode_test(user_id: int, role_id: int, desactivee: bool = False) -> discord.ui.View:
    """Boutons de décision de fin de période de test (voir DecisionPeriodeTest),
    prêts à être envoyés. Vue arrêtée tout de suite : les clics sont reçus par
    DecisionPeriodeTest, enregistré au démarrage (bot.add_dynamic_items, voir
    cogs/events.py)."""
    vue = discord.ui.View(timeout=None)
    for action in ("garder", "retirer"):
        item = DecisionPeriodeTest(action, user_id, role_id)
        item.item.disabled = desactivee
        vue.add_item(item)
    vue.stop()
    return vue


class RecrutementCog(commands.Cog):
    # Le panneau de recrutement se poste désormais via /creer-message
    # (cogs/creermessage.py), qui reprend cet embed et ConditionsSelect ci-dessus.
    def __init__(self, bot):
        self.bot = bot

async def setup(bot):
    await bot.add_cog(RecrutementCog(bot))
