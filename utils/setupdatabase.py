import warnings

import aiomysql
import pymysql
from dotenv import load_dotenv

load_dotenv()

TABLES = {
    "utilisateurs": [
        "user_id BIGINT PRIMARY KEY",
        "argent INT DEFAULT 0",
        "xp INT DEFAULT 0",
        "niveau INT DEFAULT 0",
        "nb_tickets_open INT DEFAULT 0",
        "warn INT DEFAULT 0",
        # Timestamp epoch du dernier /daily réclamé (voir cogs/profile.py) ; NULL =
        # jamais réclamé.
        "last_daily BIGINT",
        "commun INT DEFAULT 0",
        "rare INT DEFAULT 0",
        "epique INT DEFAULT 0",
        "mytique INT DEFAULT 0",
        "legendaire INT DEFAULT 0",
        "secret INT DEFAULT 0",
    ],

    "inventaire": [
        "user_id BIGINT NOT NULL",
        "item_id INT NOT NULL",
        "quantite INT NOT NULL DEFAULT 0",
        "PRIMARY KEY(user_id, item_id)"
    ],

    "shop": [
        # VARCHAR (et non TEXT) car cette colonne est la clé primaire : InnoDB a besoin
        # d'une longueur fixe pour indexer une clé.
        "name VARCHAR(100) PRIMARY KEY",
        "price INT NOT NULL",
        "type INT NOT NULL",
        # BIGINT : "valeur" peut contenir un id de rôle Discord (snowflake), qui dépasse
        # largement la plage d'un INT 32 bits.
        "valeur BIGINT NOT NULL",
        "duration INT"
    ],

    "temp_bans": [
        "user_id BIGINT",
        "unban_at BIGINT"
    ],

    "ticket": [
        "ticket_id INT PRIMARY KEY AUTO_INCREMENT",
        "thread_id BIGINT NOT NULL",
        "membre_id BIGINT NOT NULL",
        "modo_id BIGINT",
        "statut INT NOT NULL",
        "raison TEXT NOT NULL",
        "last_message BIGINT",
        # Nom conservé tel quel malgré le seuil désormais à 24h (voir ticket_watcher
        # dans start.py) pour éviter une migration de colonne pour un simple renommage :
        # sert juste de flag "avertissement d'inactivité déjà envoyé".
        "warn_12h INT",
        "closed_at BIGINT",
        "modo_message_id BIGINT",
        "message_ticket_id BIGINT",
        # Message MP envoyé au modérateur à la fermeture automatique (voir
        # ConfirmationClotureView dans cogs/tickets.py) : permet de relier sa réponse
        # (clic sur le select) au bon ticket sans dépendre d'un salon/thread.
        "mod_dm_message_id BIGINT",
        # Description/pub collectées dans le flux "ticket partenariat" (voir
        # ConditionsPartenariatView.accepter dans cogs/tickets.py), enregistrées ici
        # plutôt que gardées uniquement sur l'instance de MentionPartenariatView :
        # cette dernière est enregistrée globalement au démarrage (bot.add_view) pour
        # rester persistante, ce qui écraserait des attributs stockés sur l'instance.
        "partenariat_description TEXT",
        "partenariat_pub TEXT"
    ],
    "role_special": [
        "id INT NOT NULL PRIMARY KEY AUTO_INCREMENT",
        "user_id BIGINT NOT NULL",
        "role_id BIGINT",
        "status INT NOT NULL DEFAULT 0",
        "message_accepter_id BIGINT",
    ],

    "warns": [
        "id INT PRIMARY KEY AUTO_INCREMENT",
        "user_id BIGINT NOT NULL",
        "modo_id BIGINT NOT NULL",
        "raison TEXT",
        "created_at BIGINT",
        "created_at_iso VARCHAR(64)"
    ],

    "contestations": [
        "message_id BIGINT PRIMARY KEY",
        "membre_id BIGINT NOT NULL",
        "warn_id INT",
        "warn_raison TEXT",
        "warn_created_at BIGINT",
    ],

    "temp_roles": [
        # Fusion des anciennes tables role_temp (période de test staff) et
        # shop_temp_roles (rôle temporaire acheté en boutique), qui répondaient
        # au même besoin (rôle à retirer/traiter après end_time) avec un schéma
        # quasi identique. `origin` distingue les deux comportements attendus
        # (voir staff_test_watcher dans start.py et check_temp_roles dans
        # cogs/boutique.py) : 'staff_test' ou 'shop_purchase'.
        "id INT PRIMARY KEY AUTO_INCREMENT",
        "user_id BIGINT NOT NULL",
        "role_id BIGINT NOT NULL",
        "end_time BIGINT NOT NULL",
        "origin VARCHAR(32) NOT NULL",
    ],

    "profil_extra": [
        # Réponses libres du bouton "Personnaliser mon profil" (voir cogs/profile.py :
        # JEUX_PLATEFORMES). `cle` identifie la question (ex: 'psn', 'fortnite_niveau'),
        # une ligne par question répondue — évite une colonne par jeu/plateforme dans
        # `utilisateurs`, qui serait presque toujours NULL pour la plupart des membres.
        "user_id BIGINT NOT NULL",
        "cle VARCHAR(32) NOT NULL",
        "valeur VARCHAR(255) NOT NULL",
        "PRIMARY KEY(user_id, cle)"
    ],

    "error": [
        # Historique des erreurs CRITICAL (voir utils/error_handler.py) : alimenté
        # automatiquement, pour investigation a posteriori sans dépendre des logs.
        "id INT PRIMARY KEY AUTO_INCREMENT",
        "created_at BIGINT NOT NULL",
        "created_at_iso VARCHAR(64) NOT NULL",
        "level VARCHAR(16) NOT NULL",
        "source VARCHAR(128) NOT NULL",
        "message TEXT NOT NULL",
        "traceback TEXT",
    ],
}


