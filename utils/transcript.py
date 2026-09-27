"""Transcription HTML d'un ticket à sa fermeture (voir FermerView.create dans
cogs/tickets.py et ticket_watcher dans start.py), consultable ensuite par le staff
via /archive (cogs/archive.py). Les métadonnées enregistrées au passage (ouverture,
première réponse d'un modérateur, fermeture...) alimentent aussi /stats-staff
(cogs/stats_staff.py).

Le rendu « comme sur Discord » (messages, embeds, pièces jointes, réactions, fiche
des membres...) est délégué à chat-exporter (DiscordChatExporterPy). Ce module y
ajoute ce dont une archive a besoin pour rester lisible une fois le thread
supprimé (24h après la fermeture, voir ticket_watcher) : les pièces jointes sont
intégrées au fichier lui-même, car leurs liens Discord sont signés et expirent.
"""
import asyncio
import base64
import functools
import hashlib
import html
import importlib.resources
import io
import logging
import mimetypes
import re
import secrets
import time
import zlib

import aiohttp
import aiomysql
import chat_exporter
import discord
from PIL import Image, ImageOps

from utils.database import connexion
from utils.staff import est_moderateur

logger = logging.getLogger(__name__)

# Fuseau des horodatages affichés dans la page (réutilisé par cogs/archive.py).
FUSEAU = "Europe/Paris"

# Pièces jointes intégrées au fichier : 5 Mo au total. Une fois encodées en
# base64 (+33 %), ça fait ~6,7 Mo : le texte de la page garde une large marge sous
# la limite d'envoi de /archive (10 Mo sur un serveur sans boost, voir
# Guild.filesize_limit), et le blob compressé reste loin du max_allowed_packet
# par défaut de MariaDB (16 Mo). Au-delà, les fichiers suivants gardent leur lien
# Discord d'origine (toujours listés avec leur nom, mais le lien finira par expirer).
_BUDGET_PIECES_JOINTES = 5 * 1024 * 1024
# Plafond par fichier (après optimisation pour une image) : une seule vidéo ne doit
# pas consommer tout le budget au détriment des captures d'écran qui suivent.
_TAILLE_MAX_PAR_FICHIER = 2 * 1024 * 1024
# Une image source plus lourde n'est même pas téléchargée (mémoire limitée sur
# alwaysdata) ; en dessous, elle est presque toujours ramenée sous le plafond
# ci-dessus par la réduction + le réencodage.
_TAILLE_MAX_IMAGE_SOURCE = 15 * 1024 * 1024
# Taille maximale d'une image une fois décodée (largeur x hauteur x octets par
# pixel) ; au-delà, elle garde son lien. La réduire demande temporairement 2 à 3
# fois plus de mémoire (copies intermédiaires de Pillow, dont une copie
# prémultipliée pour la transparence : mesuré ~+60 Mo pour 24 Mo décodés en RGBA),
# de quoi faire tomber le bot sur un hébergement à RAM limitée. Couvre les
# captures d'écran jusqu'au 1440p / Retina ; une capture 4K en RGBA (33 Mo) garde
# son lien. Les photos JPEG passent quasiment toujours : elles sont décodées
# directement à taille réduite (voir _optimiser_image).
_OCTETS_DECODES_MAX = 24 * 1024 * 1024
# Côté le plus long après réduction : largement assez pour lire une capture
# d'écran en plein écran, pour une fraction du poids d'origine.
_COTE_MAX = 1600
_QUALITE_WEBP = 80
# Formats qu'un navigateur affiche tels quels : un fichier d'origine déjà plus
# léger que sa version réencodée est gardé tel quel.
_FORMATS_NAVIGATEUR = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_MIME_VALIDE = re.compile(r"[a-z0-9.+-]+/[a-z0-9.+-]+")

# Garde-fou (réseau lent, CDN d'emojis de chat-exporter injoignable...) : au-delà,
# l'archive est enregistrée sans page plutôt que de bloquer indéfiniment.
_DELAI_GENERATION = 180
# Marge sous le max_allowed_packet par défaut de MariaDB (16 Mo) : la requête
# entière (blob échappé compris) doit tenir dans un seul paquet.
_TAILLE_MAX_COMPRESSEE = 12 * 1024 * 1024

