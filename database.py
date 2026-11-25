import sqlite3
import random
import threading
import functools
import time
from contextlib import contextmanager

import config
from config import ADMIN_IDS  # re-exported for main.py

# ==================== PLACEHOLDER SEED DATA ====================
# Only inserted into EMPTY tables on first start, so every command works out
# of the box. Replace these entries in the dashboard.

SEED_SHOP_ITEMS = [
    # (item_name, price, description, category, currency_type)
    ("Sample Item", 1000, "Placeholder - replace in the dashboard", "Miscellaneous", "main"),
    ("Sample Premium Item", 5, "Placeholder - replace in the dashboard", "Cosmetics", "premium"),
]

SEED_LOCATIONS = ["Arms Factory", "Lakeside Village"]

# (item_name, message, category, sell_value, weight, difficulty); item_name None = nothing found
SEED_LOOT = {
    "scavenging": [
        ("Sample Scrap", "[Placeholder] You spot something useful among the debris.", "Miscellaneous", 100, 10, 10),
        (None, "[Placeholder] You searched everywhere but found nothing of use.", None, 0, 10, 10),
    ],
    "foraging": [
        ("Sample Herb", "[Placeholder] You spot a plant worth gathering.", "Consumables", 100, 10, 10),
        (None, "[Placeholder] You searched the undergrowth but found nothing worth taking.", None, 0, 10, 10),
    ],
}


# Starter texts for /cnmatch (one per result type, for both maps). Placeholders:
# {character}, {map}, {anomaly}, {count}
SEED_CN_MESSAGES = [
    ("barely", "{character} scraped together just enough to meet the quota and stumbled out of {map} "
               "with {count} classic(s), the {anomaly} close behind."),
    ("normal", "{character} met the quota in {map}, kept clear of the {anomaly} and left with {count} classics."),
    ("great", "{character} cleared {map} and walked out with a heavy bag of {count} classics. "
              "The {anomaly} never stood a chance of catching up."),
    ("death_anomaly", "The {anomaly} caught {character} in {map}."),
    ("death_other", "{character} didn't make it out of {map}. The quota wasn't met in time."),
]


def _seed_match_messages():
    """One placeholder message per role and result level."""
    return [(role, level, f"[Placeholder] {role} result {level}/4 - edit this message in the dashboard.")
            for role in config.ROLES for level in range(5)]


def _serialize_writes(func):
    """Run a write method while holding the instance write lock.

    Prevents two writes from the same process reaching SQLite at the same time
    ('database is locked'). RLock, so decorated methods may call each other.
    """
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        with self._write_lock:
            return func(self, *args, **kwargs)
    return wrapper


