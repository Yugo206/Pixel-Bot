# Pixel Bot

Pixel bot est un bot discord multigaming développé en **python** (discord.py) pour gerer : 
- Les systèmes de partenariat
- Les systeme de moderation
- Les systeme d'argent & d'XP
- Les tickets, recrutement et bien plus

## 1) Installation

Pour cloner le repo :

```bash
git clone https://github.com/Yugo206/Pixel-Bot.git
```
*Astuce : Pensez a cloner la branche `main`car elle est la version stable. Pour 
les version beta, clonez la branche `beta`*

## 2) Installer les dépendances

Pour installer les dépendances :

```bash
pip install -r requirements.txt
```
## 3) Ajout du .env

Dans le meme dossier que `start.py`, créer un nouveau fichier `.env` et y écrire ceci :
(remplace les crochet par les ids de votre serveur)

```.env
DISCORD_TOKEN=[votre token discord]
CHANNEL_MODO_ID=[l'id de votre salon moderateur]
ROLE_MODO_ID=[l'id de votre role moderateur]
GUILD_ID=[l'id de votre serveur]
CHANNEL_TRADE_ID=[l'id de votre salon de trade brainrot]
CHANNEL_COMMANDE_ID=[l'id de votre salon de commande]
CHEMIN_DB=[le chemin local où vous voulez que la base de donnée soit]
CHANNEL_RECRUTEMENT=[l'identifiant du salon de recrutement]
```
*Astuce : Pensez a avoir activé le mode developpeur dans les parametres de discord pour obtenir les identifiants*

## 4) Terminé !

*Astuce : Pensez a faire git pull sur la branche `main` pour obtenir les nouvelles fonctionnalités 
et les corrections de bug. Pour les version bêta, il faut pull la branche `beta`*