# Une seule transcription à la fois : limite le pic mémoire (historique complet,
# images décodées, page en construction) sur un hébergement à RAM limitée, et
# chat-exporter garde un cache global remis à zéro à la fin de chaque export —
# deux exports simultanés se le videraient mutuellement.
_verrou = asyncio.Lock()

_UPSERT_ARCHIVE = """
INSERT INTO ticket_archives (
    thread_id, nom, membre_id, modo_id, raison, created_at, first_response_at,
    first_response_by, closed_at, closed_by, nb_messages, html, html_taille, html_closed_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    nom = VALUES(nom),
    membre_id = VALUES(membre_id),
    modo_id = VALUES(modo_id),
    raison = VALUES(raison),
    created_at = VALUES(created_at),
    first_response_at = VALUES(first_response_at),
    first_response_by = VALUES(first_response_by),
    closed_at = VALUES(closed_at),
    closed_by = VALUES(closed_by),
    nb_messages = VALUES(nb_messages),
    html_taille = COALESCE(VALUES(html_taille), html_taille),
    html = COALESCE(VALUES(html), html),
    html_closed_at = COALESCE(VALUES(html_closed_at), html_closed_at)
"""
# Upsert : un ticket rouvert (ConfirmationClotureView, cogs/tickets.py) puis
# refermé met à jour son archive au lieu d'en créer une seconde. Si la page n'a
# pas pu être générée cette fois-ci (html, html_taille et html_closed_at NULL
# ensemble), on garde celle de la fermeture précédente plutôt que de la perdre ;
# html_closed_at indique alors qu'elle date d'avant la réouverture.

# Injecté juste avant </body>. Les images et pistes audio ne portent leurs données
# qu'une fois, dans leur src (voir _PiecesJointes.assembler) : le lien qui les
# entoure est reconstruit ici au clic en URL blob:. Les navigateurs bloquent de
# toute façon l'ouverture d'une URL data: dans un onglet ; une URL blob: s'ouvre
# normalement (image en grand) ou se télécharge sous son vrai nom (audio).
_SCRIPT_PIECES_JOINTES = b"""
    <style>[data-pj-lien] { cursor: pointer; }</style>
    <script>
      document.addEventListener("click", function (e) {
        var lien = e.target.closest && e.target.closest("[data-pj-lien]");
        if (!lien || lien.hasAttribute("href")) return;
        var media = document.querySelector('[data-pj="' + lien.getAttribute("data-pj-lien") + '"]');
        if (!media) return;
        var src = media.getAttribute("src"), virgule = src.indexOf(",");
        var binaire = atob(src.slice(virgule + 1)), octets = new Uint8Array(binaire.length);
        for (var i = 0; i < binaire.length; i++) octets[i] = binaire.charCodeAt(i);
        var type = src.slice(5, virgule).split(";")[0];
        lien.href = URL.createObjectURL(new Blob([octets], { type: type }));
        if (media.tagName !== "IMG") lien.download = lien.textContent.trim() || "fichier";
      });
    </script>
"""


# Gestionnaires d'évènements inline (onclick="...") des gabarits de chat-exporter :
# aperçu d'un message cité (reference.html, pin.html), menu déroulant
# (component_menu.html), spoiler (parse/ast.py) et lecteur audio (onclick="").
_GESTIONNAIRES_GABARIT = re.compile(
    rb"""\son[a-z]+="(scrollToMessage\(event, '\d*'\)|showDropdown\(\d+\)|showSpoiler\(event, this\)|)\""""
)


def _empreinte_csp(code: bytes) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(code).digest()).decode("ascii") + "'"


@functools.cache
def _sources_scripts_gabarit() -> str:
    """Sources CSP des scripts du gabarit de chat-exporter (voir _ajouter_csp) :
    empreinte de chacun de ses scripts inline, plus le nôtre, et adresse exacte de
    ses scripts externes. Lues dans le gabarit installé, pour suivre une mise à
    jour de la bibliothèque sans rien recopier ici — il ne contient aucune
    variable dans ses scripts, qui se retrouvent donc tels quels dans la page."""
    gabarit = importlib.resources.files("chat_exporter").joinpath("html/base.html").read_bytes()
    inline = re.findall(rb"<script>(.*?)</script>", gabarit, re.S)
    inline += re.findall(rb"<script>(.*?)</script>", _SCRIPT_PIECES_JOINTES, re.S)
    externes = re.findall(rb'<script src="(https://[^"]+)"', gabarit)
    return " ".join(
        [_empreinte_csp(code) for code in inline] + [url.decode("ascii") for url in externes]
    )