async def init_db(pool: aiomysql.Pool):
    """Crée les tables manquantes et ajoute les colonnes manquantes sur celles qui existent déjà.
    Suppose que la base (DB_NAME) existe déjà sur le serveur MariaDB."""
    async with pool.acquire() as conn:
        async with conn.cursor() as c:
            for table, columns in TABLES.items():
                # 1️⃣ Création de la table (le "IF NOT EXISTS" fait émettre un warning
                # MariaDB anodin quand la table existe déjà : on le supprime volontairement).
                create_sql = f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(columns)})"
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", category=pymysql.Warning)
                    await c.execute(create_sql)

                # 2️⃣ Colonnes existantes
                await c.execute(
                    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                    (table,)
                )
                existing_columns = {row[0] for row in await c.fetchall()}

                # 3️⃣ Ajout des colonnes manquantes
                for col in columns:
                    if col.startswith("PRIMARY KEY"):
                        continue

                    col_name = col.split()[0]
                    if col_name not in existing_columns:
                        await c.execute(f"ALTER TABLE {table} ADD COLUMN {col}")

            # 4️⃣ Migration ponctuelle : d'anciennes tables (role_temp, shop_temp_roles)
            # ont été fusionnées dans temp_roles (voir ci-dessus). Sans effet une fois
            # la migration faite, puisque ces tables n'existent plus alors.
            await _migrate_legacy_temp_roles(c)

            # 5️⃣ Contrainte UNIQUE sur ticket.thread_id (voir _migrate_ticket_thread_unique).
            await _migrate_ticket_thread_unique(c)

            # 6️⃣ Contrainte UNIQUE sur role_special.user_id (voir _migrate_role_special_user_unique).
            await _migrate_role_special_user_unique(c)

        await conn.commit()


async def _migrate_legacy_temp_roles(c):
    """Copie les données de role_temp et shop_temp_roles (si elles existent encore)
    vers temp_roles, puis supprime les anciennes tables. Idempotent : ne fait rien
    une fois la migration effectuée (les tables n'existent alors plus)."""
    await c.execute(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ('role_temp', 'shop_temp_roles')"
    )
    legacy_tables = {row[0] for row in await c.fetchall()}

    if "role_temp" in legacy_tables:
        await c.execute(
            "INSERT INTO temp_roles (user_id, role_id, end_time, origin) "
            "SELECT user_id, role_id, end_time, 'staff_test' FROM role_temp"
        )
        await c.execute("DROP TABLE role_temp")

    if "shop_temp_roles" in legacy_tables:
        await c.execute(
            "INSERT INTO temp_roles (user_id, role_id, end_time, origin) "
            "SELECT user_id, role_id, end_time, 'shop_purchase' FROM shop_temp_roles"
        )
        await c.execute("DROP TABLE shop_temp_roles")


async def _migrate_ticket_thread_unique(c):
    """Ajoute une contrainte UNIQUE(thread_id) sur `ticket` si elle n'existe pas déjà.

    Sans cette contrainte, un doublon accidentel (ex: deux instances du bot lancées
    en même temps) fait traiter le même ticket deux fois par ticket_watcher —
    chaque ligne envoie son propre avertissement d'inactivité pour le même thread.
    Dédoublonne d'abord par sécurité (garde la ligne la plus récente par thread_id) :
    idempotent, sans effet une fois la contrainte posée."""
    await c.execute(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ticket' AND INDEX_NAME = 'thread_id_unique'"
    )
    (already_done,) = await c.fetchone()
    if already_done:
        return

    await c.execute("""
        DELETE t1 FROM ticket t1
        INNER JOIN ticket t2
        ON t1.thread_id = t2.thread_id AND t1.ticket_id < t2.ticket_id
    """)
    await c.execute("ALTER TABLE ticket ADD CONSTRAINT thread_id_unique UNIQUE (thread_id)")


async def _migrate_role_special_user_unique(c):
    """Ajoute une contrainte UNIQUE(user_id) sur `role_special` si elle n'existe pas déjà.

    Sans cette contrainte, la vérification "candidature déjà en cours" de
    ConditionsSelect.commencer (cogs/recrutement.py) — un simple SELECT fait bien
    avant l'INSERT, qui n'arrive que plusieurs minutes après (aller-retour MP +
    formulaire) — laisse une fenêtre où deux clics créent deux candidatures pour
    le même membre. Dédoublonne d'abord par sécurité (garde la ligne la plus
    récente par user_id) : idempotent, sans effet une fois la contrainte posée."""
    await c.execute(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'role_special' AND INDEX_NAME = 'user_id_unique'"
    )
    (already_done,) = await c.fetchone()
    if already_done:
        return

    await c.execute("""
        DELETE t1 FROM role_special t1
        INNER JOIN role_special t2
        ON t1.user_id = t2.user_id AND t1.id < t2.id
    """)
    await c.execute("ALTER TABLE role_special ADD CONSTRAINT user_id_unique UNIQUE (user_id)")
