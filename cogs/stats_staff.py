"""/stats-staff : nombre de tickets traités et temps de réponse moyen de chaque
modérateur, avec leur classement. Réservé au staff.

Tout vient de `ticket_archives` (une ligne écrite à la fermeture de chaque ticket,
voir utils/transcript.py) : seuls les tickets fermés sont comptés. Les
statistiques démarrent donc vides au déploiement, et un ticket encore ouvert n'y
apparaît qu'une fois fermé.
"""
import logging
import time

import aiomysql
import discord
from discord import app_commands
from discord.ext import commands

from utils.database import connexion
from utils.staff import est_moderateur, est_owner, formater_duree

logger = logging.getLogger(__name__)

# Nombre de modérateurs listés. Une ligne du classement fait au plus ~110
# caractères (mention + compteurs) : 15 lignes, l'en-tête et la ligne de position
# ajoutée pour qui lance la commande restent loin des 4096 caractères autorisés
# dans la description d'un embed.
TOP_AFFICHE = 15

# Nombre minimum de réponses pour le « ⚡ Plus rapide » : sans seuil, un modérateur
# qui n'a répondu qu'à un seul ticket (par chance dans la minute) passerait devant
# quelqu'un qui répond vite sur des dizaines de tickets. 3 reste assez bas pour
# qu'un petit staff ait un gagnant dès les premiers jours, ou sur « 7 derniers jours ».
MIN_REPONSES_PLUS_RAPIDE = 3

MEDAILLES = ["🥇", "🥈", "🥉"]

# Jamais de colonne `html` ici (LONGBLOB, potentiellement lourd) : uniquement des
# agrégats sur les métadonnées. AVG ignore les NULL, donc les tickets sans réponse
# d'un modérateur (first_response_at NULL) n'entrent pas dans la moyenne globale.
REQUETE_GLOBALE = """
SELECT COUNT(*),
       AVG(first_response_at - created_at),
       SUM(first_response_at IS NULL)
FROM ticket_archives
WHERE closed_at >= %s
"""

# Une ligne par modérateur, issue de deux agrégats distincts :
# - tickets traités, attribués à COALESCE(modo_id, first_response_by) : celui qui a
#   cliqué « Prendre en charge », sinon celui qui a répondu en premier ;
# - temps de réponse, attribué à first_response_by (le premier modérateur à écrire).
# Les deux ne désignent pas forcément la même personne (un modo peut répondre en
# premier sur un ticket qu'un autre prend ensuite en charge) : un modérateur peut
# donc avoir des tickets traités sans temps de réponse, ou l'inverse. MariaDB n'a
# pas de FULL OUTER JOIN : on empile les deux agrégats (UNION ALL) puis on les
# regroupe par modérateur. La moyenne est recalculée à partir de la somme et du
# nombre de réponses, ce qui revient exactement à AVG sur ses tickets.
# Le tri se fait sur une table dérivée pour que `moyenne IS NULL` porte sur une
# vraie colonne, sans dépendre de la résolution des alias dans ORDER BY.
REQUETE_PAR_MODO = """
SELECT modo, traites, reponses, moyenne
FROM (
    SELECT modo,
           SUM(nb_traites) AS traites,
           SUM(nb_reponses) AS reponses,
           SUM(total_reponses) / NULLIF(SUM(nb_reponses), 0) AS moyenne
    FROM (
        SELECT COALESCE(modo_id, first_response_by) AS modo,
               COUNT(*) AS nb_traites, 0 AS nb_reponses, 0 AS total_reponses
        FROM ticket_archives
        WHERE closed_at >= %s AND COALESCE(modo_id, first_response_by) IS NOT NULL
        GROUP BY COALESCE(modo_id, first_response_by)
        UNION ALL
        SELECT first_response_by, 0, COUNT(*), SUM(first_response_at - created_at)
        FROM ticket_archives
        WHERE closed_at >= %s AND first_response_by IS NOT NULL AND first_response_at IS NOT NULL
        GROUP BY first_response_by
    ) AS par_source
    GROUP BY modo
) AS par_modo
ORDER BY traites DESC, moyenne IS NULL, moyenne, modo
"""


def _s(n: int) -> str:
    """Marque du pluriel : en français, 0 et 1 restent au singulier."""
    return "s" if n > 1 else ""


def _ligne_classement(position: int, modo_id: int, traites: int, reponses: int,
                      moyenne: float | None, est_toi: bool) -> str:
    rang = MEDAILLES[position - 1] if position <= len(MEDAILLES) else f"#{position}"
    # La ligne de qui lance la commande passe entièrement en gras : le nombre n'y
    # est alors pas mis en gras séparément, un ** imbriqué casserait le rendu
    # Markdown de Discord.
    nombre = str(traites) if est_toi else f"**{traites}**"
    texte = f"{rang} <@{modo_id}> — {nombre} ticket{_s(traites)}"
    if moyenne is not None:
        texte += f" · ⏱️ {formater_duree(moyenne)} en moyenne ({reponses} réponse{_s(reponses)})"
    else:
        # Tickets pris en charge, mais un autre modérateur a toujours écrit avant.
        texte += " · ⏱️ aucune première réponse"
    return f"**{texte}** ← toi" if est_toi else texte