def _ajouter_csp(page: bytes) -> bytes:
    """Ajoute une politique de sécurité du contenu (CSP) en tête de la page.

    chat-exporter (3.1.0) n'échappe pas tout ce qui vient des membres : aperçu du
    message auquel on répond, pseudo dans la fiche d'un membre, liens
    « javascript: », noms de fichiers. Sans cette politique, un membre pourrait
    faire exécuter du JavaScript à l'ouverture de l'archive par un modérateur
    (falsifier l'affichage des preuves, envoyer la page ailleurs...). Le
    navigateur n'exécute plus que les scripts du gabarit et le nôtre, reconnus à
    leur empreinte SHA-256 : tout script, gestionnaire « on...= » ou lien
    « javascript: » injecté est ignoré. La page ne peut rien envoyer nulle part
    (ni requête, ni formulaire), seulement afficher des images, médias, styles et
    polices."""
    gestionnaires = sorted({m.group(1) for m in _GESTIONNAIRES_GABARIT.finditer(page)})
    politique = (
        "default-src 'none'; "
        # 'unsafe-hashes' : autorise les gestionnaires inline légitimes listés
        # ci-dessus, par leur empreinte (ils n'appellent que les fonctions du
        # gabarit).
        f"script-src 'unsafe-hashes' {_sources_scripts_gabarit()} "
        + " ".join(_empreinte_csp(code) for code in gestionnaires) + "; "
        "style-src 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "font-src data: https://cdn.jsdelivr.net; "
        # Avatars, emojis, images des embeds (hébergées n'importe où) ; data: et
        # blob: pour les pièces jointes intégrées (voir _SCRIPT_PIECES_JOINTES).
        "img-src data: blob: https:; media-src data: blob: https:; "
        "object-src 'none'; base-uri 'none'; form-action 'none'"
    )
    # En tout début de <head> (celui du gabarit, avant tout contenu variable) pour
    # s'appliquer à toute la page. Le charset passe devant : un navigateur ne le
    # cherche que dans les 1024 premiers octets, que la liste d'empreintes peut
    # dépasser.
    meta = (
        '<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="'
        + html.escape(politique, quote=True) + '">'
    ).encode("ascii")
    return page.replace(b"<head>", b"<head>" + meta, 1)


_BALISE_META = re.compile(rb"<meta", re.I)


def _neutraliser_meta(page: bytes) -> bytes:
    """Rend inerte toute balise <meta> après le <head> du gabarit (qui n'en a
    aucune ailleurs) : un <meta http-equiv="refresh"> injecté par un membre (voir
    _ajouter_csp) redirigerait le modérateur vers la page de son choix, et la CSP
    n'y peut rien. Du texte échappé n'en contient jamais (« &lt;meta ») : seule
    une balise injectée est touchée, et devient un élément inconnu sans effet."""
    fin_head = page.find(b"</head>")
    if fin_head == -1:
        return page
    return page[:fin_head] + _BALISE_META.sub(b"<x-meta", page[fin_head:])


def _type_mime(attachment: discord.Attachment) -> str:
    """Type MIME réduit à sa partie essentielle (sans "; charset=...") et assaini :
    il finit dans un attribut HTML de la page."""
    brut = (attachment.content_type or "").split(";")[0].strip().lower()
    if _MIME_VALIDE.fullmatch(brut):
        return brut
    devine = mimetypes.guess_type(attachment.filename)[0]
    return devine if devine and _MIME_VALIDE.fullmatch(devine) else "application/octet-stream"


