"""Génère l'image de /profil (avatar, pseudo, rang, niveau, barre de progression
XP, argent) — voir cogs/profile.py, qui appelle generate_profile_card() en lieu
et place de l'ancien embed texte.

Le rendu Pillow est bloquant/CPU-bound : fait dans _render (synchrone) puis
déporté via asyncio.to_thread par generate_profile_card, pour ne pas geler la
boucle événementielle du bot pendant le dessin.
"""
import io
from pathlib import Path

import asyncio
import discord
from PIL import Image, ImageDraw, ImageFont

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
FONT_BOLD = str(FONTS_DIR / "DejaVuSans-Bold.ttf")
FONT_REGULAR = str(FONTS_DIR / "DejaVuSans.ttf")

WIDTH, HEIGHT = 900, 300
BG_COLOR = (24, 26, 32)
ACCENT_COLOR = (88, 101, 242)
BAR_BG_COLOR = (45, 48, 58)
TEXT_COLOR = (255, 255, 255)
SUBTEXT_COLOR = (170, 173, 182)

AVATAR_SIZE = 180
AVATAR_MARGIN = 40
TEXT_X = AVATAR_MARGIN + AVATAR_SIZE + 40
RIGHT_MARGIN = 50


def _xp_progress(xp: int) -> tuple[int, int, int]:
    """(niveau, xp_dans_le_niveau, xp_requis_pour_le_niveau) — même courbe que
    Profile.get_level (cogs/profile.py), mais expose aussi la progression dans
    le niveau courant, nécessaire pour la barre."""
    level = 1
    xp_needed = 10
    remaining = xp
    while remaining >= xp_needed:
        remaining -= xp_needed
        xp_needed *= 2
        level += 1
    return level, remaining, xp_needed


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    """Tronque `text` avec une ellipse si son rendu dépasse max_width (pseudos
    Discord jusqu'à 32 caractères, largeur de carte fixe)."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return f"{text}…" if text else "…"


def _circular_avatar(avatar_bytes: bytes) -> Image.Image:
    avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((AVATAR_SIZE, AVATAR_SIZE))
    mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, AVATAR_SIZE, AVATAR_SIZE), fill=255)
    avatar.putalpha(mask)
    return avatar


def _render(avatar_bytes: bytes, pseudo: str, argent: int, xp: int, rang: int | None) -> io.BytesIO:
    level, xp_in_level, xp_needed = _xp_progress(xp)
    progress_ratio = xp_in_level / xp_needed if xp_needed else 0

    img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    avatar_y = (HEIGHT - AVATAR_SIZE) // 2
    draw.ellipse(
        (AVATAR_MARGIN - 5, avatar_y - 5, AVATAR_MARGIN + AVATAR_SIZE + 5, avatar_y + AVATAR_SIZE + 5),
        fill=ACCENT_COLOR,
    )
    avatar_img = _circular_avatar(avatar_bytes)
    img.paste(avatar_img, (AVATAR_MARGIN, avatar_y), avatar_img)

    font_pseudo = ImageFont.truetype(FONT_BOLD, 40)
    font_rang = ImageFont.truetype(FONT_REGULAR, 22)
    font_niveau = ImageFont.truetype(FONT_BOLD, 28)
    font_stat_label = ImageFont.truetype(FONT_REGULAR, 18)
    font_stat_value = ImageFont.truetype(FONT_BOLD, 30)
    font_xp = ImageFont.truetype(FONT_REGULAR, 20)

    max_text_width = WIDTH - RIGHT_MARGIN - TEXT_X
    draw.text((TEXT_X, 38), _fit_text(draw, pseudo, font_pseudo, max_text_width), font=font_pseudo, fill=TEXT_COLOR)

    if rang is not None:
        draw.text((TEXT_X, 88), f"Rang #{rang} sur le serveur", font=font_rang, fill=SUBTEXT_COLOR)

    draw.text((TEXT_X, 130), f"Niveau {level}", font=font_niveau, fill=ACCENT_COLOR)

    xp_txt = f"{xp_in_level} / {xp_needed} XP"
    draw.text((WIDTH - RIGHT_MARGIN, 130), xp_txt, font=font_xp, fill=SUBTEXT_COLOR, anchor="rs")

    bar_x0, bar_y0 = TEXT_X, 172
    bar_x1, bar_y1 = WIDTH - RIGHT_MARGIN, 198
    draw.rounded_rectangle((bar_x0, bar_y0, bar_x1, bar_y1), radius=13, fill=BAR_BG_COLOR)
    fill_x1 = bar_x0 + int((bar_x1 - bar_x0) * progress_ratio)
    if fill_x1 > bar_x0:
        draw.rounded_rectangle((bar_x0, bar_y0, max(fill_x1, bar_x0 + 26), bar_y1), radius=13, fill=ACCENT_COLOR)

    draw.text((TEXT_X, 230), "ARGENT", font=font_stat_label, fill=SUBTEXT_COLOR)
    draw.text((TEXT_X, 252), f"{argent} €", font=font_stat_value, fill=TEXT_COLOR)

    draw.text((TEXT_X + 220, 230), "XP TOTAL", font=font_stat_label, fill=SUBTEXT_COLOR)
    draw.text((TEXT_X + 220, 252), str(xp), font=font_stat_value, fill=TEXT_COLOR)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


async def generate_profile_card(member: discord.Member | discord.User, argent: int, xp: int, rang: int | None) -> discord.File:
    """Point d'entrée async : lit l'avatar puis dessine la carte dans un thread
    séparé (Pillow est synchrone/CPU-bound)."""
    avatar_bytes = await member.display_avatar.replace(size=256, format="png").read()
    buffer = await asyncio.to_thread(_render, avatar_bytes, member.display_name, argent, xp, rang)
    return discord.File(buffer, filename="profil.png")