class Database:
    def __init__(self, db_name=None):
        self.db_name = db_name or config.DB_FILE
        self._write_lock = threading.RLock()
        self.init_database()
        self.populate_initial_data()

    # ==================== CONNECTION HELPERS ====================

    def get_connection(self):
        conn = sqlite3.connect(self.db_name, timeout=15)
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        # Python lower() so case-insensitive matching also works for non-ASCII names
        conn.create_function("LOWER", 1, lambda x: x.lower() if x else None, deterministic=True)
        return conn

    @contextmanager
    def _conn(self):
        """Connection that is always closed. Uncommitted changes are discarded."""
        conn = self.get_connection()
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _profile_name(cursor, name):
        """Return the exact stored name of a profile (case-insensitive lookup), or None.

        Always write the stored name into other tables: the inventory foreign key
        is case-sensitive, so 'norton' would fail where 'Norton' is stored.
        """
        cursor.execute("SELECT name FROM profiles WHERE LOWER(name) = LOWER(?)", (name,))
        row = cursor.fetchone()
        return row[0] if row else None

    @staticmethod
    def _currency_column(key):
        if key not in config.CURRENCY_COLUMNS:
            raise ValueError(f"Unknown currency: {key}")
        return config.CURRENCY_COLUMNS[key]

    @staticmethod
    def currency_name(key):
        return config.CURRENCIES[key]["name"]

    @staticmethod
    def loot_table(game):
        if game not in config.MINIGAMES:
            raise ValueError(f"Unknown minigame: {game}")
        return f"{game}_items"

    def _item_tables(self):
        """Tables that define items, in priority order for display name/category."""
        return ["shop_items"] + [self.loot_table(g) for g in config.MINIGAMES] + ["cn_items"]

    def _sellable_tables(self):
        """Tables whose items can be sold with /sell (minigame loot and Cryptic Notes classics)."""
        return [self.loot_table(g) for g in config.MINIGAMES] + ["cn_items"]

    # ==================== TEXT NORMALISATION ====================

    def normalise(self, text):
        """Capitalise the first letter of each word and keep the rest as typed.

        'rusty key' -> 'Rusty Key', "engineer's kit" -> "Engineer's Kit",
        'McAllister' stays 'McAllister' (str.title() would make it 'Mcallister').
        """
        if not text:
            return text
        return " ".join(word[:1].upper() + word[1:] for word in text.split())

    def normalise_category(self, category):
        """Fold a category onto config.ITEM_CATEGORIES (case/whitespace-insensitive)."""
        if not category:
            return 'Miscellaneous'
        cleaned = category.strip()
        for known in config.ITEM_CATEGORIES:
            if cleaned.lower() == known.lower():
                return known
        return cleaned.title()

    # ==================== SCHEMA ====================

    @staticmethod
    def _ensure_columns(cursor, table, columns):
        """Add any missing columns (lets new minigames be added via config)."""
        cursor.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in cursor.fetchall()}
        for col, decl in columns.items():
            if col not in existing:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                print(f"✓ Added {col} column to {table} table")

    def init_database(self):
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")  # persistent, only needs setting once

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    role TEXT NOT NULL,
                    currency_main INTEGER DEFAULT 0,
                    currency_premium INTEGER DEFAULT 0,
                    user_id TEXT
                )''')
            level_columns = {}
            for game in config.MINIGAMES:
                level_columns[f"{game}_level"] = "INTEGER DEFAULT 1"
                level_columns[f"{game}_xp"] = "INTEGER DEFAULT 0"
            self._ensure_columns(cursor, "profiles", level_columns)

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS inventory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    character_name TEXT NOT NULL,
                    item_name TEXT NOT NULL,
                    FOREIGN KEY (character_name) REFERENCES profiles(name) ON DELETE CASCADE
                )''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS shop_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_name TEXT UNIQUE NOT NULL,
                    price INTEGER NOT NULL,
                    description TEXT,
                    category TEXT DEFAULT 'Miscellaneous',
                    currency_type TEXT DEFAULT 'main'
                )''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS match_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    performance_level INTEGER NOT NULL,
                    message TEXT NOT NULL
                )''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS locations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL
                )''')

            for game in config.MINIGAMES:
                cursor.execute(f'''
                    CREATE TABLE IF NOT EXISTS {self.loot_table(game)} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        item_name TEXT,
                        message TEXT NOT NULL,
                        description TEXT,
                        category TEXT DEFAULT 'Miscellaneous',
                        sell_value INTEGER DEFAULT 0,
                        weight INTEGER DEFAULT 10,
                        difficulty INTEGER DEFAULT 10,
                        flee_message TEXT,
                        fail_message TEXT
                    )''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    message_id TEXT,
                    user_id TEXT NOT NULL,
                    title TEXT,
                    description TEXT,
                    color TEXT DEFAULT '000000',
                    footer_text TEXT,
                    image_url TEXT,
                    thumbnail_url TEXT,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    notified INTEGER DEFAULT 0,
                    active INTEGER DEFAULT 1,
                    status TEXT DEFAULT 'active',      -- pending / active / denied / expired / completed / cancelled
                    roster_id INTEGER,                 -- set for roster character reservations
                    request_message_id TEXT,           -- request message in the staff channel
                    reminder_message_id TEXT,          -- "ends soon" message with the Extend button
                    reminded INTEGER DEFAULT 0,
                    extension_pending INTEGER DEFAULT 0,
                    note TEXT,
                    player_name TEXT,
                    decided_by TEXT
                )''')

            # Character roster: who plays which character.
            # Databases created by an early version allowed each name only once (UNIQUE), which
            # crashed adding "Norton Campbell | Fool's Gold" next to "Norton Campbell | Prospector".
            # Rebuild such a table without that rule; all entries are kept.
            cursor.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'roster'")
            old_roster = cursor.fetchone()
            rebuild_roster = bool(old_roster and 'UNIQUE' in old_roster[0].upper())
            if rebuild_roster:
                # One transaction (committed at the end of init_database), so the bot and the
                # dashboard starting at the same time can't interfere with each other
                cursor.execute("BEGIN IMMEDIATE")
                cursor.execute("ALTER TABLE roster RENAME TO roster_old")
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS roster (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,              -- same name allowed with a different title
                    title TEXT,                      -- (e.g. Norton Campbell: Prospector / Fool's Gold)
                    role TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'free',   -- free / reserved / taken
                    player_id TEXT,
                    player_name TEXT
                )''')
            # Identity switches: two linked entries always share status and player
            self._ensure_columns(cursor, "roster", {"linked_id": "INTEGER"})
            if rebuild_roster:
                old_columns = {c[1] for c in cursor.execute("PRAGMA table_info(roster_old)").fetchall()}
                columns = ", ".join(c for c in ("id", "name", "title", "role", "status", "player_id",
                                                "player_name", "linked_id") if c in old_columns)
                cursor.execute(f"INSERT INTO roster ({columns}) SELECT {columns} FROM roster_old")
                cursor.execute("DROP TABLE roster_old")
                print("✓ Roster table upgraded: the same name may now be used with different titles")

            # ----- Cryptic Notes (/cnmatch) -----
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cn_maps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    other_death_chance INTEGER DEFAULT 8   -- % for quota not met, falls, ...
                )''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cn_anomalies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    danger INTEGER DEFAULT 2,              -- 1 (weak) .. 5 (deadliest)
                    description TEXT
                )''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cn_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_name TEXT NOT NULL,
                    tier TEXT NOT NULL DEFAULT 'B',        -- key of config.CN_TIERS
                    sell_value INTEGER DEFAULT 0,
                    weight INTEGER DEFAULT 10,             -- how common within its tier
                    category TEXT DEFAULT 'Collectibles',
                    description TEXT
                )''')
            # Which anomaly / classic can appear on which map
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cn_spawns (
                    kind TEXT NOT NULL,                    -- 'anomaly' or 'item'
                    entry_id INTEGER NOT NULL,
                    map_id INTEGER NOT NULL,
                    PRIMARY KEY (kind, entry_id, map_id)
                )''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cn_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    map_id INTEGER,                        -- NULL = both maps
                    outcome TEXT NOT NULL,                 -- key of config.CN_OUTCOMES
                    message TEXT NOT NULL
                )''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cn_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_name TEXT NOT NULL,
                    map_id INTEGER,
                    outcome TEXT,
                    created_at INTEGER NOT NULL
                )''')

            # Simple key/value settings edited on the dashboard (e.g. the roster layout)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )''')

            # Old: roster messages of the single roster post (only kept to clean them up once)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS roster_messages (
                    position INTEGER PRIMARY KEY,
                    message_id TEXT NOT NULL
                )''')
            # Roster messages per role (Hunters and Survivors each have their own forum post), in posting order
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS roster_post_messages (
                    role TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    message_id TEXT NOT NULL,
                    PRIMARY KEY (role, position)
                )''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS activity_characters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    character_name TEXT NOT NULL,
                    role TEXT,
                    discord_username TEXT,
                    discord_user_id TEXT,
                    added_at INTEGER NOT NULL,
                    removed INTEGER DEFAULT 0
                )''')
            # Entries with a roster_id come from the roster automatically; the others are OCs
            self._ensure_columns(cursor, "activity_characters", {"roster_id": "INTEGER"})

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS activity_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    character_id INTEGER NOT NULL,
                    month TEXT NOT NULL,
                    status TEXT NOT NULL,
                    checked_at INTEGER NOT NULL,
                    FOREIGN KEY (character_id) REFERENCES activity_characters(id) ON DELETE CASCADE,
                    UNIQUE(character_id, month)
                )''')

            # Embeds, welcome message and reaction roles (managed via the dashboard).
            # The old code never created these, so a fresh install crashed without them.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS embeds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    title TEXT,
                    description TEXT,
                    color TEXT DEFAULT '000000',
                    footer_text TEXT,
                    image_url TEXT,
                    thumbnail_url TEXT,
                    channel_id TEXT,
                    message_id TEXT
                )''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS welcome_settings (
                    id INTEGER PRIMARY KEY,
                    enabled INTEGER DEFAULT 0,
                    embed_id INTEGER,
                    channel_id TEXT
                )''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS reaction_roles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL,
                    emoji TEXT NOT NULL,
                    role_id TEXT NOT NULL,
                    UNIQUE(message_id, emoji)
                )''')

            conn.commit()

    def populate_initial_data(self):
        """Fill empty tables with the placeholder content above."""
        with self._conn() as conn:
            cursor = conn.cursor()

            def is_empty(table):
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                return cursor.fetchone()[0] == 0

            if is_empty("shop_items"):
                cursor.executemany(
                    "INSERT INTO shop_items (item_name, price, description, category, currency_type) VALUES (?, ?, ?, ?, ?)",
                    SEED_SHOP_ITEMS)
            if is_empty("match_messages"):
                cursor.executemany(
                    "INSERT INTO match_messages (role, performance_level, message) VALUES (?, ?, ?)",
                    _seed_match_messages())
            if is_empty("locations"):
                cursor.executemany("INSERT INTO locations (name) VALUES (?)", [(n,) for n in SEED_LOCATIONS])
            for game, rows in SEED_LOOT.items():
                if game in config.MINIGAMES and is_empty(self.loot_table(game)):
                    cursor.executemany(
                        f"INSERT INTO {self.loot_table(game)} (item_name, message, category, sell_value, weight, difficulty) "
                        f"VALUES (?, ?, ?, ?, ?, ?)", rows)

            # Cryptic Notes: maps from config (new ones are added, existing settings are kept)
            for map_name, death_chance in config.CN_MAPS.items():
                cursor.execute("INSERT OR IGNORE INTO cn_maps (name, other_death_chance) VALUES (?, ?)",
                               (map_name, death_chance))
            if is_empty("cn_messages"):
                cursor.executemany("INSERT INTO cn_messages (map_id, outcome, message) VALUES (NULL, ?, ?)",
                                   SEED_CN_MESSAGES)
            if is_empty("cn_anomalies") and is_empty("cn_items"):
                self._seed_cn_examples(cursor)
            conn.commit()

    @staticmethod
    def _seed_cn_examples(cursor):
        """The three examples from the server's codex, so the codex isn't empty on first start."""
        maps = {name: map_id for map_id, name in cursor.execute("SELECT id, name FROM cn_maps").fetchall()}
        abod, pd = maps.get("A Bringer of Doom"), maps.get("Phosphorescence Disease")
        cursor.execute("INSERT INTO cn_anomalies (name, danger) VALUES ('Wraith', 3)")
        if abod:
            cursor.execute("INSERT INTO cn_spawns VALUES ('anomaly', ?, ?)", (cursor.lastrowid, abod))
        cursor.execute("INSERT INTO cn_items (item_name, tier, sell_value) VALUES ('Pickaxe', 'B', 150)")
        if pd:
            cursor.execute("INSERT INTO cn_spawns VALUES ('item', ?, ?)", (cursor.lastrowid, pd))
        cursor.execute("INSERT INTO cn_items (item_name, tier, sell_value) VALUES ('Cursed Sapphire', 'S', 1000)")
        sapphire = cursor.lastrowid
        for map_id in (abod, pd):
            if map_id:
                cursor.execute("INSERT INTO cn_spawns VALUES ('item', ?, ?)", (sapphire, map_id))

    # ==================== PROFILES ====================

    @_serialize_writes
    def create_profile(self, name, role, user_id):
        name = name.strip()
        with self._conn() as conn:
            cursor = conn.cursor()
            existing = self._profile_name(cursor, name)
            if existing:
                return False, f"Profile {existing} already exists!"
            try:
                cursor.execute("INSERT INTO profiles (name, role, user_id) VALUES (?, ?, ?)",
                               (name, role, str(user_id)))
                conn.commit()
                return True, f"Profile created for {name}"
            except sqlite3.IntegrityError:
                return False, f"Profile {name} already exists!"
            except Exception as e:
                return False, f"Error: {e}"

    def check_ownership(self, name, user_id):
        """Returns (is_owner, message). Admins and unowned (legacy) characters pass."""
        try:
            if int(user_id) in ADMIN_IDS:
                return True, "Admin access"
            with self._conn() as conn:
                result = conn.execute("SELECT user_id FROM profiles WHERE LOWER(name) = LOWER(?)", (name,)).fetchone()
            if not result:
                return False, f"Character **{name}** not found!"
            owner_id = result[0]
            if owner_id is None:
                return True, "Legacy character (no owner)"
            if str(owner_id) == str(user_id):
                return True, "You own this character"
            return False, f"❌ You don't own **{name}**! Only the creator can edit this character."
        except Exception as e:
            return False, f"Error: {e}"

    @_serialize_writes
    def claim_character(self, name, user_id):
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name, user_id FROM profiles WHERE LOWER(name) = LOWER(?)", (name,))
            result = cursor.fetchone()
            if not result:
                return False, f"Character **{name}** not found!"
            if result[1] is not None:
                return False, f"**{result[0]}** is already owned by someone!"
            cursor.execute("UPDATE profiles SET user_id = ? WHERE name = ?", (str(user_id), result[0]))
            conn.commit()
            return True, f"✅ You now own **{result[0]}**!"

    @_serialize_writes
    def assign_owner(self, character_name, new_owner_id):
        with self._conn() as conn:
            cursor = conn.cursor()
            stored = self._profile_name(cursor, character_name)
            if not stored:
                return False, f"Character **{character_name}** not found!"
            cursor.execute("UPDATE profiles SET user_id = ? WHERE name = ?", (str(new_owner_id), stored))
            conn.commit()
            return True, f"✅ Assigned **{stored}** to user ID {new_owner_id}"

    @_serialize_writes
    def edit_profile_name(self, old_name, new_name):
        new_name = new_name.strip()  # kept exactly as typed, like /create
        if not new_name:
            return False, "The new name can't be empty!"
        with self._conn() as conn:
            cursor = conn.cursor()
            stored = self._profile_name(cursor, old_name)
            if not stored:
                return False, f"Profile {old_name} not found!"
            cursor.execute("SELECT 1 FROM profiles WHERE LOWER(name) = LOWER(?) AND name != ?", (new_name, stored))
            if cursor.fetchone():
                return False, f"Profile {new_name} already exists!"
            try:
                # FK enforcement off while inventory still points at the old name.
                # Only works outside a transaction, which is the case here (only SELECTs so far).
                cursor.execute("PRAGMA foreign_keys=OFF")
                cursor.execute("UPDATE profiles SET name = ? WHERE name = ?", (new_name, stored))
                cursor.execute("UPDATE inventory SET character_name = ? WHERE character_name = ?", (new_name, stored))
                conn.commit()
                cursor.execute("PRAGMA foreign_keys=ON")
                return True, f"Profile renamed to {new_name}"
            except Exception as e:
                return False, f"Error: {e}"

    @_serialize_writes
    def edit_profile_role(self, name, new_role):
        with self._conn() as conn:
            cursor = conn.cursor()
            stored = self._profile_name(cursor, name)
            if not stored:
                return False, f"Profile {name} not found!"
            cursor.execute("UPDATE profiles SET role = ? WHERE name = ?", (new_role, stored))
            conn.commit()
            return True, f"Role updated to {new_role}"

    @_serialize_writes
    def delete_profile(self, name):
        with self._conn() as conn:
            cursor = conn.cursor()
            stored = self._profile_name(cursor, name)
            if not stored:
                return False, f"Profile {name} not found!"
            cursor.execute("DELETE FROM inventory WHERE character_name = ?", (stored,))
            cursor.execute("DELETE FROM profiles WHERE name = ?", (stored,))
            conn.commit()
            return True, f"Profile {stored} deleted"

    def get_profile(self, name):
        """Profile as a dict, with extra 'main' / 'premium' keys for the currencies."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM profiles WHERE LOWER(name) = LOWER(?)", (name,)).fetchone()
        if not row:
            return None
        profile = dict(row)
        for key, column in config.CURRENCY_COLUMNS.items():
            profile[key] = profile.get(column) or 0
        for game in config.MINIGAMES:
            profile[f"{game}_level"] = profile.get(f"{game}_level") or 1
            profile[f"{game}_xp"] = profile.get(f"{game}_xp") or 0
        return profile

    def search_profile_names(self, query, limit=25):
        """Character names containing `query` (for autocomplete of other players' characters)."""
        with self._conn() as conn:
            rows = conn.execute("SELECT name FROM profiles WHERE LOWER(name) LIKE ? ORDER BY name COLLATE NOCASE "
                                "LIMIT ?", (f"%{(query or '').strip().lower()}%", limit)).fetchall()
        return [r[0] for r in rows]

    def get_profiles_for_user(self, user_id):
        """[(name, role, main, premium)] for one Discord user, sorted by name."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT name, role, currency_main, currency_premium FROM profiles "
                "WHERE user_id = ? ORDER BY name COLLATE NOCASE", (str(user_id),)).fetchall()

    # ==================== CURRENCY ====================

    @_serialize_writes
    def add_currency(self, name, currency, amount):
        column = self._currency_column(currency)
        if amount <= 0:
            return False, "Amount must be greater than 0!"
        with self._conn() as conn:
            cursor = conn.cursor()
            stored = self._profile_name(cursor, name)
            if not stored:
                return False, f"Profile {name} not found!"
            cursor.execute(f"UPDATE profiles SET {column} = {column} + ? WHERE name = ?", (amount, stored))
            conn.commit()
            return True, f"Added {amount:,} {self.currency_name(currency)}"

    @_serialize_writes
    def remove_currency(self, name, currency, amount):
        column = self._currency_column(currency)
        if amount <= 0:
            return False, "Amount must be greater than 0!"
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT name, {column} FROM profiles WHERE LOWER(name) = LOWER(?)", (name,))
            result = cursor.fetchone()
            if not result:
                return False, f"Profile {name} not found!"
            stored, balance = result
            if balance < amount:
                return False, f"Insufficient {self.currency_name(currency)}! Current: {balance:,}"
            cursor.execute(f"UPDATE profiles SET {column} = {column} - ? WHERE name = ?", (amount, stored))
            conn.commit()
            return True, f"Removed {amount:,} {self.currency_name(currency)}"

    @_serialize_writes
    def add_rewards(self, name, main_amount, premium_amount):
        """Add both currencies in one transaction."""
        with self._conn() as conn:
            cursor = conn.cursor()
            stored = self._profile_name(cursor, name)
            if not stored:
                return False, f"Profile {name} not found!"
            cursor.execute(
                "UPDATE profiles SET currency_main = currency_main + ?, currency_premium = currency_premium + ? "
                "WHERE name = ?", (main_amount, premium_amount, stored))
            conn.commit()
            return True, "Rewards added"

    @_serialize_writes
    def transfer_currency(self, sender, receiver, currency, amount):
        """Move currency between two characters in one transaction."""
        column = self._currency_column(currency)
        if amount <= 0:
            return False, "Amount must be greater than 0!"
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT name, {column} FROM profiles WHERE LOWER(name) = LOWER(?)", (sender,))
            sender_row = cursor.fetchone()
            if not sender_row:
                return False, f"Profile {sender} not found!"
            receiver_stored = self._profile_name(cursor, receiver)
            if not receiver_stored:
                return False, f"Receiver character **{receiver}** not found!"
            sender_stored, balance = sender_row
            if balance < amount:
                return False, f"Insufficient {self.currency_name(currency)}! Current: {balance:,}"
            cursor.execute(f"UPDATE profiles SET {column} = {column} - ? WHERE name = ?", (amount, sender_stored))
            cursor.execute(f"UPDATE profiles SET {column} = {column} + ? WHERE name = ?", (amount, receiver_stored))
            conn.commit()
            return True, "Transferred"

    @_serialize_writes
    def exchange_currency(self, name, from_currency, amount):
        """Exchange main <-> premium. Returns (success, message, received, to_currency)."""
        if from_currency not in config.CURRENCY_COLUMNS:
            return False, "Invalid currency type!", 0, None
        if amount <= 0:
            return False, "Amount must be greater than 0!", 0, None

        to_currency = 'premium' if from_currency == 'main' else 'main'
        if from_currency == 'main':
            rate = config.MAIN_TO_PREMIUM_RATE
            received = amount // rate
            cost = received * rate
        else:
            rate = config.PREMIUM_TO_MAIN_RATE
            received = amount * rate
            cost = amount

        from_name, to_name = self.currency_name(from_currency), self.currency_name(to_currency)
        if received <= 0:
            return False, f"You need at least {rate:,} {from_name} to exchange for 1 {to_name}!", 0, None

        from_col, to_col = self._currency_column(from_currency), self._currency_column(to_currency)
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT name, {from_col} FROM profiles WHERE LOWER(name) = LOWER(?)", (name,))
            result = cursor.fetchone()
            if not result:
                return False, f"Profile {name} not found!", 0, None
            stored, balance = result
            if balance < cost:
                return False, f"Insufficient {from_name}! You need {cost:,}, you have {balance:,}.", 0, None
            cursor.execute(
                f"UPDATE profiles SET {from_col} = {from_col} - ?, {to_col} = {to_col} + ? WHERE name = ?",
                (cost, received, stored))
            conn.commit()
            return True, f"Exchanged {cost:,} {from_name} for {received:,} {to_name}", received, to_currency

    # ==================== INVENTORY ====================

    def _item_catalog(self, cursor):
        """{normalised key: (display name, category)} from all item definition tables."""
        catalog = {}
        for table in self._item_tables():
            cursor.execute(f"SELECT item_name, category FROM {table} WHERE item_name IS NOT NULL")
            for item_name, category in cursor.fetchall():
                key = item_name.strip().lower()
                if key and key not in catalog:
                    catalog[key] = (item_name.strip(), category)
        return catalog

    def _sell_value(self, cursor, item_name):
        """Sell value from the first loot table that gives the item a value (0 = not sellable)."""
        for table in self._sellable_tables():
            cursor.execute(
                f"SELECT sell_value FROM {table} "
                f"WHERE LOWER(TRIM(item_name)) = LOWER(TRIM(?)) AND sell_value > 0 LIMIT 1", (item_name,))
            result = cursor.fetchone()
            if result:
                return result[0]
        return 0

    @staticmethod
    def _count_item(cursor, character, item_name):
        cursor.execute(
            "SELECT COUNT(*) FROM inventory WHERE character_name = ? AND LOWER(TRIM(item_name)) = LOWER(TRIM(?))",
            (character, item_name))
        return cursor.fetchone()[0]

    @staticmethod
    def _delete_items(cursor, character, item_name, quantity):
        cursor.execute(
            "DELETE FROM inventory WHERE id IN (SELECT id FROM inventory WHERE character_name = ? "
            "AND LOWER(TRIM(item_name)) = LOWER(TRIM(?)) LIMIT ?)", (character, item_name, quantity))

    @staticmethod
    def _insert_items(cursor, character, item_name, quantity):
        cursor.executemany("INSERT INTO inventory (character_name, item_name) VALUES (?, ?)",
                           [(character, item_name)] * quantity)

    def get_inventory(self, name):
        """[{'item_name', 'quantity'}] sorted A–Z. Spelling variants (medkit / Medkit) count as one item."""
        with self._conn() as conn:
            cursor = conn.cursor()
            # Group case/whitespace-insensitively so spelling variants count as one item
            cursor.execute("""
                SELECT LOWER(TRIM(item_name)) AS item_key, MIN(TRIM(item_name)), COUNT(*)
                FROM inventory WHERE LOWER(character_name) = LOWER(?)
                GROUP BY item_key""", (name,))
            rows = cursor.fetchall()
            catalog = self._item_catalog(cursor)

        items = [{'item_name': catalog.get(item_key, (self.normalise(raw_name), None))[0], 'quantity': quantity}
                 for item_key, raw_name, quantity in rows]
        items.sort(key=lambda i: i['item_name'].lower())
        return items

    @_serialize_writes
    def add_items_with_quantity(self, name, item_quantities):
        """Returns (success, message, items_added)."""
        with self._conn() as conn:
            cursor = conn.cursor()
            stored = self._profile_name(cursor, name)
            if not stored:
                return False, f"Profile {name} not found!", {}
            try:
                for item_name, quantity in item_quantities.items():
                    self._insert_items(cursor, stored, item_name, quantity)
                conn.commit()
                return True, "Items added successfully", dict(item_quantities)
            except Exception as e:
                return False, f"Error: {e}", {}

    @_serialize_writes
    def remove_items_with_quantity(self, name, item_quantities, verb="remove"):
        """Remove (or use) items; all-or-nothing. Returns (success, message, items_removed)."""
        with self._conn() as conn:
            cursor = conn.cursor()
            stored = self._profile_name(cursor, name)
            if not stored:
                return False, f"Profile {name} not found!", {}
            for item_name, quantity in item_quantities.items():
                count = self._count_item(cursor, stored, item_name)
                if count < quantity:
                    return False, (f"Not enough **{item_name}** in inventory! "
                                   f"You have {count}, trying to {verb} {quantity}."), {}
            for item_name, quantity in item_quantities.items():
                self._delete_items(cursor, stored, item_name, quantity)
            conn.commit()
            return True, f"Items {verb}d successfully", dict(item_quantities)

    def use_items_with_quantity(self, name, item_quantities):
        return self.remove_items_with_quantity(name, item_quantities, verb="use")

    @_serialize_writes
    def rename_inventory_item(self, old_name, new_name):
        """Rename an item in every character's inventory. Returns how many copies were renamed."""
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE inventory SET item_name = ? WHERE LOWER(TRIM(item_name)) = LOWER(TRIM(?))",
                           (new_name.strip(), old_name))
            conn.commit()
            return cursor.rowcount

    @_serialize_writes
    def transfer_items(self, sender, receiver, item_quantities):
        """Move items between characters in one transaction (keeps the exact item names).

        Returns (success, message, items_moved).
        """
        with self._conn() as conn:
            cursor = conn.cursor()
            sender_stored = self._profile_name(cursor, sender)
            if not sender_stored:
                return False, f"Profile {sender} not found!", {}
            receiver_stored = self._profile_name(cursor, receiver)
            if not receiver_stored:
                return False, f"Receiver character **{receiver}** not found!", {}
            for item_name, quantity in item_quantities.items():
                count = self._count_item(cursor, sender_stored, item_name)
                if count < quantity:
                    return False, (f"Not enough **{item_name}** in inventory! "
                                   f"You have {count}, trying to give {quantity}."), {}
            moved = {}
            for item_name, quantity in item_quantities.items():
                cursor.execute(
                    "SELECT TRIM(item_name) FROM inventory WHERE character_name = ? "
                    "AND LOWER(TRIM(item_name)) = LOWER(TRIM(?)) LIMIT 1", (sender_stored, item_name))
                display = cursor.fetchone()[0]
                cursor.execute(
                    "UPDATE inventory SET character_name = ? WHERE id IN (SELECT id FROM inventory "
                    "WHERE character_name = ? AND LOWER(TRIM(item_name)) = LOWER(TRIM(?)) LIMIT ?)",
                    (receiver_stored, sender_stored, item_name, quantity))
                moved[display] = quantity
            conn.commit()
            return True, "Items transferred", moved

    # ==================== SHOP ====================

    def get_shop_items(self):
        """[(item_name, price, description, currency_type)] sorted by price."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT item_name, price, description, currency_type FROM shop_items ORDER BY price").fetchall()

    @_serialize_writes
    def buy_items_with_quantity(self, name, item_quantities):
        """Buy items, e.g. {"Oil Lantern": 2}. All items must use the same currency.

        Returns (success, message, total_price, currency_key, items_purchased).
        """
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name, currency_main, currency_premium FROM profiles WHERE LOWER(name) = LOWER(?)", (name,))
            result = cursor.fetchone()
            if not result:
                return False, f"Profile {name} not found!", 0, None, {}
            stored, balances = result[0], {"main": result[1], "premium": result[2]}

            total_cost, currency, purchases = 0, None, {}
            for item_name, quantity in item_quantities.items():
                cursor.execute(
                    "SELECT item_name, price, currency_type FROM shop_items "
                    "WHERE LOWER(TRIM(item_name)) = LOWER(TRIM(?))", (item_name,))
                row = cursor.fetchone()
                if not row:
                    return False, f"Item **{item_name}** not found in shop!", 0, None, {}
                actual_name, price, item_currency = row
                item_currency = item_currency if item_currency in config.CURRENCY_COLUMNS else 'main'
                if currency is None:
                    currency = item_currency
                elif currency != item_currency:
                    names = " or ".join(c["name"] for c in config.CURRENCIES.values())
                    return False, f"Cannot mix currencies! All items must use the same currency ({names}).", 0, None, {}
                purchases[actual_name] = purchases.get(actual_name, 0) + quantity
                total_cost += price * quantity

            if balances[currency] < total_cost:
                return (False, f"Insufficient {self.currency_name(currency)}! Total cost is **{total_cost:,}**, "
                               f"you have **{balances[currency]:,}**.", total_cost, currency, {})

            column = self._currency_column(currency)
            try:
                cursor.execute(f"UPDATE profiles SET {column} = {column} - ? WHERE name = ?", (total_cost, stored))
                for item_name, quantity in purchases.items():
                    self._insert_items(cursor, stored, item_name, quantity)
                conn.commit()
                return True, "Purchase successful", total_cost, currency, purchases
            except Exception as e:
                return False, f"Error: {e}", 0, None, {}

    # ==================== SELLING ====================

    def get_item_values(self, name, item_names):
        """Returns (success, message, items_with_values, total_value)."""
        with self._conn() as conn:
            cursor = conn.cursor()
            stored = self._profile_name(cursor, name)
            if not stored:
                return False, f"Profile **{name}** not found!", [], 0
            catalog = self._item_catalog(cursor)
            items_info, total_value, not_found = [], 0, []
            for item_name in item_names:
                quantity = self._count_item(cursor, stored, item_name)
                if quantity == 0:
                    not_found.append(item_name)
                    continue
                sell_value = self._sell_value(cursor, item_name)
                display = catalog.get(item_name.strip().lower(), (item_name,))[0]
                items_info.append({'name': display, 'quantity': quantity,
                                   'sell_value': sell_value, 'total': sell_value * quantity})
                total_value += sell_value * quantity

        if not items_info:
            return False, f"None of the items were found in **{name}**'s inventory!", [], 0
        if not_found:
            return True, f"Some items not found: **{', '.join(not_found)}**", items_info, total_value
        return True, "Items valued successfully", items_info, total_value

    @_serialize_writes
    def sell_items(self, name, item_quantities):
        """Sell items for the main currency. Returns (success, message, items_sold, total_earned)."""
        with self._conn() as conn:
            cursor = conn.cursor()
            stored = self._profile_name(cursor, name)
            if not stored:
                return False, f"Profile **{name}** not found!", [], 0
            catalog = self._item_catalog(cursor)

            items_sold, total_earned = [], 0
            not_found, no_value, not_enough = [], [], []
            for item_name, quantity in item_quantities.items():
                count = self._count_item(cursor, stored, item_name)
                if count == 0:
                    not_found.append(item_name)
                    continue
                if count < quantity:
                    not_enough.append(f"{item_name} (have {count}, need {quantity})")
                    continue
                sell_value = self._sell_value(cursor, item_name)
                if sell_value == 0:
                    no_value.append(item_name)
                    continue
                self._delete_items(cursor, stored, item_name, quantity)
                earned = sell_value * quantity
                cursor.execute("UPDATE profiles SET currency_main = currency_main + ? WHERE name = ?",
                               (earned, stored))
                display = catalog.get(item_name.strip().lower(), (item_name,))[0]
                items_sold.append({'name': display, 'quantity': quantity, 'sell_value': sell_value, 'total': earned})
                total_earned += earned
            conn.commit()

        if not items_sold:
            if not_enough:
                return False, f"Not enough items: **{', '.join(not_enough)}**", [], 0
            if not_found:
                return False, f"Items not found in inventory: **{', '.join(not_found)}**", [], 0
            if no_value:
                return False, f"Items have no sell value: **{', '.join(no_value)}**", [], 0
            return False, "No items could be sold!", [], 0

        warnings = []
        if not_found:
            warnings.append(f"Not found: {', '.join(not_found)}")
        if no_value:
            warnings.append(f"No value: {', '.join(no_value)}")
        if not_enough:
            warnings.append(f"Not enough: {', '.join(not_enough)}")
        message = "Items sold successfully" + (" (" + " | ".join(warnings) + ")" if warnings else "")
        return True, message, items_sold, total_earned

    # ==================== MATCHES & LOCATIONS ====================

    @_serialize_writes
    def complete_match(self, name):
        """Random match result 0-4 with rewards. Returns a dict, or None if the profile doesn't exist."""
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name, role FROM profiles WHERE LOWER(name) = LOWER(?)", (name,))
            row = cursor.fetchone()
            if not row:
                return None
            stored, role = row
            performance = random.randint(0, 4)
            cursor.execute(
                "SELECT message FROM match_messages WHERE role = ? AND performance_level = ? "
                "ORDER BY RANDOM() LIMIT 1", (role, performance))
            result = cursor.fetchone()
            main_reward, premium_reward = config.MATCH_REWARDS[performance]
            if main_reward or premium_reward:
                cursor.execute(
                    "UPDATE profiles SET currency_main = currency_main + ?, currency_premium = currency_premium + ? "
                    "WHERE name = ?", (main_reward, premium_reward, stored))
                conn.commit()

        return {
            'role': role,
            'message': result[0] if result else f"You completed a match as {role}.",
            'main': main_reward,
            'premium': premium_reward,
            'performance': performance,
            'performance_text': config.MATCH_RESULT_TEXT.get(role, {}).get(performance, f"Result: {performance}/4"),
        }

    def get_random_location(self):
        with self._conn() as conn:
            result = conn.execute("SELECT name FROM locations ORDER BY RANDOM() LIMIT 1").fetchone()
        return result[0] if result else "Unknown Location"

    # ==================== MINIGAMES ====================

    def roll_minigame(self, name, game):
        """Pick a weighted random outcome for a minigame. None if the profile doesn't exist."""
        table = self.loot_table(game)
        with self._conn() as conn:
            cursor = conn.cursor()
            if not self._profile_name(cursor, name):
                return None
            cursor.execute(f"SELECT item_name, message, description, weight, difficulty, flee_message, fail_message "
                           f"FROM {table}")
            outcomes = cursor.fetchall()

        if not outcomes:
            return {'item': None, 'message': "There is nothing to find here yet.",
                    'description': "There is nothing to find here yet.",
                    'difficulty': 10, 'flee_message': None, 'fail_message': None}

        weights = [o[3] if o[3] and o[3] > 0 else 10 for o in outcomes]
        item_name, message, description, _, difficulty, flee, fail = random.choices(outcomes, weights=weights, k=1)[0]
        has_item = item_name and item_name.strip() and item_name.strip().lower() not in ('none', 'nothing', 'null')
        return {
            'item': item_name.strip() if has_item else None,
            'message': message,
            'description': description or message,
            'difficulty': difficulty or 10,
            'flee_message': flee or None,
            'fail_message': fail or None,
        }

    @_serialize_writes
    def add_minigame_item(self, name, item_name):
        """Add one item to the inventory after a successful minigame roll."""
        if not item_name or not item_name.strip():
            return False
        with self._conn() as conn:
            cursor = conn.cursor()
            stored = self._profile_name(cursor, name)
            if not stored:
                return False
            self._insert_items(cursor, stored, item_name.strip(), 1)
            conn.commit()
            return True

    @_serialize_writes
    def add_xp(self, name, xp_amount, game):
        """Add minigame XP and handle level ups (level N -> N+1 needs N*100 XP)."""
        self.loot_table(game)  # validates the game name
        level_col, xp_col = f"{game}_level", f"{game}_xp"
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT name, {level_col}, {xp_col} FROM profiles WHERE LOWER(name) = LOWER(?)", (name,))
            result = cursor.fetchone()
            if not result:
                return None
            stored = result[0]
            current_level, new_xp = result[1] or 1, (result[2] or 0) + xp_amount
            new_level = current_level
            while new_xp >= new_level * 100:
                new_xp -= new_level * 100
                new_level += 1
            cursor.execute(f"UPDATE profiles SET {level_col} = ?, {xp_col} = ? WHERE name = ?",
                           (new_level, new_xp, stored))
            conn.commit()
        return {
            'leveled_up': new_level > current_level,
            'old_level': current_level,
            'new_level': new_level,
            'current_xp': new_xp,
            'xp_for_next': new_level * 100,
            'xp_gained': xp_amount,
        }

    @staticmethod
    def level_bonus(level):
        """Roll bonus: +1 every 5 levels (1-4: +0, 5-9: +1, ...)."""
        return ((level or 1) - 1) // 5

    def get_level_bonus(self, name, game):
        profile = self.get_profile(name)
        return self.level_bonus(profile[f"{game}_level"]) if profile else 0

    # ==================== CRYPTIC NOTES ====================

    def get_cn_maps(self):
        with self._conn() as conn:
            rows = conn.execute("SELECT id, name, other_death_chance FROM cn_maps ORDER BY id").fetchall()
        return [{'id': r[0], 'name': r[1], 'other_death_chance': r[2]} for r in rows]

    def find_cn_map(self, text):
        """By id (autocomplete value) or name, ignoring case."""
        text = (text or "").strip()
        for cn_map in self.get_cn_maps():
            if text == str(cn_map['id']) or text.lower() == cn_map['name'].lower():
                return cn_map
        return None

    @_serialize_writes
    def update_cn_map(self, map_id, other_death_chance):
        with self._conn() as conn:
            conn.execute("UPDATE cn_maps SET other_death_chance = ? WHERE id = ?",
                         (max(0, min(100, int(other_death_chance))), map_id))
            conn.commit()

    @staticmethod
    def _set_cn_spawns(cursor, kind, entry_id, map_ids):
        cursor.execute("DELETE FROM cn_spawns WHERE kind = ? AND entry_id = ?", (kind, entry_id))
        cursor.executemany("INSERT OR IGNORE INTO cn_spawns (kind, entry_id, map_id) VALUES (?, ?, ?)",
                           [(kind, entry_id, int(m)) for m in map_ids])

    def get_cn_codex(self, kind):
        """All anomalies ('anomaly') or classics ('item'), each with a 'maps' list of map ids."""
        with self._conn() as conn:
            if kind == 'anomaly':
                rows = conn.execute("SELECT id, name, danger, description FROM cn_anomalies "
                                    "ORDER BY name COLLATE NOCASE").fetchall()
                entries = [{'id': r[0], 'name': r[1], 'danger': r[2], 'description': r[3]} for r in rows]
            else:
                rows = conn.execute("SELECT id, item_name, tier, sell_value, weight, category, description "
                                    "FROM cn_items ORDER BY item_name COLLATE NOCASE").fetchall()
                entries = [{'id': r[0], 'name': r[1], 'tier': r[2], 'sell_value': r[3], 'weight': r[4],
                            'category': r[5], 'description': r[6]} for r in rows]
            spawns = conn.execute("SELECT entry_id, map_id FROM cn_spawns WHERE kind = ?", (kind,)).fetchall()
        maps_of = {}
        for entry_id, map_id in spawns:
            maps_of.setdefault(entry_id, []).append(map_id)
        for entry in entries:
            entry['maps'] = sorted(maps_of.get(entry['id'], []))
        return entries

    @_serialize_writes
    def save_cn_anomaly(self, anomaly_id, name, danger, description, map_ids):
        """Add (anomaly_id=None) or update an anomaly. Returns (success, message)."""
        name = (name or "").strip()
        if not name:
            return False, "The anomaly needs a name!"
        danger = max(1, min(5, int(danger or 2)))
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM cn_anomalies WHERE LOWER(name) = LOWER(?) AND id != ?",
                           (name, anomaly_id or -1))
            if cursor.fetchone():
                return False, f"{name} is already in the codex!"
            if anomaly_id:
                cursor.execute("UPDATE cn_anomalies SET name = ?, danger = ?, description = ? WHERE id = ?",
                               (name, danger, description or None, anomaly_id))
            else:
                cursor.execute("INSERT INTO cn_anomalies (name, danger, description) VALUES (?, ?, ?)",
                               (name, danger, description or None))
                anomaly_id = cursor.lastrowid
            self._set_cn_spawns(cursor, 'anomaly', anomaly_id, map_ids)
            conn.commit()
        return True, f"{name} saved!"

    @_serialize_writes
    def save_cn_item(self, item_id, name, tier, sell_value, weight, category, description, map_ids):
        """Add (item_id=None) or update a classic. Renaming also renames it in every inventory."""
        name = self.normalise((name or "").strip())
        if not name:
            return False, "The classic needs a name!"
        tier = tier if tier in config.CN_TIERS else 'B'
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM cn_items WHERE LOWER(item_name) = LOWER(?) AND id != ?",
                           (name, item_id or -1))
            if cursor.fetchone():
                return False, f"{name} is already in the codex!"
            values = (name, tier, max(0, int(sell_value or 0)), max(1, int(weight or 10)),
                      self.normalise_category(category or 'Collectibles'), description or None)
            old_name = None
            if item_id:
                row = cursor.execute("SELECT item_name FROM cn_items WHERE id = ?", (item_id,)).fetchone()
                old_name = row[0] if row else None
                cursor.execute("UPDATE cn_items SET item_name = ?, tier = ?, sell_value = ?, weight = ?, "
                               "category = ?, description = ? WHERE id = ?", values + (item_id,))
            else:
                cursor.execute("INSERT INTO cn_items (item_name, tier, sell_value, weight, category, description) "
                               "VALUES (?, ?, ?, ?, ?, ?)", values)
                item_id = cursor.lastrowid
            self._set_cn_spawns(cursor, 'item', item_id, map_ids)
            conn.commit()
        if old_name and old_name.strip() != name:
            self.rename_inventory_item(old_name, name)
        return True, f"{name} saved!"

    @_serialize_writes
    def delete_cn_entry(self, kind, entry_id):
        table = "cn_anomalies" if kind == 'anomaly' else "cn_items"
        with self._conn() as conn:
            conn.execute(f"DELETE FROM {table} WHERE id = ?", (entry_id,))
            conn.execute("DELETE FROM cn_spawns WHERE kind = ? AND entry_id = ?", (kind, entry_id))
            conn.commit()
        return True, "Removed from the codex."

    def import_cn_codex(self, kind, text, map_ids):
        """Many entries at once.

        Anomalies: one per line "Name | danger 1-5"   (danger optional, default 2)
        Classics:  one per line "Name | tier | sell value"  (tier B/A/S/EX, value optional)
        Returns (added_count, skipped_messages).
        """
        added, skipped = 0, []
        tier_lookup = {key.lower(): key for key in config.CN_TIERS}
        tier_lookup.update({t['label'].lower(): key for key, t in config.CN_TIERS.items()})
        tier_lookup.update({'exquisite': 'EX', 'exquisite classic': 'EX'})
        for line in (text or "").splitlines():
            parts = [p.strip() for p in line.split("|")]
            if not parts[0]:
                continue
            if kind == 'anomaly':
                danger = parts[1] if len(parts) > 1 and parts[1].isdigit() else 2
                ok, message = self.save_cn_anomaly(None, parts[0], danger, None, map_ids)
            else:
                raw = (parts[1] if len(parts) > 1 and parts[1] else 'B').lower()
                # accepts "S", "S-Tier", "S-Tier Classic", "Exquisite", "Exquisite Classic", ...
                tier = tier_lookup.get(raw) or tier_lookup.get(raw.replace('-tier', '').replace('classic', '').strip())
                if not tier:
                    skipped.append(f"{parts[0]}: unknown tier '{parts[1]}' (use B, A, S or EX)")
                    continue
                value = parts[2].replace(',', '').replace('.', '') if len(parts) > 2 else ''
                value = int(value) if value.isdigit() else config.CN_TIERS[tier]['value']
                ok, message = self.save_cn_item(None, parts[0], tier, value, 10, 'Collectibles', None, map_ids)
            if ok:
                added += 1
            else:
                skipped.append(message)
        return added, skipped

    def get_cn_messages(self):
        with self._conn() as conn:
            rows = conn.execute("SELECT id, map_id, outcome, message FROM cn_messages ORDER BY outcome, id").fetchall()
        return [{'id': r[0], 'map_id': r[1], 'outcome': r[2], 'message': r[3]} for r in rows]

    @_serialize_writes
    def save_cn_message(self, message_id, map_id, outcome, message):
        message = (message or "").strip()
        if outcome not in config.CN_OUTCOMES or not message:
            return False, "Choose a result type and write a message!"
        map_id = int(map_id) if str(map_id or '').isdigit() else None
        with self._conn() as conn:
            if message_id:
                conn.execute("UPDATE cn_messages SET map_id = ?, outcome = ?, message = ? WHERE id = ?",
                             (map_id, outcome, message, message_id))
            else:
                conn.execute("INSERT INTO cn_messages (map_id, outcome, message) VALUES (?, ?, ?)",
                             (map_id, outcome, message))
            conn.commit()
        return True, "Message saved!"

    @_serialize_writes
    def delete_cn_message(self, message_id):
        with self._conn() as conn:
            conn.execute("DELETE FROM cn_messages WHERE id = ?", (message_id,))
            conn.commit()
        return True, "Message deleted."

    def pick_cn_anomaly(self, map_id):
        """A random anomaly that can appear on this map (the whole group meets the same one), or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT a.id, a.name, a.danger FROM cn_anomalies a JOIN cn_spawns s "
                "ON s.kind = 'anomaly' AND s.entry_id = a.id WHERE s.map_id = ? ORDER BY RANDOM() LIMIT 1",
                (map_id,)).fetchone()
        return {'id': row[0], 'name': row[1], 'danger': row[2] or 1} if row else None

    @_serialize_writes
    def run_cryptic_notes(self, name, cn_map, anomaly):
        """One character's Cryptic Notes run. Returns a result dict, or None if the profile doesn't exist."""
        now = int(time.time())
        with self._conn() as conn:
            cursor = conn.cursor()
            stored = self._profile_name(cursor, name)
            if not stored:
                return None

            cursor.execute("SELECT COUNT(*) FROM cn_runs WHERE profile_name = ? AND created_at > ?",
                           (stored, now - config.CN_EXHAUSTION_WINDOW))
            earlier_runs = cursor.fetchone()[0]
            exhausted = earlier_runs >= config.CN_EXHAUSTION_RUNS   # 4th run within the hour or later

            # 1) Does the character die? The anomaly (by its danger) or something else (quota, fall, ...)
            anomaly_kill = config.CN_DANGER_KILL_CHANCE.get(anomaly['danger'], 0) if anomaly else 0
            other_death = cn_map['other_death_chance'] + (config.CN_EXHAUSTED_EXTRA_DEATH if exhausted else 0)
            items, reward = [], 0
            if random.random() * 100 < anomaly_kill:
                outcome = 'death_anomaly'
            elif random.random() * 100 < other_death:
                outcome = 'death_other'
            else:
                # 2) Survived: find classics that can appear on this map
                cursor.execute(
                    "SELECT i.item_name, i.tier, i.sell_value, i.weight FROM cn_items i JOIN cn_spawns s "
                    "ON s.kind = 'item' AND s.entry_id = i.id WHERE s.map_id = ?", (cn_map['id'],))
                pool = {}
                for item_name, tier, sell_value, weight in cursor.fetchall():
                    pool.setdefault(tier, []).append((item_name, sell_value or 0, weight or 10))
                tiers = [t for t in config.CN_TIERS if t in pool]
                if tiers:
                    counts = config.CN_ITEM_COUNT_WEIGHTS
                    for _ in range(random.choices(list(counts), weights=list(counts.values()))[0]):
                        tier = random.choices(tiers, weights=[config.CN_TIERS[t]['weight'] for t in tiers])[0]
                        item_name, sell_value, _w = random.choices(pool[tier], weights=[i[2] for i in pool[tier]])[0]
                        items.append({'name': item_name, 'tier': tier, 'sell_value': sell_value})
                rare = sum(1 for i in items if i['tier'] in ('S', 'EX'))
                outcome = 'barely' if len(items) <= 3 else ('great' if len(items) >= 7 or rare >= 2 else 'normal')
                reward = config.CN_REWARDS.get(outcome, 0)
                self._insert_items_many(cursor, stored, [i['name'] for i in items])
                if reward:
                    cursor.execute("UPDATE profiles SET currency_main = currency_main + ? WHERE name = ?",
                                   (reward, stored))

            # 3) Story text for this result (map-specific or for both maps)
            cursor.execute("SELECT message FROM cn_messages WHERE outcome = ? AND (map_id IS NULL OR map_id = ?) "
                           "ORDER BY RANDOM() LIMIT 1", (outcome, cn_map['id']))
            row = cursor.fetchone()
            text = row[0] if row else config.CN_OUTCOMES[outcome]
            for key, value in {'{character}': stored, '{map}': cn_map['name'], '{count}': str(len(items)),
                               '{anomaly}': anomaly['name'] if anomaly else 'unknown anomaly'}.items():
                text = text.replace(key, value)

            cursor.execute("INSERT INTO cn_runs (profile_name, map_id, outcome, created_at) VALUES (?, ?, ?, ?)",
                           (stored, cn_map['id'], outcome, now))
            conn.commit()

        return {
            'character': stored,
            'outcome': outcome,
            'died': outcome.startswith('death'),
            'message': text,
            'items': items,
            'total_value': sum(i['sell_value'] for i in items),
            'reward': reward,
            'runs_this_hour': earlier_runs + 1,
            'exhausted': exhausted,
        }

    @staticmethod
    def _insert_items_many(cursor, character, item_names):
        cursor.executemany("INSERT INTO inventory (character_name, item_name) VALUES (?, ?)",
                           [(character, n) for n in item_names])

    # ==================== WELCOME & REACTION ROLES ====================

    def get_welcome_embed(self):
        """(embed_row, channel_id) if welcome messages are enabled and configured, else None."""
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT enabled, embed_id, channel_id FROM welcome_settings WHERE id = 1")
            settings = cursor.fetchone()
            if not settings or not settings[0] or not settings[1] or not settings[2]:
                return None
            cursor.execute("SELECT title, description, color, footer_text, image_url, thumbnail_url "
                           "FROM embeds WHERE id = ?", (settings[1],))
            embed = cursor.fetchone()
        return (embed, settings[2]) if embed else None

    def get_reaction_role(self, message_id, emoji):
        with self._conn() as conn:
            result = conn.execute("SELECT role_id FROM reaction_roles WHERE message_id = ? AND emoji = ?",
                                  (str(message_id), emoji)).fetchone()
        return int(result[0]) if result else None

    # ==================== MAINTENANCE ====================

    def checkpoint(self):
        """Truncate the WAL file without blocking other writers (unlike VACUUM)."""
        with self._conn() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # ==================== SETTINGS ====================

    def get_settings(self, prefix):
        """{key without prefix: value} for all settings starting with `prefix`."""
        with self._conn() as conn:
            rows = conn.execute("SELECT key, value FROM settings WHERE key LIKE ?", (prefix + '%',)).fetchall()
        return {key[len(prefix):]: value for key, value in rows}

    @_serialize_writes
    def set_settings(self, prefix, values):
        with self._conn() as conn:
            conn.executemany("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                             [(prefix + key, value) for key, value in values.items()])
            conn.commit()

    # ==================== RESERVATIONS ====================

    @_serialize_writes
    def set_reservation_message(self, reservation_id, message_id):
        with self._conn() as conn:
            conn.execute("UPDATE reservations SET message_id = ? WHERE id = ?", (str(message_id), reservation_id))
            conn.commit()

    def get_reservation_end_dates(self):
        """{roster_id: expires_at} for all active roster reservations (shown on the Roster page)."""
        with self._conn() as conn:
            rows = conn.execute("SELECT r.roster_id, r.expires_at, ro.linked_id FROM reservations r "
                                "LEFT JOIN roster ro ON ro.id = r.roster_id "
                                "WHERE r.status = 'active' AND r.roster_id IS NOT NULL").fetchall()
        end_dates = {}
        for roster_id, expires_at, linked_id in rows:
            end_dates[roster_id] = expires_at
            if linked_id:
                end_dates[linked_id] = expires_at  # the linked identity ends at the same time
        return end_dates

    def get_active_reservations(self):
        with self._conn() as conn:
            return conn.execute("SELECT * FROM reservations WHERE active = 1 ORDER BY expires_at ASC").fetchall()

    def get_due_reservations(self):
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM reservations WHERE active = 1 AND notified = 0 AND expires_at <= ?",
                (int(time.time()),)).fetchall()

    @staticmethod
    def _free_reserved_character(cursor, roster_id, user_id):
        """Free a roster character that is reserved by this user (taken ones stay taken)."""
        cursor.execute("UPDATE roster SET status = 'free', player_id = NULL, player_name = NULL "
                       "WHERE id = ? AND status = 'reserved' AND player_id = ?", (roster_id, str(user_id)))
        Database._sync_linked(cursor, roster_id)

    @_serialize_writes
    def expire_reservation(self, reservation_id):
        """Mark a due reservation as done and free its roster character. Returns the reservation dict."""
        info = self.get_reservation_info(reservation_id)
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE reservations SET notified = 1, active = 0, extension_pending = 0, "
                           "status = CASE WHEN roster_id IS NOT NULL THEN 'expired' ELSE status END WHERE id = ?",
                           (reservation_id,))
            if info and info['roster_id']:
                self._free_reserved_character(cursor, info['roster_id'], info['user_id'])
            conn.commit()
        return info

    @_serialize_writes
    def delete_reservation(self, reservation_id):
        """Cancel a reservation completely (frees its roster character if it was reserved)."""
        info = self.get_reservation_info(reservation_id)
        if not info:
            return False, "Reservation not found!"
        with self._conn() as conn:
            cursor = conn.cursor()
            if info['roster_id'] and info['status'] == 'active':
                self._free_reserved_character(cursor, info['roster_id'], info['user_id'])
            cursor.execute("DELETE FROM reservations WHERE id = ?", (reservation_id,))
            conn.commit()
        return True, "Reservation removed!"

    # ==================== RESERVATION REQUESTS (roster characters) ====================

    _RES_FIELDS = ("id", "guild_id", "channel_id", "message_id", "user_id", "title", "created_at", "expires_at",
                   "status", "roster_id", "request_message_id", "reminder_message_id", "reminded",
                   "extension_pending", "note", "player_name")

    def _res_select(self):
        return "SELECT " + ", ".join(self._RES_FIELDS) + " FROM reservations"

    def get_reservation_info(self, reservation_id):
        with self._conn() as conn:
            row = conn.execute(self._res_select() + " WHERE id = ?", (reservation_id,)).fetchone()
        return dict(zip(self._RES_FIELDS, row)) if row else None

    @staticmethod
    def _count_member_reservations(cursor, user_id, statuses):
        cursor.execute(
            f"SELECT COUNT(*) FROM reservations WHERE user_id = ? AND roster_id IS NOT NULL "
            f"AND status IN ({','.join('?' * len(statuses))})", (str(user_id), *statuses))
        return cursor.fetchone()[0]

    @_serialize_writes
    def create_reservation_request(self, guild_id, channel_id, user_id, player_name, character_id, note):
        """A member asks for a roster character. Returns (success, message, reservation_id)."""
        limit = config.MAX_RESERVATIONS_PER_MEMBER
        with self._conn() as conn:
            cursor = conn.cursor()
            character = self._roster_dict(cursor.execute(self._ROSTER_SELECT + " WHERE id = ?",
                                                         (character_id,)).fetchone())
            if not character:
                return False, "That character isn't on the roster.", None
            if character['status'] != 'free':
                return False, f"**{character['display']}** is already {character['status']}.", None
            ids = self._with_linked(cursor, character['id'])  # the linked identity counts too
            cursor.execute(f"SELECT 1 FROM reservations WHERE roster_id IN ({','.join('?' * len(ids))}) "
                           f"AND status = 'pending'", ids)
            if cursor.fetchone():
                return False, f"**{character['display']}** was already requested and is waiting for staff.", None
            if self._count_member_reservations(cursor, user_id, ('pending', 'active')) >= limit:
                return False, (f"You can have at most {limit} reservations at a time "
                               f"(requests that are waiting for staff count too)."), None
            cursor.execute(
                "INSERT INTO reservations (guild_id, channel_id, user_id, title, created_at, expires_at, active, "
                "status, roster_id, note, player_name) VALUES (?, ?, ?, ?, ?, 0, 0, 'pending', ?, ?, ?)",
                (str(guild_id), str(channel_id), str(user_id), character['display'], int(time.time()),
                 character['id'], note, player_name))
            conn.commit()
            return True, "", cursor.lastrowid

    @_serialize_writes
    def set_request_message(self, reservation_id, message_id):
        with self._conn() as conn:
            conn.execute("UPDATE reservations SET request_message_id = ? WHERE id = ?", (str(message_id), reservation_id))
            conn.commit()

    @_serialize_writes
    def cancel_request(self, reservation_id):
        with self._conn() as conn:
            conn.execute("UPDATE reservations SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
                         (reservation_id,))
            conn.commit()

    @_serialize_writes
    def approve_reservation(self, reservation_id, days, staff_id):
        """Staff approved a request: the reservation starts now. Returns (success, message)."""
        info = self.get_reservation_info(reservation_id)
        if not info or info['status'] != 'pending':
            return False, "This request was already handled."
        with self._conn() as conn:
            cursor = conn.cursor()
            character = self._roster_dict(cursor.execute(self._ROSTER_SELECT + " WHERE id = ?",
                                                         (info['roster_id'],)).fetchone())
            if not character:
                return False, "This character was removed from the roster."
            if character['status'] != 'free':
                return False, f"**{character['display']}** is no longer free (it's {character['status']})."
            if self._count_member_reservations(cursor, info['user_id'], ('active',)) >= config.MAX_RESERVATIONS_PER_MEMBER:
                return False, (f"<@{info['user_id']}> already has "
                               f"{config.MAX_RESERVATIONS_PER_MEMBER} active reservations.")
            now = int(time.time())
            cursor.execute(
                "UPDATE reservations SET status = 'active', active = 1, notified = 0, created_at = ?, "
                "expires_at = ?, decided_by = ? WHERE id = ?",
                (now, now + days * 86400, str(staff_id), reservation_id))
            cursor.execute("UPDATE roster SET status = 'reserved', player_id = ?, player_name = ? WHERE id = ?",
                           (info['user_id'], info['player_name'], character['id']))
            self._sync_linked(cursor, character['id'])
            conn.commit()
        return True, "Approved"

    @_serialize_writes
    def approve_extension(self, reservation_id, days, staff_id):
        """Staff approved an extension: the reservation now runs `days` from now."""
        info = self.get_reservation_info(reservation_id)
        if not info or info['status'] != 'active' or not info['extension_pending']:
            return False, "This reservation isn't active anymore or has no open extension request."
        with self._conn() as conn:
            conn.execute(
                "UPDATE reservations SET expires_at = ?, extension_pending = 0, reminded = 0, notified = 0, "
                "decided_by = ? WHERE id = ?",
                (int(time.time()) + days * 86400, str(staff_id), reservation_id))
            conn.commit()
        return True, "Extended"

    @_serialize_writes
    def deny_request(self, reservation_id, staff_id):
        """Deny a new request or an extension. Returns (success, message, 'request' / 'extension')."""
        info = self.get_reservation_info(reservation_id)
        with self._conn() as conn:
            if info and info['status'] == 'pending':
                conn.execute("UPDATE reservations SET status = 'denied', notified = 1, decided_by = ? WHERE id = ?",
                             (str(staff_id), reservation_id))
                conn.commit()
                return True, "Denied", 'request'
            if info and info['status'] == 'active' and info['extension_pending']:
                conn.execute("UPDATE reservations SET extension_pending = 0, decided_by = ? WHERE id = ?",
                             (str(staff_id), reservation_id))
                conn.commit()
                return True, "Denied", 'extension'
        return False, "This request was already handled.", None

    @_serialize_writes
    def request_extension(self, reservation_id, user_id):
        info = self.get_reservation_info(reservation_id)
        if not info:
            return False, "Reservation not found."
        if str(user_id) != info['user_id']:
            return False, f"Only <@{info['user_id']}> can extend this reservation."
        if info['status'] != 'active':
            return False, "This reservation isn't active anymore."
        if info['extension_pending']:
            return False, "You already asked for an extension. Staff will get back to you."
        with self._conn() as conn:
            conn.execute("UPDATE reservations SET extension_pending = 1 WHERE id = ?", (reservation_id,))
            conn.commit()
        return True, "Requested"

    def get_due_reminders(self):
        """Active roster reservations that end within the reminder window and weren't reminded yet."""
        now = int(time.time())
        with self._conn() as conn:
            rows = conn.execute(
                self._res_select() + " WHERE status = 'active' AND roster_id IS NOT NULL AND reminded = 0 "
                "AND expires_at > ? AND expires_at <= ?",
                (now, now + config.RESERVATION_REMINDER_HOURS * 3600)).fetchall()
        return [dict(zip(self._RES_FIELDS, r)) for r in rows]

    @_serialize_writes
    def mark_reminded(self, reservation_id, message_id=None):
        with self._conn() as conn:
            conn.execute("UPDATE reservations SET reminded = 1, reminder_message_id = ? WHERE id = ?",
                         (str(message_id) if message_id else None, reservation_id))
            conn.commit()

    def get_member_reservations(self, user_id):
        """{'pending': [...], 'active': [...], 'taken': [...]} for /myreservations and leave alerts."""
        with self._conn() as conn:
            rows = conn.execute(
                self._res_select() + " WHERE user_id = ? AND roster_id IS NOT NULL AND status IN ('pending', 'active') "
                "ORDER BY expires_at", (str(user_id),)).fetchall()
        reservations = [dict(zip(self._RES_FIELDS, r)) for r in rows]
        return {
            'pending': [r for r in reservations if r['status'] == 'pending'],
            'active': [r for r in reservations if r['status'] == 'active'],
            'taken': [c for c in self.characters_of_player(user_id) if c['status'] == 'taken'],
        }

    # ==================== ROSTER ====================

    _ROSTER_FIELDS = ("id", "name", "title", "role", "status", "player_id", "player_name", "linked_id")
    _ROSTER_SELECT = "SELECT id, name, title, role, status, player_id, player_name, linked_id FROM roster"

    @staticmethod
    def _sync_linked(cursor, character_id):
        """Copy status and player of a character to its linked identity (if it has one)."""
        cursor.execute(
            "UPDATE roster SET status = (SELECT status FROM roster WHERE id = ?), "
            "player_id = (SELECT player_id FROM roster WHERE id = ?), "
            "player_name = (SELECT player_name FROM roster WHERE id = ?) "
            "WHERE id = (SELECT linked_id FROM roster WHERE id = ?)", (character_id,) * 4)

    @staticmethod
    def _with_linked(cursor, character_id):
        """[character_id] plus the id of its linked identity, if any."""
        row = cursor.execute("SELECT linked_id FROM roster WHERE id = ?", (character_id,)).fetchone()
        return [character_id] + ([row[0]] if row and row[0] else [])

    @_serialize_writes
    def set_roster_link(self, character_id, partner_id):
        """Link two roster entries (identity switch), or unlink with partner_id=None."""
        character = self.get_roster_character(character_id)
        if not character:
            return False, "Character not found!"
        with self._conn() as conn:
            cursor = conn.cursor()
            if not partner_id:
                if not character['linked_id']:
                    return True, ""
                cursor.execute("UPDATE roster SET linked_id = NULL WHERE id = ? OR linked_id = ?",
                               (character_id, character_id))
                conn.commit()
                return True, f"{character['display']} is no longer linked."

            partner_id = int(partner_id)
            if partner_id == character_id:
                return False, "A character can't be linked to itself!"
            if character['linked_id'] == partner_id:
                return True, ""
            partner = self._roster_dict(cursor.execute(self._ROSTER_SELECT + " WHERE id = ?",
                                                       (partner_id,)).fetchone())
            if not partner:
                return False, "Linked character not found!"
            if partner['linked_id'] and partner['linked_id'] != character_id:
                return False, f"{partner['display']} is already linked to another character. Unlink it first."
            if (character['status'] != 'free' and partner['status'] != 'free'
                    and (character['status'], character['player_id']) != (partner['status'], partner['player_id'])):
                return False, (f"Can't link: {character['display']} and {partner['display']} are reserved/taken "
                               f"differently. Release one of them first.")

            ids = (character_id, partner_id)
            cursor.execute("UPDATE roster SET linked_id = NULL WHERE id IN (?, ?) OR linked_id IN (?, ?)", ids * 2)
            cursor.execute("UPDATE roster SET linked_id = ? WHERE id = ?", (partner_id, character_id))
            cursor.execute("UPDATE roster SET linked_id = ? WHERE id = ?", (character_id, partner_id))
            # The one that is reserved/taken passes its status on to the other
            self._sync_linked(cursor, character_id if character['status'] != 'free' else partner_id)
            conn.commit()
        return True, f"{character['display']} and {partner['display']} are now linked."

    @classmethod
    def _roster_dict(cls, row):
        if not row:
            return None
        character = dict(zip(cls._ROSTER_FIELDS, row))
        character['display'] = (f"{character['name']} | {character['title']}"
                                 if character['title'] else character['name'])
        return character

    def get_roster(self, role=None):
        sql, params = self._ROSTER_SELECT, ()
        if role:
            sql, params = sql + " WHERE role = ?", (role,)
        with self._conn() as conn:
            rows = conn.execute(sql + " ORDER BY name COLLATE NOCASE", params).fetchall()
        return [self._roster_dict(r) for r in rows]

    def get_roster_character(self, character_id):
        with self._conn() as conn:
            return self._roster_dict(conn.execute(self._ROSTER_SELECT + " WHERE id = ?", (character_id,)).fetchone())

    def find_roster_character(self, text):
        """Find by id (autocomplete value), name, title or 'Name | Title', ignoring case."""
        text = (text or "").strip()
        if text.isdigit():
            found = self.get_roster_character(int(text))
            if found:
                return found
        with self._conn() as conn:
            # Exact "Name | Title" first, because one name can belong to two characters
            row = conn.execute(self._ROSTER_SELECT + " WHERE LOWER(name || ' | ' || title) = LOWER(?)",
                               (text,)).fetchone()
            if not row:
                row = conn.execute(self._ROSTER_SELECT + " WHERE LOWER(title) = LOWER(?) OR LOWER(name) = LOWER(?)",
                                   (text, text)).fetchone()
        return self._roster_dict(row)

    @staticmethod
    def _roster_duplicate(cursor, name, title, exclude_id=None):
        """True if a character with the same name AND title already exists."""
        cursor.execute("SELECT 1 FROM roster WHERE LOWER(name) = LOWER(?) "
                       "AND LOWER(COALESCE(title, '')) = LOWER(?) AND id != ?",
                       (name, title or '', exclude_id or -1))
        return cursor.fetchone() is not None

    def search_roster(self, query, statuses=None, role=None, exclude_pending=False, limit=25):
        """Roster characters whose name or title contains `query` (for autocomplete)."""
        like = f"%{(query or '').strip().lower()}%"
        sql = self._ROSTER_SELECT + " WHERE (LOWER(name) LIKE ? OR LOWER(COALESCE(title, '')) LIKE ?)"
        params = [like, like]
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            params += list(statuses)
        if role:
            sql += " AND role = ?"
            params.append(role)
        if exclude_pending:  # also hide it if its linked identity was requested
            sql += (" AND NOT EXISTS (SELECT 1 FROM reservations r WHERE r.status = 'pending' "
                    "AND (r.roster_id = roster.id OR r.roster_id = roster.linked_id))")
        sql += " ORDER BY name COLLATE NOCASE LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            return [self._roster_dict(r) for r in conn.execute(sql, params).fetchall()]

    def characters_of_player(self, user_id):
        with self._conn() as conn:
            rows = conn.execute(self._ROSTER_SELECT + " WHERE player_id = ? ORDER BY name COLLATE NOCASE",
                                (str(user_id),)).fetchall()
        return [self._roster_dict(r) for r in rows]

    @classmethod
    def _cancel_character_reservations(cls, cursor, roster_id):
        """Cancel reservations and requests of a character and of its linked identity."""
        ids = cls._with_linked(cursor, roster_id)
        cursor.execute(f"UPDATE reservations SET status = 'cancelled', active = 0, notified = 1, extension_pending = 0 "
                       f"WHERE roster_id IN ({','.join('?' * len(ids))}) AND status IN ('pending', 'active')", ids)

    @_serialize_writes
    def add_roster_character(self, name, title, role):
        name, title = (name or "").strip(), (title or "").strip() or None
        if not name:
            return False, "A name is required!"
        if role not in config.ROLES:
            return False, "Invalid role!"
        display = f"{name} | {title}" if title else name
        with self._conn() as conn:
            if self._roster_duplicate(conn.cursor(), name, title):
                return False, f"{display} is already on the roster!"
            try:
                conn.execute("INSERT INTO roster (name, title, role) VALUES (?, ?, ?)", (name, title, role))
                conn.commit()
            except sqlite3.IntegrityError as e:  # show a message instead of crashing the page
                return False, f"{display} couldn't be added: {e}"
        return True, f"{display} added to the roster!"

    def import_roster(self, text, role):
        """Add many characters at once: one 'Name | Title' (or just 'Name') per line.

        Lines copied from a roster message work too: the '﹕﹒' prefix and
        everything after '―' are ignored. Returns (added_count, skipped_messages).
        """
        added, skipped = 0, []
        for line in (text or "").splitlines():
            line = line.split("―")[0].strip().lstrip("﹕﹒").strip()
            if not line:
                continue
            name, _, title = line.partition("|")
            ok, message = self.add_roster_character(name, title, role)
            if ok:
                added += 1
            else:
                skipped.append(message)
        return added, skipped

    @_serialize_writes
    def update_roster_character(self, character_id, name, title, role):
        name, title = (name or "").strip(), (title or "").strip() or None
        if not name or role not in config.ROLES:
            return False, "Name and role are required!"
        with self._conn() as conn:
            cursor = conn.cursor()
            display = f"{name} | {title}" if title else name
            if self._roster_duplicate(cursor, name, title, exclude_id=character_id):
                return False, f"{display} is already on the roster!"
            cursor.execute("UPDATE roster SET name = ?, title = ?, role = ? WHERE id = ?",
                           (name, title, role, character_id))
            cursor.execute("UPDATE reservations SET title = ? WHERE roster_id = ? AND status IN ('pending', 'active')",
                           (display, character_id))
            conn.commit()
        return True, f"{display} updated!"

    @_serialize_writes
    def release_roster_character(self, character_id):
        """Make a character free again and cancel its reservation."""
        character = self.get_roster_character(character_id)
        if not character:
            return False, "Character not found!"
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE roster SET status = 'free', player_id = NULL, player_name = NULL WHERE id = ?",
                           (character_id,))
            self._sync_linked(cursor, character_id)
            self._cancel_character_reservations(cursor, character_id)
            conn.commit()
        return True, f"{character['display']} is free again!"

    @_serialize_writes
    def set_roster_taken(self, character_id, player_id=None, player_name=None):
        """Submission approved: the character is taken and its reservation ends (no expiry ping)."""
        character = self.get_roster_character(character_id)
        if not character:
            return False, "Character not found!"
        player = str(player_id) if player_id else character['player_id']
        if not player:
            return False, f"{character['display']} isn't reserved, so choose who plays them."
        if character['status'] == 'taken' and player == character['player_id']:
            return False, f"{character['display']} is already taken by them."
        if not player_name and player == character['player_id']:
            player_name = character['player_name']
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE roster SET status = 'taken', player_id = ?, player_name = ? WHERE id = ?",
                           (player, player_name, character_id))
            self._sync_linked(cursor, character_id)
            ids = self._with_linked(cursor, character_id)
            marks = ','.join('?' * len(ids))
            cursor.execute(f"UPDATE reservations SET status = 'completed', active = 0, notified = 1, "
                           f"extension_pending = 0 WHERE roster_id IN ({marks}) AND status = 'active'", ids)
            cursor.execute(f"UPDATE reservations SET status = 'cancelled' WHERE roster_id IN ({marks}) "
                           f"AND status = 'pending'", ids)
            conn.commit()
        return True, f"{character['display']} is now taken!"

    @_serialize_writes
    def delete_roster_character(self, character_id):
        character = self.get_roster_character(character_id)
        if not character:
            return False, "Character not found!"
        with self._conn() as conn:
            cursor = conn.cursor()
            self._cancel_character_reservations(cursor, character_id)
            if character['linked_id'] and character['status'] == 'reserved':
                # the reservation is gone, so the linked identity must not stay reserved
                cursor.execute("UPDATE roster SET status = 'free', player_id = NULL, player_name = NULL WHERE id = ?",
                               (character['linked_id'],))
            cursor.execute("UPDATE roster SET linked_id = NULL WHERE linked_id = ?", (character_id,))
            cursor.execute("DELETE FROM roster WHERE id = ?", (character_id,))
            conn.commit()
        return True, f"{character['display']} removed from the roster!"

    @_serialize_writes
    def release_player(self, user_id):
        """Free every character of a player and cancel their reservations. Returns the freed names."""
        names = [c['display'] for c in self.characters_of_player(user_id)]
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE roster SET status = 'free', player_id = NULL, player_name = NULL "
                           "WHERE player_id = ?", (str(user_id),))
            cursor.execute("UPDATE reservations SET status = 'cancelled', active = 0, notified = 1, "
                           "extension_pending = 0 WHERE user_id = ? AND roster_id IS NOT NULL "
                           "AND status IN ('pending', 'active')", (str(user_id),))
            conn.commit()
        return names

    def get_roster_message_ids(self, role):
        """IDs of the roster messages the bot posted in this role's forum post, in order."""
        with self._conn() as conn:
            return [r[0] for r in conn.execute(
                "SELECT message_id FROM roster_post_messages WHERE role = ? ORDER BY position", (role,)).fetchall()]

    @_serialize_writes
    def set_roster_message_ids(self, role, message_ids):
        with self._conn() as conn:
            conn.execute("DELETE FROM roster_post_messages WHERE role = ?", (role,))
            conn.executemany("INSERT INTO roster_post_messages (role, position, message_id) VALUES (?, ?, ?)",
                             [(role, i, str(mid)) for i, mid in enumerate(message_ids)])
            conn.commit()

    def get_old_roster_message_ids(self):
        """Messages from the time there was only one roster post (cleaned up once by the bot)."""
        with self._conn() as conn:
            return [r[0] for r in conn.execute("SELECT message_id FROM roster_messages ORDER BY position").fetchall()]

    @_serialize_writes
    def clear_old_roster_message_ids(self):
        with self._conn() as conn:
            conn.execute("DELETE FROM roster_messages")
            conn.commit()

    # ==================== ACTIVITY CHECK ====================

    @_serialize_writes
    def add_activity_character(self, character_name, role, discord_username, discord_user_id):
        with self._conn() as conn:
            conn.execute('''
                INSERT INTO activity_characters (character_name, role, discord_username, discord_user_id, added_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (character_name, role, discord_username, discord_user_id, int(time.time())))
            conn.commit()
            return True, "Character added to Activity Check!"

    @_serialize_writes
    def remove_activity_character(self, character_id):
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, roster_id FROM activity_characters WHERE id = ?", (character_id,))
            row = cursor.fetchone()
            if not row:
                return False, "Character not found!"
            if row[1]:
                return False, ("This character comes from the roster. It leaves the Activity Check "
                               "automatically when it's released on the Roster page.")
            cursor.execute("UPDATE activity_characters SET removed = 1 WHERE id = ?", (character_id,))
            conn.commit()
            return True, "Character removed from Activity Check!"

    def get_all_activity_characters(self, include_removed=False):
        query = ("SELECT id, character_name, role, discord_username, discord_user_id, added_at, removed, roster_id "
                 "FROM activity_characters ")
        if not include_removed:
            query += "WHERE removed = 0 "
        query += "ORDER BY character_name COLLATE NOCASE"
        with self._conn() as conn:
            rows = conn.execute(query).fetchall()
        return [{'id': r[0], 'character_name': r[1], 'role': r[2], 'discord_username': r[3],
                 'discord_user_id': r[4], 'added_at': r[5], 'removed': r[6], 'roster_id': r[7],
                 'is_oc': r[7] is None} for r in rows]

    @_serialize_writes
    def sync_activity_with_roster(self):
        """Put every TAKEN roster character on the Activity Check, and take released ones off.

        Linked identities become one entry. An entry belongs to one player: if the
        character gets a new player, the old entry (with its history) is retired and a
        new one is made. If the same player takes it again, the old entry comes back.
        """
        taken = sorted((c for c in self.get_roster() if c['status'] == 'taken'), key=lambda c: c['id'])
        by_id = {c['id']: c for c in taken}
        wanted = {}  # roster_id -> (name, role, player_id, player_name)
        for c in taken:
            if c['linked_id'] and c['linked_id'] in wanted:
                continue  # already covered by its linked identity
            name, role = c['display'], c['role']
            partner = by_id.get(c['linked_id'])
            if partner:
                name += f" / {partner['title'] or partner['name']}"
                if partner['role'] != c['role']:
                    role = f"{c['role']} / {partner['role']}"
            wanted[c['id']] = (name, role, c['player_id'], c['player_name'])

        with self._conn() as conn:
            cursor = conn.cursor()
            rows = cursor.execute("SELECT id, roster_id, discord_user_id, removed FROM activity_characters "
                                  "WHERE roster_id IS NOT NULL").fetchall()
            handled = set()
            for row_id, roster_id, player_id, removed in rows:
                target = wanted.get(roster_id)
                if target and str(player_id) == str(target[2]) and roster_id not in handled:
                    name, role, _, player_name = target
                    cursor.execute("UPDATE activity_characters SET character_name = ?, role = ?, removed = 0, "
                                   "discord_username = COALESCE(?, discord_username) WHERE id = ?",
                                   (name, role, player_name, row_id))
                    handled.add(roster_id)
                elif not removed:
                    cursor.execute("UPDATE activity_characters SET removed = 1 WHERE id = ?", (row_id,))
            for roster_id, (name, role, player_id, player_name) in wanted.items():
                if roster_id not in handled:
                    cursor.execute(
                        "INSERT INTO activity_characters (character_name, role, discord_username, discord_user_id, "
                        "added_at, roster_id) VALUES (?, ?, ?, ?, ?, ?)",
                        (name, role, player_name, player_id, int(time.time()), roster_id))
            conn.commit()

    @_serialize_writes
    def update_player_names(self, names):
        """Store current Discord display names ({user_id: name}) for the roster and the Activity Check."""
        if not names:
            return
        with self._conn() as conn:
            for user_id, name in names.items():
                conn.execute("UPDATE roster SET player_name = ? WHERE player_id = ?", (name, str(user_id)))
                conn.execute("UPDATE activity_characters SET discord_username = ? WHERE discord_user_id = ?",
                             (name, str(user_id)))
            conn.commit()

    @_serialize_writes
    def set_activity_status(self, character_id, month, status):
        if status not in ('active', 'inactive', 'hiatus'):
            return False, "Invalid status!"
        with self._conn() as conn:
            conn.execute('''
                INSERT INTO activity_checks (character_id, month, status, checked_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(character_id, month) DO UPDATE SET
                    status = excluded.status,
                    checked_at = excluded.checked_at
            ''', (character_id, month, status, int(time.time())))
            conn.commit()
            return True, "Status saved!"

    def get_activity_statuses_for_month(self, month):
        with self._conn() as conn:
            rows = conn.execute("SELECT character_id, status FROM activity_checks WHERE month = ?", (month,)).fetchall()
        return {row[0]: row[1] for row in rows}

    def get_all_checked_months(self):
        with self._conn() as conn:
            rows = conn.execute("SELECT DISTINCT month FROM activity_checks ORDER BY month DESC").fetchall()
        return [row[0] for row in rows]

    def get_inactive_characters_for_month(self, month):
        with self._conn() as conn:
            rows = conn.execute('''
                SELECT ac.id, ac.character_name, ac.role, ac.discord_username, ac.discord_user_id
                FROM activity_checks chk
                JOIN activity_characters ac ON ac.id = chk.character_id
                WHERE chk.month = ? AND chk.status = 'inactive'
                ORDER BY ac.character_name COLLATE NOCASE
            ''', (month,)).fetchall()
        return [{'id': r[0], 'character_name': r[1], 'role': r[2], 'discord_username': r[3], 'discord_user_id': r[4]}
                for r in rows]
