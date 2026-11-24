"""
Central configuration for the bot AND the dashboard.

Everything that is specific to this server (names, emojis, currencies,
minigames, rewards, ports, admins) lives here, so changing the theme never
requires touching the command or database code.

Values marked "env" can be overridden in the .env file without editing code.
"""
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

# ==================== GENERAL ====================

BOT_NAME = "Identity V Bot"          # Shown in the dashboard header / login page
DB_FILE = os.getenv("DB_FILE", "game_database.db")  # env

# Discord server the dashboard creates reservations for. env: GUILD_ID
GUILD_ID = os.getenv("GUILD_ID", "0")

# Users who can edit ANY character and use admin commands.
# Review this list for the new server!
ADMIN_IDS = [
    790588177240555561,
    1416546676189167796,  # Pao
]

# ==================== PORTS & SERVICES ====================
# The old bot uses 5000/5002 on the same Pi, so this one uses 5010/5012.

BOT_API_HOST = "127.0.0.1"  # Internal API: only reachable from this machine
BOT_API_PORT = int(os.getenv("BOT_API_PORT", "5012"))        # env
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "5010"))    # env
BOT_API_URL = f"http://127.0.0.1:{BOT_API_PORT}"

# systemd service names restarted by the dashboard's "Pull Updates" button
BOT_SERVICE = os.getenv("BOT_SERVICE", "idvbot")                    # env
DASHBOARD_SERVICE = os.getenv("DASHBOARD_SERVICE", "idvdashboard")  # env

# ==================== CURRENCIES ====================
# The keys "main" and "premium" are fixed (they are used in the database).
# Only change the names and emojis here.

CURRENCIES = {
    "main": {
        "name": "Clues",
        "emoji": "🔍",
    },
    "premium": {
        "name": "Echoes",   # <- or "Fragments", decide here; nothing else needs to change
        "emoji": "💠",
    },
}

# Database column for each currency (do not change after the database exists)
CURRENCY_COLUMNS = {"main": "currency_main", "premium": "currency_premium"}

# Exchange rates for /exchange (deliberately asymmetric, like the old bot)
MAIN_TO_PREMIUM_RATE = 20000   # 20,000 main -> 1 premium
PREMIUM_TO_MAIN_RATE = 5000    # 1 premium  -> 5,000 main

# ==================== ROLES & MATCHES ====================

ROLES = ["Hunter", "Survivor"]

# /match: performance 0-4 -> (main reward, premium reward)
MATCH_REWARDS = {
    0: (0, 0),
    1: (3000, 0),
    2: (6000, 0),
    3: (9000, 1),
    4: (12000, 2),
}

# Result line shown in the /match embed
MATCH_RESULT_TEXT = {
    "Hunter": {
        0: "0 Eliminations (Total Defeat)",
        1: "1 Elimination",
        2: "2 Eliminations (Draw)",
        3: "3 Eliminations",
        4: "4 Eliminations (Perfect Victory!)",
    },
    "Survivor": {
        0: "0 Survivors Escaped (Total Defeat)",
        1: "1 Survivor Escaped",
        2: "2 Survivors Escaped (Draw)",
        3: "3 Survivors Escaped",
        4: "4 Survivors Escaped (Perfect Escape!)",
    },
}

# /matchreward: reward per escape/elimination -> (main, premium)
MATCH_REWARD_PER_COUNT = (5000, 1)

# ==================== RESERVATIONS & ROSTER ====================

def _id_list(value):
    return [int(x) for x in (value or "").split(",") if x.strip().isdigit()]


RESERVE_CHANNEL_ID = int(os.getenv("RESERVE_CHANNEL_ID", "1574169279601967184"))  # members use /reserve here
STAFF_CHANNEL_ID = int(os.getenv("STAFF_CHANNEL_ID", "1574176734520156241"))      # requests & leave alerts
ROSTER_CHANNEL_ID = int(os.getenv("ROSTER_CHANNEL_ID", "0"))  # where the bot keeps the roster messages (0 = off)

# Besides ADMIN_IDS, members with one of these roles can approve/deny requests. env: STAFF_ROLE_IDS=id,id
STAFF_ROLE_IDS = _id_list(os.getenv("STAFF_ROLE_IDS"))

RESERVATION_DAYS = 14               # length of a normal approval
MAX_RESERVATION_DAYS = 14           # longest length staff can choose
MAX_RESERVATIONS_PER_MEMBER = 2     # active reservations + requests waiting for staff
RESERVATION_REMINDER_HOURS = 24     # "ends soon" ping with the Extend button