def _optimiser_image(contenu: bytes, type_mime: str) -> tuple[bytes, str] | None:
    """Réduit et réencode une image en WebP. Renvoie (contenu, type MIME) à
    intégrer, ou None pour garder le lien d'origine. Lève si Pillow ne sait pas
    lire le fichier (SVG, HEIC, image corrompue...) : même résultat, via
    _PiecesJointes.process_asset.

    Exécutée dans un thread (asyncio.to_thread) : décoder puis réencoder une grosse
    image bloquerait sinon toute la boucle du bot, heartbeat gateway compris."""
    with Image.open(io.BytesIO(contenu)) as image:
        if getattr(image, "is_animated", False):
            # GIF/WebP/PNG animés : réencoder chaque image coûterait cher en CPU pour
            # un gain incertain ; gardés tels quels, dans la limite du plafond par fichier.
            return (contenu, type_mime) if type_mime in _FORMATS_NAVIGATEUR else None
        echelle = _COTE_MAX / max(image.size)
        if image.format == "JPEG" and echelle < 1:
            # Décodage direct à 1/2, 1/4 ou 1/8 de la taille (jamais en dessous de la
            # taille finale, d'où les dimensions exactes visées plutôt qu'un carré
            # _COTE_MAX x _COTE_MAX) : une photo de téléphone n'est jamais
            # décompressée en pleine résolution.
            image.draft("RGB", (max(int(image.width * echelle), 1), max(int(image.height * echelle), 1)))
        # Vérifié avant le décodage (seul l'en-tête a été lu jusqu'ici). Palette,
        # transparence, 16 bits... : 4 octets par pixel une fois convertie, par prudence.
        transparence = image.has_transparency_data
        octets_par_pixel = 3 if image.mode in ("1", "L", "RGB", "YCbCr") and not transparence else 4
        if image.width * image.height * octets_par_pixel > _OCTETS_DECODES_MAX:
            return None
        # Applique l'orientation EXIF avant qu'elle ne disparaisse au réencodage
        # (sinon une photo prise en portrait s'afficherait couchée).
        ImageOps.exif_transpose(image, in_place=True)
        if image.mode in ("I", "I;16", "I;16B", "I;16L", "F"):
            # Niveaux de gris sur 16 bits (ou plus) : convertie directement, chaque
            # valeur au-delà de 255 serait écrêtée et l'image sortirait toute
            # blanche. Ramenée d'abord sur 8 bits.
            image = image.convert("I").point(lambda v: v * (1 / 256)).convert("L")
        # Avant la réduction : Pillow réduit une image à palette sans lissage
        # (plus proche voisin), ce qui donnerait des bords en escalier. Une image
        # RGB peut aussi porter une couleur transparente (tRNS d'un PNG) : sans
        # passage en RGBA, elle deviendrait opaque.
        if transparence:
            if image.mode != "RGBA":
                image = image.convert("RGBA")
        elif image.mode != "RGB":
            image = image.convert("RGB")
        image.thumbnail((_COTE_MAX, _COTE_MAX))
        sortie = io.BytesIO()
        image.save(sortie, "WEBP", quality=_QUALITE_WEBP)

    webp = sortie.getvalue()
    if len(webp) >= len(contenu) and type_mime in _FORMATS_NAVIGATEUR:
        return contenu, type_mime
    return webp, "image/webp"