class StatsStaff(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="stats-staff",
        description="Tickets traités et temps de réponse moyen des modérateurs (réservé au staff)"
    )
    @app_commands.describe(periode="Période analysée, selon la date de fermeture des tickets (défaut : depuis le début)")
    @app_commands.choices(periode=[
        app_commands.Choice(name="7 derniers jours", value=7),
        app_commands.Choice(name="30 derniers jours", value=30),
        app_commands.Choice(name="Depuis le début", value=0),
    ])
    # Masque la commande aux membres sans « Gérer les messages » (même permission
    # que /warn, cf. utils/staff.py). Ce n'est qu'un réglage par défaut, qu'un
    # admin peut ouvrir à d'autres rôles depuis les paramètres d'intégration du
    # serveur : la vraie vérification est faite au début de la commande.
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def stats_staff(self, interaction: discord.Interaction, periode: app_commands.Choice[int] | None = None):
        if not (est_moderateur(interaction.user) or est_owner(interaction.user.id)):
            await interaction.response.send_message(
                "❌ Cette commande est réservée aux modérateurs.", ephemeral=True
            )
            return

        # Éphémère de bout en bout : les statistiques du staff ne doivent pas
        # s'afficher dans un salon public.
        await interaction.response.defer(ephemeral=True)

        jours = periode.value if periode is not None else 0
        # 0 = depuis le début : closed_at >= 0 garde toutes les lignes, ce qui
        # évite une seconde variante des requêtes sans filtre.
        depuis = int(time.time()) - jours * 86400 if jours else 0
        libelle_periode = periode.name if periode is not None else "Depuis le début"

        try:
            async with connexion() as conn:
                async with conn.cursor() as c:
                    await c.execute(REQUETE_GLOBALE, (depuis,))
                    total, moyenne_globale, sans_reponse = await c.fetchone()
                    await c.execute(REQUETE_PAR_MODO, (depuis, depuis))
                    rows = await c.fetchall()
        except aiomysql.Error as e:
            logger.critical(f"[stats-staff] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
            return

        if not total:
            await interaction.followup.send(
                "📭 Aucun ticket archivé sur cette période.\n"
                "-# Seuls les tickets fermés depuis la mise en place des archives sont comptés.",
                ephemeral=True
            )
            return

        # SUM/AVG/division renvoient des Decimal côté aiomysql : conversion une fois
        # pour toutes, avant formatage et comparaisons.
        classement = [
            (int(modo), int(traites), int(reponses), float(moyenne) if moyenne is not None else None)
            for modo, traites, reponses, moyenne in rows
        ]

        en_tete = [
            f"🎫 Tickets fermés : **{total}**",
            "⏱️ Temps de réponse moyen : "
            + (f"**{formater_duree(float(moyenne_globale))}**" if moyenne_globale is not None else "—"),
            f"📭 Sans réponse d'un modérateur : **{int(sans_reponse or 0)}**",
        ]
        eligibles = [
            ligne for ligne in classement
            if ligne[3] is not None and ligne[2] >= MIN_REPONSES_PLUS_RAPIDE
        ]
        if eligibles:
            # À moyenne égale, celui qui a le plus de réponses l'emporte.
            modo_id, _, reponses, moyenne = min(eligibles, key=lambda ligne: (ligne[3], -ligne[2]))
            en_tete.append(
                f"⚡ Plus rapide : <@{modo_id}> — {formater_duree(moyenne)} en moyenne ({reponses} réponses)"
            )

        lignes = []
        position_toi = None
        for position, (modo_id, traites, reponses, moyenne) in enumerate(classement, start=1):
            est_toi = modo_id == interaction.user.id
            if est_toi:
                position_toi = position
            if position <= TOP_AFFICHE:
                lignes.append(_ligne_classement(position, modo_id, traites, reponses, moyenne, est_toi))

        if not classement:
            lignes.append("Aucun modérateur à classer sur cette période.")
        elif position_toi is not None and position_toi > TOP_AFFICHE:
            modo_id, traites, reponses, moyenne = classement[position_toi - 1]
            lignes.append("…")
            lignes.append(_ligne_classement(position_toi, modo_id, traites, reponses, moyenne, True))
        elif position_toi is None:
            lignes.append("\n*Tu n'apparais pas dans ce classement sur cette période.*")

        embed = discord.Embed(
            title=f"📊 Statistiques staff — {libelle_periode.lower()}",
            description="\n".join(en_tete) + "\n\n🏆 **Classement**\n" + "\n".join(lignes),
            color=discord.Color.blurple()
        )
        embed.set_footer(
            text="Tickets traités : pris en charge (ou, à défaut, première réponse). "
                 "Temps de réponse : de l'ouverture au premier message d'un modérateur. "
                 "Tickets fermés uniquement."
        )
        # Les mentions dans un embed ne notifient personne : afficher <@id> ici
        # ne dérange pas les modérateurs cités.
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(StatsStaff(bot))