# Starting layout of the roster. Everything here can be changed on the dashboard
# (Roster -> Layout); these values are only used until something is saved there.
ROSTER_DEFAULTS = {
    "style": "embed",                              # "embed" or "message" (plain text)
    "line_format": "﹕﹒{character} ― {player}",    # {character}, {name}, {title}, {player}
    "reserved_suffix": " (reserved)",
    "alternate_indent": "1",                       # indent every second line
    "blank_lines": "1",                            # empty line between characters
    "empty_text": "*No characters yet.*",
}
for _role in ROLES:
    ROSTER_DEFAULTS.update({
        f"{_role}_title": f"{_role}s",
        f"{_role}_color": "#000000",
        f"{_role}_intro": "",
        f"{_role}_footer": "",
        f"{_role}_thumbnail": "",
        f"{_role}_image": "",
    })

# ==================== MINIGAMES ====================
# Each entry becomes a slash command, a dashboard page and a loot table.
# To add a minigame later, add an entry here and restart; the database
# table and level columns are created automatically.

MINIGAMES = {
    "scavenging": {
        "label": "Scavenging",
        "command_description": "Go scavenging for useful items!",
        "emoji": "🧰",
        "dashboard_icon": "bi-search",
        "found_field": "Found",
        "attempt_button": "Retrieve Item",
        "leave_button": "Leave It",
        "success_title": "Successful Retrieval!",
        "success_text": "Successfully retrieved **{item}**!",
        "fail_title": "Failed Retrieval!",
        "leave_title": "Item Left Behind",
        "leave_text": "**{name}** decided to leave the **{item}** behind.",
        "default_flee": "You decided to leave the {item} where it was.",
        "default_fail": "The **{item}** was too damaged or out of reach to retrieve.",
    },
    "foraging": {
        "label": "Foraging",
        "command_description": "Go foraging for plants and natural resources!",
        "emoji": "🌿",
        "dashboard_icon": "bi-flower1",
        "found_field": "Spotted",
        "attempt_button": "Gather",
        "leave_button": "Leave It",
        "success_title": "Successful Harvest!",
        "success_text": "Successfully gathered **{item}**!",
        "fail_title": "Failed Harvest!",
        "leave_title": "Left Untouched",
        "leave_text": "**{name}** decided to leave the **{item}** untouched.",
        "default_flee": "You decided to leave the {item} where it grows.",
        "default_fail": "The **{item}** crumbled in your hands before you could gather it.",
    },
}

# XP: success = base + difficulty * per_difficulty, fail = success // 4
MINIGAME_XP_BASE = 10
MINIGAME_XP_PER_DIFFICULTY = 5
MINIGAME_XP_LEAVE = 5

# ==================== INVENTORY ====================

ITEM_CATEGORIES = ['Consumables', 'Tools', 'Collectibles', 'Miscellaneous',
                   'Cosmetics', 'Pets', 'Shelter', 'NSFW']

# ==================== LOOK & FEEL ====================
# Emojis: Unicode defaults work everywhere. To use your server's custom emojis,
# replace a value with the full emoji code, e.g. "<:clue:123456789012345678>"
# (type \:emoji_name: in Discord to get it; animated ones start with <a:).

EMOJI = {
    "success": "✅",
    "error": "❌",
    "profile": "⭐",
    "match": "⚔️",
    "dice": "🎲",
    "choose": "🎯",
    "location": "🗺️",
    "level_up": "⬆️",
    "Hunter": "🗡️",
    "Survivor": "🏃",
}

COLOR_DEFAULT = (0, 0, 0)       # Normal embeds
COLOR_ERROR = (116, 7, 14)      # Errors / failures

# Bot status, rotated every few hours
STATUS_ROTATION_HOURS = 3
STATUS_MESSAGES = [
    ("watching", "/help | Watching the matches unfold"),
    ("watching", "/help | Watching survivors decode ciphers"),
    ("watching", "/help | Watching hunters stalk the manor"),
    ("playing", "/help | Playing with the survivors' hope"),
    ("watching", "/help | Watching the manor's secrets unravel"),
    ("playing", "/help | Playing an endless game of chase"),
]


def currency_label(key):
    """'🔍 Clues' style label for a currency key."""
    c = CURRENCIES[key]
    return f"{c['emoji']} {c['name']}"