class _PiecesJointes(chat_exporter.AttachmentHandler):
    """Intègre les pièces jointes au fichier HTML (URL data:, en base64), dans la
    limite de _BUDGET_PIECES_JOINTES : leurs liens Discord sont signés et expirent,
    et le thread lui-même est supprimé 24h après la fermeture — sans ça, les images
    d'une archive consultée plus tard seraient toutes cassées.

    chat-exporter appelle process_asset pour chaque pièce jointe (message et
    messages transférés), puis insère attachment.url tel quel dans ses gabarits
    (sans échappement ni paramètre ajouté derrière). On y met un simple jeton, et
    les données ne sont insérées qu'une fois la page générée (voir assembler) :
    - les gabarits image et audio utilisent l'URL deux fois (lien + src) : l'y
      mettre directement doublerait le poids de chaque image ;
    - plusieurs Mo de base64 ne transitent pas par les nombreuses concaténations
      de chaîne de chat-exporter (ni par des str Python, jusqu'à 4 octets par
      caractère dès qu'un emoji est présent dans la page)."""

    def __init__(self):
        # Jeton aléatoire par export : ne peut pas apparaître par hasard dans le
        # contenu d'un message, et ne contient que [a-z0-9-], sans danger dans un
        # attribut HTML sans guillemets (gabarits image/vidéo de chat-exporter).
        self.prefixe = f"pixelbot-pj-{secrets.token_hex(8)}-"
        self.pieces: list[tuple[str, bytes, str]] = []  # (type MIME, contenu, nom du fichier)
        self.restant = _BUDGET_PIECES_JOINTES
        self.non_integrees = 0

    async def process_asset(self, attachment: discord.Attachment) -> discord.Attachment:
        try:
            piece = await self._preparer(attachment)
        except Exception as e:
            # Une pièce jointe illisible (supprimée entre-temps, format inconnu...)
            # ne doit pas faire échouer toute la transcription : elle garde son lien.
            logger.warning(f"[transcript] Pièce jointe {attachment.filename!r} non intégrée : {e}")
            piece = None

        if piece is None:
            self.non_integrees += 1
            return attachment

        self.pieces.append(piece)
        attachment.url = attachment.proxy_url = f"{self.prefixe}{len(self.pieces) - 1}"
        return attachment

    async def _preparer(self, attachment: discord.Attachment) -> tuple[str, bytes, str] | None:
        type_mime = _type_mime(attachment)
        est_image = type_mime.startswith("image/")
        # Vérifié avant téléchargement : un fichier qui ne rentrera pas n'est pas lu
        # pour rien. Seule une image peut encore maigrir après coup.
        if self.restant <= 0:
            return None
        if attachment.size > (_TAILLE_MAX_IMAGE_SOURCE if est_image else min(_TAILLE_MAX_PAR_FICHIER, self.restant)):
            return None

        # Lu tout de suite : l'URL, fraîchement signée par la lecture de l'historique,
        # est valide à ce moment-là.
        contenu = await attachment.read()
        if est_image:
            optimise = await asyncio.to_thread(_optimiser_image, contenu, type_mime)
            if optimise is None:
                return None
            contenu, type_mime = optimise

        if len(contenu) > min(_TAILLE_MAX_PAR_FICHIER, self.restant):
            return None
        self.restant -= len(contenu)
        return type_mime, contenu, attachment.filename

    def assembler(self, page: bytes) -> bytes:
        """Remplace les jetons de la page générée par les données des pièces jointes,
        en une seule passe sur des octets. Chaque pièce n'est écrite qu'une fois :
        - dans le src qui l'affiche (image, vidéo, audio), qu'on marque data-pj ;
        - le lien qui entoure une image ou accompagne un audio perd son href au
          profit de data-pj-lien, reconstruit au clic (voir _SCRIPT_PIECES_JOINTES) ;
        - un fichier sans aperçu (PDF, texte...) n'a qu'un lien : il reçoit les
          données directement, avec download pour garder son nom au téléchargement."""
        if not self.pieces:
            return page

        prefixe = re.escape(self.prefixe.encode("ascii"))
        avec_src = {int(m.group(1)) for m in re.finditer(rb'src="?' + prefixe + rb"(\d+)", page)}

        def remplacer(match: re.Match) -> bytes:
            attribut, index = match.group(1), int(match.group(3))
            if attribut == b"href" and index in avec_src:
                return b'data-pj-lien="%d"' % index

            type_mime, contenu, nom = self.pieces[index]
            valeur = b"data:" + type_mime.encode("ascii") + b";base64," + base64.b64encode(contenu)
            if attribut == b"src":
                return b'src="' + valeur + b'" data-pj="%d"' % index
            nom_html = html.escape(nom, quote=True).encode("utf-8")
            return b'href="' + valeur + b'" download="' + nom_html + b'"'

        resultat = re.sub(rb'(href|src)=("?)' + prefixe + rb'(\d+)\2', remplacer, page)
        if self.prefixe.encode("ascii") in resultat:
            # Gabarit de chat-exporter modifié (mise à jour de la bibliothèque) : les
            # pièces concernées resteraient cassées dans la page, à corriger ici.
            logger.warning("[transcript] Jeton de pièce jointe non remplacé dans la page générée.")
        return resultat


def _injecter_script(page: bytes) -> bytes:
    """Ajoute _SCRIPT_PIECES_JOINTES juste avant la dernière balise </body> (le
    contenu des messages est échappé par chat-exporter : la dernière est celle du
    gabarit)."""
    fin = page.rfind(b"</body>")
    if fin == -1:
        return page + _SCRIPT_PIECES_JOINTES
    return page[:fin] + _SCRIPT_PIECES_JOINTES + page[fin:]


async def _generer_html(bot, thread: discord.Thread, messages: list[discord.Message]) -> tuple[bytes | None, int | None]:
    """Page HTML compressée (zlib) et sa taille décompressée, ou (None, None) si la
    génération échoue — l'archive est alors enregistrée sans page, ses métadonnées
    restent utiles aux statistiques staff."""
    if not messages:
        # raw_export relirait alors lui-même tout l'historique (Transcript.export) :
        # rien à montrer de toute façon.
        return None, None

    for essai in (1, 2):
        pieces = _PiecesJointes()
        try:
            # messages est dans l'ordre de thread.history() (du plus récent au plus
            # ancien), celui qu'attend raw_export : il l'inverse lui-même, sur place.
            page = await asyncio.wait_for(
                chat_exporter.raw_export(
                    thread,
                    messages,
                    tz_info=FUSEAU,
                    guild=thread.guild,
                    bot=bot,
                    military_time=True,
                    # Pas de lien de don vers l'auteur de la bibliothèque dans nos archives.
                    support_dev=False,
                    attachment_handler=pieces,
                    # Sinon chat-exporter avale l'erreur et renvoie une page "Whoops!
                    # Something went wrong..." qu'on enregistrerait comme une vraie archive.
                    raise_exceptions=True,
                ),
                timeout=_DELAI_GENERATION,
            )
            break
        except aiohttp.ClientError as e:
            # chat-exporter vérifie chaque emoji auprès de son CDN et n'y tolère
            # qu'une connexion refusée : une connexion coupée en cours de route
            # fait échouer toute la page. Un second essai suffit en général.
            if essai == 2:
                logger.error(
                    f"[transcript] Génération HTML du ticket {thread.id} impossible, archive enregistrée "
                    f"sans page : {type(e).__name__}: {e}",
                    exc_info=True,
                )
                return None, None
            logger.warning(f"[transcript] Ticket {thread.id} : erreur réseau pendant la génération ({e!r}), nouvel essai.")
            # Historique relu : chat-exporter modifie les messages qu'on lui passe
            # (liste inversée, liens des pièces jointes remplacés par nos jetons,
            # contenu vide d'un message cité remplacé par un texte d'aperçu).
            try:
                messages = [message async for message in thread.history(limit=None)]
            except discord.HTTPException as e2:
                logger.error(f"[transcript] Ticket {thread.id} : historique illisible pour le second essai : {e2}")
                return None, None
        except Exception as e:
            logger.error(
                f"[transcript] Génération HTML du ticket {thread.id} impossible, archive enregistrée "
                f"sans page : {type(e).__name__}: {e}",
                exc_info=True,
            )
            return None, None

    try:
        # Script et CSP ajoutés tant que la page est encore légère, puis les données
        # des pièces jointes en une seule passe (voir _PiecesJointes.assembler).
        brut = pieces.assembler(_ajouter_csp(_neutraliser_meta(_injecter_script(page.encode("utf-8")))))
        del page
        pieces.pieces.clear()

        taille = len(brut)
        compresse = await asyncio.to_thread(zlib.compress, brut)
        del brut
    except Exception as e:
        logger.error(
            f"[transcript] Génération HTML du ticket {thread.id} impossible, archive enregistrée "
            f"sans page : {type(e).__name__}: {e}",
            exc_info=True,
        )
        return None, None

    if len(compresse) > _TAILLE_MAX_COMPRESSEE:
        logger.error(
            f"[transcript] Page du ticket {thread.id} trop volumineuse ({len(compresse)} octets "
            "compressés), archive enregistrée sans page."
        )
        return None, None

    logger.info(
        f"[transcript] Ticket {thread.id} : page de {taille} octets ({len(compresse)} compressés), "
        f"{_BUDGET_PIECES_JOINTES - pieces.restant} octets de pièces jointes intégrées, "
        f"{pieces.non_integrees} laissée(s) en lien."
    )
    return compresse, taille


async def _archiver(bot, thread: discord.Thread, closed_by: int | None, closed_at: int) -> bool:
    async with connexion() as conn:
        async with conn.cursor() as c:
            await c.execute(
                "SELECT membre_id, modo_id, raison, closed_by FROM ticket WHERE thread_id = %s",
                (thread.id,)
            )
            ligne = await c.fetchone()

    if ligne is None:
        logger.warning(f"[transcript] Ticket {thread.id} absent de la table `ticket` : archive abandonnée.")
        return False
    membre_id, modo_id, raison, ferme_par = ligne
    if closed_by is None:
        # Archive (re)générée après coup (filet de sécurité de ticket_watcher) :
        # qui a fermé le ticket est resté enregistré dans `ticket` (NULL pour une
        # fermeture automatique).
        closed_by = ferme_par

    # Historique lu une seule fois, pour les métadonnées comme pour la page.
    messages = [message async for message in thread.history(limit=None)]

    # Premier message d'un modérateur (autre que le membre du ticket lui-même, qui
    # peut être modérateur) : sert au temps de réponse de /stats-staff. Les messages
    # système ("X a épinglé un message", "X a ajouté Y au fil"...) ne comptent pas
    # comme une réponse.
    first_response_at = first_response_by = None
    participants = set()
    for message in reversed(messages):  # du plus ancien au plus récent
        if message.author.bot:
            continue
        participants.add(message.author.id)
        if (
            first_response_by is None
            and not message.is_system()
            and message.author.id != membre_id
            and est_moderateur(message.author)
        ):
            first_response_at = int(message.created_at.timestamp())
            first_response_by = message.author.id
    participants.update(uid for uid in (modo_id, closed_by) if uid is not None)
    nb_messages = len(messages)

    html_compresse, html_taille = await _generer_html(bot, thread, messages)
    del messages

    async with connexion() as conn:
        async with conn.cursor() as c:
            await c.execute(
                _UPSERT_ARCHIVE,
                (
                    thread.id,
                    thread.name[:100],
                    membre_id,
                    modo_id,
                    raison,
                    # Le thread est créé à l'ouverture du ticket (TicketCreateView) :
                    # son id (snowflake) porte donc l'heure d'ouverture.
                    int(discord.utils.snowflake_time(thread.id).timestamp()),
                    first_response_at,
                    first_response_by,
                    closed_at,
                    closed_by,
                    nb_messages,
                    html_compresse,
                    html_taille,
                    closed_at if html_compresse is not None else None,
                ),
            )
            if participants:
                await c.executemany(
                    "INSERT IGNORE INTO ticket_archive_participants (user_id, thread_id) VALUES (%s, %s)",
                    [(user_id, thread.id) for user_id in participants],
                )
        await conn.commit()
    return html_compresse is not None


async def archiver_ticket(bot, thread: discord.Thread, *, closed_by: int | None, closed_at: int | None = None) -> bool:
    """Génère et enregistre (ou met à jour) l'archive d'un ticket qui vient d'être
    fermé. closed_by = qui a cliqué « Fermer le ticket », None pour une fermeture
    automatique ou une archive générée après coup (repris alors de `ticket`) ;
    closed_at = même horodatage que celui écrit dans `ticket` (maintenant par
    défaut).

    Renvoie True si l'archive a été enregistrée avec sa page. Ne lève jamais : la
    fermeture d'un ticket ne doit pas échouer à cause de son archive. Les échecs
    sont loggés (ERROR/CRITICAL, donc signalés en MP à l'owner)."""
    if closed_at is None:
        closed_at = int(time.time())
    try:
        async with _verrou:
            return await _archiver(bot, thread, closed_by, closed_at)
    except aiomysql.Error as e:
        logger.critical(f"[transcript] Erreur DB en archivant le ticket {thread.id} : {e}", exc_info=True)
    except Exception as e:
        logger.error(f"[transcript] Archivage du ticket {thread.id} impossible : {e}", exc_info=True)
    return False


async def archive_a_jour(thread_id: int, closed_at: int) -> bool:
    """Vrai si ce ticket a une archive complète (avec sa page) de sa dernière
    fermeture — voir le filet de sécurité de ticket_watcher dans start.py, juste
    avant la suppression du thread. Une archive d'une fermeture précédente (ticket
    rouvert puis refermé depuis) ou sans page (génération en échec) ne compte pas,
    y compris quand la page gardée est celle d'avant la réouverture (voir
    _UPSERT_ARCHIVE)."""
    async with connexion() as conn:
        async with conn.cursor() as c:
            await c.execute(
                "SELECT 1 FROM ticket_archives WHERE thread_id = %s AND html_closed_at = %s",
                (thread_id, closed_at)
            )
            return await c.fetchone() is not None
