import asyncio
import io
import itertools
import os
import random
import re
import threading

import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask, request, jsonify

import config
import roster_format
from config import EMOJI, CURRENCIES, MINIGAMES, TITLE_SEP, currency_label
from database import Database, ADMIN_IDS

# ==================== SETUP ====================

COLOR_DEFAULT = discord.Color.from_rgb(*config.COLOR_DEFAULT)
COLOR_ERROR = discord.Color.from_rgb(*config.COLOR_ERROR)
MAX_ITEM_QUANTITY = 1000  # per item per command, protects against "Item:9999999"

intents = discord.Intents.default()
intents.members = True           # member joins (welcome message)
intents.guilds = True
intents.guild_reactions = True   # reaction roles


class GameBot(commands.Bot):
    async def setup_hook(self):
        # Runs once per process (on_ready can fire again after every reconnect,
        # which used to re-sync commands and crash on "task already launched").
        self.roster_lock = asyncio.Lock()
        # Buttons on request/reminder/leave messages keep working after a restart
        self.add_dynamic_items(ReservationButton, ReleasePlayerButton)
        try:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} command(s)")
        except Exception as e:
            print(f"Failed to sync commands: {e}")
        rotate_status.start()
        daily_checkpoint.start()
        check_reservations.start()


# Slash commands only; a prefix is required by discord.py but no prefix commands exist
bot = GameBot(command_prefix=commands.when_mentioned, intents=intents, help_command=None)  # /help is the help
db = Database()
api = Flask(__name__)  # internal API used by the dashboard

CURRENCY_CHOICES = [app_commands.Choice(name=c["name"], value=key) for key, c in CURRENCIES.items()]
ROLE_CHOICES = [app_commands.Choice(name=r, value=r) for r in config.ROLES]


# ==================== HELPERS ====================

# ---------- Layout: every bot message is built from these ----------
# Look:  title / empty line / text blocks separated by empty lines / empty line.
# Change SECTION_STYLE in config.py to restyle all list labels at once.

BLANK = "⠀"  # invisible character: Discord keeps a line holding it, but deletes truly empty lines


def body(*blocks):
    """Join text blocks with an empty line between them and air at the top and bottom."""
    blocks = [b.strip("\n") for b in blocks if b and b.strip()]
    if not blocks:
        return None
    return f"{BLANK}\n" + f"\n{BLANK}\n".join(blocks) + f"\n{BLANK}"


def small(*parts):
    """One grey small-text line: '-# a · b · c'."""
    parts = [p for p in parts if p]
    return "-# " + " · ".join(parts) if parts else ""


def section(label, lines):
    """'˖ Label' above some lines (style set by config.SECTION_STYLE)."""
    if isinstance(lines, (list, tuple)):
        lines = "\n".join(lines)
    return f"{config.SECTION_STYLE.format(name=label)}\n{lines}"


def add_block(embed, block):
    """Append a block to an embed built with body() (keeps the spacing)."""
    lines = (embed.description or "").split("\n")
    while lines and lines[0].strip(BLANK + " ") == "":
        lines.pop(0)
    while lines and lines[-1].strip(BLANK + " ") == "":
        lines.pop()
    embed.description = body("\n".join(lines), block)
    return embed


def quote(text):
    """Discord quote block (grey bar on the left) around every line of the text."""
    if not text or not text.strip():
        return ""
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.strip().split("\n"))


def rewards_block(heading, amounts):
    """A result line with the amounts indented underneath, one per line:
    'Escaped' / '⠀⠀🔍 1,000 Clues' / '⠀⠀🔔 2 Echoes'."""
    return "\n".join([heading] + [f"⠀⠀{a}" for a in amounts if a])


def make_embed(emoji, title, *blocks, color=None):
    return discord.Embed(title=f"{emoji}{TITLE_SEP}{title}", description=body(*blocks),
                         color=color or COLOR_DEFAULT)


def error_embed(description, title="Error!", *blocks):
    return make_embed(EMOJI['error'], title, description, *blocks, color=COLOR_ERROR)


def success_embed(title, description=None, *blocks):
    return make_embed(EMOJI['success'], title, description, *blocks)


def amount_text(key, amount, bold=True):
    """'🔍 **1,000** Clues'"""
    c = CURRENCIES[key]
    number = f"**{amount:,}**" if bold else f"{amount:,}"
    return f"{c['emoji']} {number} {c['name']}"


async def require_owner(interaction, name):
    """Send 'Access Denied' and return False if the user doesn't own the character."""
    is_owner, msg = db.check_ownership(name, interaction.user.id)
    if not is_owner:
        await interaction.response.send_message(embed=error_embed(msg, "Access Denied!"), ephemeral=True)
    return is_owner


async def require_admin(interaction):
    if interaction.user.id in ADMIN_IDS:
        return True
    await interaction.response.send_message(
        embed=error_embed("Only admins can use this command!", "Access Denied!"), ephemeral=True)
    return False


def parse_item_quantities(text):
    """'Item:3, Other Item' -> ({'Item': 3, 'Other Item': 1}, None) or (None, error message).

    The same item typed twice (any capitalisation) is merged.
    """
    quantities, spelling = {}, {}
    for entry in text.split(','):
        entry = entry.strip()
        if not entry:
            continue
        if ':' in entry:
            item_name, _, qty_text = entry.rpartition(':')
            item_name = item_name.strip()
            try:
                qty = int(qty_text.strip())
            except ValueError:
                return None, f"Invalid quantity for **{item_name}**! Use format: Item:Number (e.g., Lantern:3)"
            if qty < 1:
                return None, f"Quantity must be at least 1 for **{item_name}**!"
        else:
            item_name, qty = entry, 1
        if not item_name:
            return None, "An item name is missing! Use format: Item:Number (e.g., Lantern:3)"
        key = spelling.setdefault(item_name.lower(), item_name)
        quantities[key] = quantities.get(key, 0) + qty
        if quantities[key] > MAX_ITEM_QUANTITY:
            return None, f"You can only handle up to {MAX_ITEM_QUANTITY:,} of **{key}** at once!"
    if not quantities:
        return None, "Please name at least one item!"
    return quantities, None


def format_items(item_quantities):
    """{'A': 2, 'B': 1} -> '**A** ×2, **B**'"""
    return ", ".join(f"**{name}** ×{qty}" if qty > 1 else f"**{name}**" for name, qty in item_quantities.items())


def _items_total_line(item_quantities, verb):
    """'-# 5 items added' when more than one item was involved."""
    total = sum(item_quantities.values())
    return small(f"{total} items {verb}") if total > 1 else None


async def resolve_channel(channel_id):
    """Find a channel, thread or forum post by ID (also archived/uncached ones)."""
    channel = bot.get_channel(channel_id)
    if channel:
        return channel
    for guild in bot.guilds:
        thread = guild.get_thread(channel_id)
        if thread:
            return thread
    try:
        return await bot.fetch_channel(channel_id)
    except discord.DiscordException:
        return None


# ==================== PROFILE COMMANDS ====================

@bot.tree.command(name="create", description="Create a character profile")
@app_commands.describe(name="Character name", role="Character role")
@app_commands.choices(role=ROLE_CHOICES)
async def create_profile(interaction: discord.Interaction, name: str, role: app_commands.Choice[str]):
    success, message = db.create_profile(name, role.value, interaction.user.id)
    if success:
        embed = success_embed("Profile Created!", f"Successfully created profile for **{name}** as a **{role.value}**!")
    else:
        embed = error_embed(message)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="editname", description="Edit a character's name")
@app_commands.describe(name="Current character name", new_name="New character name")
async def edit_profile_name(interaction: discord.Interaction, name: str, new_name: str):
    if not await require_owner(interaction, name):
        return
    success, message = db.edit_profile_name(name, new_name)
    if success:
        embed = success_embed("Name updated!", f"Successfully renamed **{name}** to **{new_name.strip()}**!")
    else:
        embed = error_embed(message)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="editrole", description="Edit a character's role")
@app_commands.describe(name="Character name", new_role="New role")
@app_commands.choices(new_role=ROLE_CHOICES)
async def edit_profile_role(interaction: discord.Interaction, name: str, new_role: app_commands.Choice[str]):
    if not await require_owner(interaction, name):
        return
    success, message = db.edit_profile_role(name, new_role.value)
    if success:
        embed = success_embed("Role updated!", f"Successfully changed **{name}**'s role to **{new_role.value}**!")
    else:
        embed = error_embed(message)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="deleteprofile", description="Delete a character profile")
@app_commands.describe(name="Character name to delete")
async def delete_profile(interaction: discord.Interaction, name: str):
    if not await require_owner(interaction, name):
        return
    success, message = db.delete_profile(name)
    if success:
        embed = success_embed("Profile deleted!", f"Successfully deleted profile for **{name}**!")
    else:
        embed = error_embed(message)
    await interaction.response.send_message(embed=embed)


def _progress_bar(level, xp):
    xp_needed = level * 100
    filled = min(10, int(xp / xp_needed * 10)) if xp_needed > 0 else 0
    return "█" * filled + "░" * (10 - filled)


INDENT = "⠀⠀"


class ProfileView(discord.ui.View):
    """Profile with 'Main Info' / 'Inventory' pages; the inventory gets ◀ ▶ when it's long."""

    def __init__(self, profile_data, inventory_items):
        super().__init__(timeout=180)
        self.profile_data = profile_data
        self.items = inventory_items                       # already sorted A–Z
        per_page = max(1, config.INVENTORY_ITEMS_PER_PAGE)
        self.inv_pages = [self.items[i:i + per_page] for i in range(0, len(self.items), per_page)] or [[]]
        self.current_page = 0                              # 0 = main info, 1 = inventory
        self.inv_page = 0
        self._update_buttons()

    def _update_buttons(self):
        on_inventory = self.current_page == 1
        self.main_info_button.style = discord.ButtonStyle.secondary if on_inventory else discord.ButtonStyle.primary
        self.inventory_button.style = discord.ButtonStyle.primary if on_inventory else discord.ButtonStyle.secondary
        paged = on_inventory and len(self.inv_pages) > 1
        for button in (self.prev_button, self.next_button):
            if paged and button not in self.children:
                self.add_item(button)
            elif not paged and button in self.children:
                self.remove_item(button)
        self.prev_button.disabled = self.inv_page == 0
        self.next_button.disabled = self.inv_page >= len(self.inv_pages) - 1

    async def _show(self, interaction):
        self._update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="Main Info", style=discord.ButtonStyle.primary)
    async def main_info_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = 0
        await self._show(interaction)

    @discord.ui.button(label="Inventory", style=discord.ButtonStyle.secondary)
    async def inventory_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = 1
        await self._show(interaction)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.inv_page = max(0, self.inv_page - 1)
        await self._show(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.inv_page = min(len(self.inv_pages) - 1, self.inv_page + 1)
        await self._show(interaction)

    def create_embed(self):
        p = self.profile_data
        if self.current_page == 0:
            currency = "\n".join(f"{INDENT}{c['emoji']}　**{p[key]:,}** {c['name']}" for key, c in CURRENCIES.items())
            levels = "\n\n".join(  # an empty line between the minigames
                f"{INDENT}{cfg['emoji']}　{cfg['label']} · **Lv. {p[f'{game}_level']}**\n"
                f"{INDENT}{_progress_bar(p[f'{game}_level'], p[f'{game}_xp'])}　"
                f"{p[f'{game}_xp']}/{p[f'{game}_level'] * 100} XP"
                for game, cfg in MINIGAMES.items())
            # The profile uses plain "Role:" labels instead of SECTION_STYLE
            return make_embed(EMOJI['profile'], p['name'],
                              f"Role:\n{INDENT}{p['role']}",
                              f"Currency:\n{currency}",
                              f"Levels:\n{levels}" if levels else None)

        inventory_emoji = EMOJI.get('inventory', '🎒')
        if not self.items:
            return make_embed(inventory_emoji, f"{p['name']}'s Inventory", "*The inventory is empty.*",
                              "Play minigames or matches to collect items!")
        lines = "\n".join(f"{INDENT}﹒{item['item_name']}　`×{item['quantity']}`"
                          for item in self.inv_pages[self.inv_page])
        embed = make_embed(inventory_emoji, f"{p['name']}'s Inventory", section("Miscellaneous", f"{BLANK}\n{lines}"))
        total = sum(item['quantity'] for item in self.items)
        footer = f"{total} item{'s' if total != 1 else ''} in total · {len(self.items)} different"
        if len(self.inv_pages) > 1:
            footer += f" · Page {self.inv_page + 1}/{len(self.inv_pages)}"
        embed.set_footer(text=footer)
        return embed


@bot.tree.command(name="profile", description="Show a character's profile")
@app_commands.describe(name="Character name")
async def show_profile(interaction: discord.Interaction, name: str):
    profile = db.get_profile(name)
    if not profile:
        await interaction.response.send_message(embed=error_embed(f"Profile **{name}** not found!"))
        return
    view = ProfileView(profile, db.get_inventory(name))
    await interaction.response.send_message(embed=view.create_embed(), view=view)


@bot.tree.command(name="list", description="Show all your character profiles")
async def list_profiles(interaction: discord.Interaction):
    profiles = db.get_profiles_for_user(interaction.user.id)
    if not profiles:
        await interaction.response.send_message(embed=make_embed(
            EMOJI['profile'], "Your Characters", "You don't have any characters yet.", small("Create one with /create")))
        return

    main_emoji, premium_emoji = CURRENCIES['main']['emoji'], CURRENCIES['premium']['emoji']

    # One '˖ Survivors' / '˖ Hunters' block per role (configured roles first); long lists continue on the next page
    roles = list(config.ROLES) + sorted({p[1] for p in profiles} - set(config.ROLES))
    blocks = []
    for role in roles:
        lines = [f"{EMOJI.get(role, '•')} **{p[0]}** · {main_emoji} {p[2]:,} · {premium_emoji} {p[3]:,}"
                 for p in profiles if p[1] == role]
        chunk, first = [], True
        for line in lines + [None]:
            if line is None or (chunk and len("\n".join(chunk + [line])) > 1500):
                if chunk:
                    blocks.append(section(f"{role}s" if first else f"{role}s (cont.)", chunk))
                    chunk, first = [], False
            if line is not None:
                chunk.append(line)

    pages, current = [], []
    for block in blocks:
        if current and len("\n\n".join(current + [block])) > 3500:
            pages.append(current)
            current = []
        current.append(block)
    pages.append(current)

    def make_page(page_num):
        embed = make_embed(EMOJI['profile'], "Your Characters",
                           f"You have **{len(profiles)}** character{'s' if len(profiles) != 1 else ''}.",
                           *pages[page_num])
        footer = "/profile [name] shows the details"
        if len(pages) > 1:
            footer += f" · Page {page_num + 1}/{len(pages)}"
        embed.set_footer(text=footer)
        return embed

    if len(pages) == 1:
        await interaction.response.send_message(embed=make_page(0))
        return

    class ListView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            self.page = 0

        @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
        async def prev_btn(self, inter: discord.Interaction, button: discord.ui.Button):
            self.page = max(0, self.page - 1)
            await inter.response.edit_message(embed=make_page(self.page), view=self)

        @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
        async def next_btn(self, inter: discord.Interaction, button: discord.ui.Button):
            self.page = min(len(pages) - 1, self.page + 1)
            await inter.response.edit_message(embed=make_page(self.page), view=self)

    await interaction.response.send_message(embed=make_page(0), view=ListView())


@bot.tree.command(name="claim", description="Claim an unowned character")
@app_commands.describe(name="Character name to claim")
async def claim_character(interaction: discord.Interaction, name: str):
    success, message = db.claim_character(name, interaction.user.id)
    embed = success_embed("Character claimed!", message) if success else error_embed(message)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="assignowner", description="[ADMIN] Manually assign character ownership")
@app_commands.describe(character="Character name", user="User to assign ownership to")
async def assign_owner(interaction: discord.Interaction, character: str, user: discord.User):
    if not await require_admin(interaction):
        return
    success, message = db.assign_owner(character, user.id)
    if success:
        embed = success_embed("Owner assigned!", f"**{character}** is now owned by {user.mention}.", small(message))
    else:
        embed = error_embed(message)
    await interaction.response.send_message(embed=embed)


# ==================== CURRENCY COMMANDS ====================

MAIN_NAME = CURRENCIES['main']['name']
PREMIUM_NAME = CURRENCIES['premium']['name']


@bot.tree.command(name="addcurrency", description=f"[ADMIN] Add {MAIN_NAME} or {PREMIUM_NAME} to a character")
@app_commands.describe(name="Character name", currency="Currency type", amount="Amount to add")
@app_commands.choices(currency=CURRENCY_CHOICES)
async def add_currency(interaction: discord.Interaction, name: str, currency: app_commands.Choice[str], amount: int):
    if not await require_admin(interaction):
        return
    success, message = db.add_currency(name, currency.value, amount)
    if success:
        embed = success_embed("Currency added!", f"Added {amount_text(currency.value, amount)} to **{name}**!")
    else:
        embed = error_embed(message)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="removecurrency", description=f"[ADMIN] Remove {MAIN_NAME} or {PREMIUM_NAME} from a character")
@app_commands.describe(name="Character name", currency="Currency type", amount="Amount to remove")
@app_commands.choices(currency=CURRENCY_CHOICES)
async def remove_currency(interaction: discord.Interaction, name: str, currency: app_commands.Choice[str], amount: int):
    if not await require_admin(interaction):
        return
    success, message = db.remove_currency(name, currency.value, amount)
    if success:
        embed = success_embed("Currency removed!", f"Removed {amount_text(currency.value, amount)} from **{name}**!")
    else:
        embed = error_embed(message)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="givecurrency", description="Give currency from your character to another character")
@app_commands.describe(sender_name="Your character's name", currency="Currency type", amount="Amount to give",
                       receiver_name="The receiver's character name")
@app_commands.choices(currency=CURRENCY_CHOICES)
async def give_currency(interaction: discord.Interaction, sender_name: str, currency: app_commands.Choice[str],
                        amount: int, receiver_name: str):
    if not await require_owner(interaction, sender_name):
        return
    success, message = db.transfer_currency(sender_name, receiver_name, currency.value, amount)
    if success:
        embed = success_embed("Currency Transferred!",
                              f"**{sender_name}** gave {amount_text(currency.value, amount)} to **{receiver_name}**!")
    else:
        embed = error_embed(message)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="exchange", description=f"Exchange {MAIN_NAME} for {PREMIUM_NAME} or vice versa")
@app_commands.describe(name="Character name", from_currency="Currency you want to spend", amount="Amount to spend")
@app_commands.choices(from_currency=CURRENCY_CHOICES)
async def exchange_currency(interaction: discord.Interaction, name: str, from_currency: app_commands.Choice[str],
                            amount: int):
    if not await require_owner(interaction, name):
        return
    success, message, received, to_currency = db.exchange_currency(name, from_currency.value, amount)
    if not success:
        await interaction.response.send_message(embed=error_embed(message), ephemeral=True)
        return
    embed = success_embed("Exchange Complete!", f"**{name}** exchanged currency!",
                          small(f"Spent {amount_text(from_currency.value, amount)}",
                                f"Received {amount_text(to_currency, received)}"))
    await interaction.response.send_message(embed=embed)


# ==================== MATCH COMMANDS ====================

@bot.tree.command(name="match", description="Play a match and earn rewards")
@app_commands.describe(name="Character name")
async def match(interaction: discord.Interaction, name: str):
    if not await require_owner(interaction, name):
        return
    result = db.complete_match(name)
    if not result:
        await interaction.response.send_message(embed=error_embed(f"Profile **{name}** not found!"))
        return
    embed = make_embed(EMOJI['match'], f"{name}'s {result['role']} Match", result['message'],
                       rewards_block(result['performance_text'],
                                     [amount_text('main', result['main']), amount_text('premium', result['premium'])]),
                       color=COLOR_ERROR)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="matchreward",
                  description=f"Give match rewards ({MAIN_NAME} & {PREMIUM_NAME}) based on escapes or eliminations")
@app_commands.describe(name="Character name", count="Number of escapes or eliminations (1–8)")
@app_commands.choices(count=[app_commands.Choice(name=str(i), value=i) for i in range(1, 9)])
async def match_reward(interaction: discord.Interaction, name: str, count: app_commands.Choice[int]):
    if not await require_owner(interaction, name):
        return
    main_per, premium_per = config.MATCH_REWARD_PER_COUNT
    main_reward, premium_reward = count.value * main_per, count.value * premium_per
    success, message = db.add_rewards(name, main_reward, premium_reward)
    if not success:
        await interaction.response.send_message(embed=error_embed(message), ephemeral=True)
        return
    embed = success_embed("Match Rewards Granted!",
                          rewards_block(f"**{name}** received their match rewards!",
                                        [amount_text('main', main_reward), amount_text('premium', premium_reward)]))
    await interaction.response.send_message(embed=embed)


# ==================== CRYPTIC NOTES (/cnmatch) ====================

CN_SHORT = {'barely': "Barely made quota", 'normal': "Made quota", 'great': "Big haul",
            'death_anomaly': "Killed", 'death_other': "Didn't make it"}


async def cn_location_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=m['name'], value=str(m['id'])) for m in db.get_cn_maps()
            if current.lower() in m['name'].lower()][:25]


async def own_character_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=p[0], value=p[0]) for p in db.get_profiles_for_user(interaction.user.id)
            if current.lower() in p[0].lower()][:25]


async def any_character_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=n, value=n) for n in db.search_profile_names(current)]


def _cn_item_lines(result):
    """'🟣 Cursed Sapphire · 1,000' lines, most valuable first (the dot already shows the tier)."""
    counted = {}
    for item in result['items']:
        entry = counted.setdefault(item['name'], [item, 0])
        entry[1] += 1
    lines = []
    for name, (item, qty) in sorted(counted.items(), key=lambda kv: (-kv[1][0]['sell_value'], kv[0].lower())):
        tier = config.CN_TIERS.get(item['tier'], {})
        lines.append(f"{tier.get('emoji', '•')} {name}{f' ×{qty}' if qty > 1 else ''} · {item['sell_value']:,}")
    return lines


def _cn_value_block(result, heading):
    """'Made quota' with the classics' value and the survival bonus underneath, one per line."""
    return rewards_block(heading, [
        f"{amount_text('main', result['total_value'])} in classics",
        f"{amount_text('main', result['reward'])} survival bonus" if result['reward'] else ""])


def _cn_exhaustion_text(result):
    if result['runs_this_hour'] < config.CN_EXHAUSTION_RUNS:
        return ""
    text = (f"{config.CN_ICONS['exhausted']} **{result['character']}** is exhausted "
            f"({result['runs_this_hour']} runs within the hour). Another run could easily end in death.")
    if result['exhausted']:
        text += " The exhaustion already made this run more dangerous."
    return text


def _cn_headline(r):
    """'Big haul' / 'Killed by the Wraith' / "Didn't make it"."""
    if r['outcome'] == 'death_anomaly' and r.get('killer'):
        return f"Killed by the {r['killer']}"
    return CN_SHORT[r['outcome']]


def _cn_anomaly_line(anomalies):
    """'-# Anomalies: Wraith ▰▰▰▱▱ · Shade ▰▱▱▱▱'"""
    if not anomalies:
        return ""
    icons = config.CN_ICONS
    parts = [f"{a['name']} {icons['danger_full'] * a['danger']}{icons['danger_empty'] * (5 - a['danger'])}"
             for a in anomalies]
    return small(f"{'Anomaly' if len(anomalies) == 1 else 'Anomalies'}: {parts[0]}", *parts[1:])


def _cn_shorten(text, limit):
    return text if not limit or len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _cn_solo_blocks(r):
    """Solo result: story, then loot with the rewards underneath (or the death), then exhaustion."""
    icons = config.CN_ICONS
    if r['died']:
        outcome = f"{icons['died']} **{_cn_headline(r)}**\n{small(config.CN_RESPAWN_TEXT)}"
        return [r['message'], outcome, _cn_exhaustion_text(r)]
    loot = section(f"Classics found ({len(r['items'])})", _cn_item_lines(r))
    rewards = _cn_value_block(r, f"{icons['survived']} **{_cn_headline(r)}**")
    return [r['message'], loot, rewards, _cn_exhaustion_text(r)]


def _cn_card(r, story_limit=None):
    """Group result: one card (embed) per character."""
    icons = config.CN_ICONS
    story = _cn_shorten(r['message'], story_limit)
    if r['died']:
        blocks = [story, small(config.CN_RESPAWN_TEXT)]
        icon, color = icons['died'], COLOR_ERROR
    else:
        loot = rewards_block("\n".join(_cn_item_lines(r)), [
            f"{amount_text('main', r['total_value'])} in classics",
            f"{amount_text('main', r['reward'])} survival bonus" if r['reward'] else ""])
        blocks = [story, loot]
        icon, color = icons['survived'], COLOR_DEFAULT
    return make_embed(icon, f"{r['character']} · {_cn_headline(r)}", *blocks, _cn_exhaustion_text(r), color=color)


def _embeds_length(embeds):
    return sum(len(e.title or "") + len(e.description or "") + len((e.footer.text if e.footer else None) or "")
               for e in embeds)


def run_cn_group(cn_map, names):
    """Run Cryptic Notes for one or more characters and build the result.

    Returns a list of embeds: solo = one embed; group = a header card plus one card per character.
    Everyone in the group meets the same 1-3 anomalies.
    """
    anomalies = db.pick_cn_anomalies(cn_map['id'])
    results = [r for r in (db.run_cryptic_notes(n, cn_map, anomalies) for n in names) if r]
    all_dead = bool(results) and all(r['died'] for r in results)
    title = f"Cryptic Notes · {cn_map['name']}"

    if len(results) <= 1:  # solo run: the story is the main text
        blocks = _cn_solo_blocks(results[0]) if results else ["Nobody could enter."]
        embed = make_embed(config.CN_EMOJI, title, _cn_anomaly_line(anomalies), *blocks,
                           color=COLOR_ERROR if all_dead else COLOR_DEFAULT)
        embed.description = embed.description[:4096]
        return [embed]

    survivors = sum(1 for r in results if not r['died'])
    header_lines = [_cn_anomaly_line(anomalies), small(f"{survivors}/{len(results)} made it out")]
    header = make_embed(config.CN_EMOJI, title, "\n".join(line for line in header_lines if line),
                        color=COLOR_ERROR if all_dead else COLOR_DEFAULT)
    # Discord allows 6,000 characters across all cards of one message: shorten long stories if needed
    for limit in (None, 500, 300, 150, 60):
        cards = [_cn_card(r, limit) for r in results]
        if _embeds_length([header] + cards) <= 5900:
            break
    for card in cards:
        card.description = card.description[:4096]
    return [header] + cards


class CNGroupView(discord.ui.View):
    """Group invite: every invited player has to accept for their own character."""
    ICONS = {key: config.CN_ICONS[key] for key in ('leader', 'accepted', 'declined', 'pending')}

    def __init__(self, leader_id, cn_map, members, owners):
        super().__init__(timeout=config.CN_INVITE_TIMEOUT)
        self.leader_id, self.cn_map, self.members, self.owners = leader_id, cn_map, members, owners
        self.answers = {members[0]: 'leader'}
        for name in members[1:]:  # characters the leader owns join right away
            self.answers[name] = 'accepted' if owners[name] == leader_id else 'pending'
        self.message = None
        self.done = False

    def pending(self):
        return [name for name, answer in self.answers.items() if answer == 'pending']

    def team(self):
        return [name for name, answer in self.answers.items() if answer in ('leader', 'accepted')]

    def invite_embed(self, closing_note=None):
        lines = [f"{self.ICONS[self.answers[n]]} **{n}** · <@{self.owners[n]}>" for n in self.members]
        embed = make_embed(
            config.CN_EMOJI, f"Cryptic Notes · {self.cn_map['name']}",
            f"<@{self.leader_id}> wants to enter **{self.cn_map['name']}** as a group.",
            section("Group", lines),
            closing_note or small("Each player accepts for their own character"))
        if not closing_note:
            embed.set_footer(text=f"Expires in {config.CN_INVITE_TIMEOUT // 60} minutes · "
                                  f"the leader can start early with everyone who accepted")
        return embed

    async def start(self, interaction):
        if self.done:
            return
        self.done = True
        self.stop()
        await interaction.response.edit_message(content=None, embeds=run_cn_group(self.cn_map, self.team()), view=None)

    async def answer(self, interaction, accept):
        mine = [n for n in self.pending() if self.owners[n] == interaction.user.id]
        if not mine:
            await interaction.response.send_message(
                embed=error_embed("You don't have a character waiting for an answer in this invite."), ephemeral=True)
            return
        for name in mine:
            self.answers[name] = 'accepted' if accept else 'declined'
        if not self.pending():  # everyone answered: go
            await self.start(interaction)
        else:
            await interaction.response.edit_message(embed=self.invite_embed(), view=self)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.answer(interaction, True)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.answer(interaction, False)

    @discord.ui.button(label="Start now", style=discord.ButtonStyle.primary)
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.leader_id:
            await interaction.response.send_message(embed=error_embed("Only the group leader can start."),
                                                    ephemeral=True)
            return
        await self.start(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.leader_id:
            await interaction.response.send_message(embed=error_embed("Only the group leader can cancel."),
                                                    ephemeral=True)
            return
        self.done = True
        self.stop()
        embed = self.invite_embed("**Cancelled by the leader.**")
        await interaction.response.edit_message(content=None, embed=embed, view=None)

    async def on_timeout(self):
        if self.done or not self.message:
            return
        self.done = True
        embed = self.invite_embed("**The invite expired.** Use /cnmatch again to try once more.")
        try:
            await self.message.edit(content=None, embed=embed, view=None)
        except discord.HTTPException:
            pass


@bot.tree.command(name="cnmatch", description="Play a Cryptic Notes match, alone or as a group of up to 4")
@app_commands.describe(location="The Cryptic Notes map", character="Your character",
                       teammate1="Group: another player's character (they have to accept)",
                       teammate2="Group: another player's character (they have to accept)",
                       teammate3="Group: another player's character (they have to accept)")
@app_commands.autocomplete(location=cn_location_autocomplete, character=own_character_autocomplete,
                           teammate1=any_character_autocomplete, teammate2=any_character_autocomplete,
                           teammate3=any_character_autocomplete)
async def cn_match(interaction: discord.Interaction, location: str, character: str,
                   teammate1: str = None, teammate2: str = None, teammate3: str = None):
    if not await require_owner(interaction, character):
        return
    cn_map = db.find_cn_map(location)
    if not cn_map:
        names = " or ".join(f"**{m['name']}**" for m in db.get_cn_maps())
        await interaction.response.send_message(embed=error_embed(f"Unknown location. Choose {names}."),
                                                ephemeral=True)
        return
    leader = db.get_profile(character)

    teammates = [t for t in (teammate1, teammate2, teammate3) if t][:config.CN_GROUP_MAX - 1]
    if not teammates:  # solo run
        await interaction.response.send_message(embeds=run_cn_group(cn_map, [leader['name']]))
        return

    members, owners = [leader['name']], {leader['name']: interaction.user.id}
    for name in teammates:
        profile = db.get_profile(name)
        if not profile:
            await interaction.response.send_message(embed=error_embed(f"Character **{name}** not found!"),
                                                    ephemeral=True)
            return
        if profile['name'].lower() in (m.lower() for m in members):
            await interaction.response.send_message(
                embed=error_embed(f"**{profile['name']}** is in the group twice!"), ephemeral=True)
            return
        if not profile['user_id']:
            await interaction.response.send_message(
                embed=error_embed(f"**{profile['name']}** has no owner, so nobody can accept the invite."),
                ephemeral=True)
            return
        members.append(profile['name'])
        owners[profile['name']] = int(profile['user_id'])

    view = CNGroupView(interaction.user.id, cn_map, members, owners)
    if not view.pending():  # all teammates belong to the leader
        await interaction.response.send_message(embeds=run_cn_group(cn_map, view.team()))
        return
    invited = " ".join(sorted({f"<@{owners[n]}>" for n in view.pending()}))
    await interaction.response.send_message(content=f"{invited} you're invited to a Cryptic Notes match!",
                                            embed=view.invite_embed(), view=view)
    view.message = await interaction.original_response()


# ==================== SHOP & INVENTORY COMMANDS ====================

# The shop itself is posted in a channel; the items and prices for /buy are set on the dashboard (Shop page).

@bot.tree.command(name="buy", description="Purchase items from the shop")
@app_commands.describe(name="Character name",
                       items="Items with optional quantities (e.g., 'Lantern:3, Bandage:2' or just 'Lantern, Bandage')")
async def buy_item(interaction: discord.Interaction, name: str, items: str):
    if not await require_owner(interaction, name):
        return
    item_quantities, error = parse_item_quantities(items)
    if error:
        await interaction.response.send_message(embed=error_embed(error))
        return
    success, message, total_price, currency, purchased = db.buy_items_with_quantity(name, item_quantities)
    if not success:
        await interaction.response.send_message(embed=error_embed(message))
        return
    total_items = sum(purchased.values())
    embed = success_embed("Purchase successful!",
                          f"**{name}** purchased {format_items(purchased)} for {amount_text(currency, total_price)}!",
                          small(f"{total_items} items", amount_text(currency, total_price, bold=False))
                          if total_items > 1 else None)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="additem", description="Add items to a character's inventory")
@app_commands.describe(name="Character name",
                       items="Items with optional quantities (e.g., 'Lantern:3, Bandage:2' or just 'Lantern')")
async def add_item(interaction: discord.Interaction, name: str, items: str):
    if not await require_owner(interaction, name):
        return
    item_quantities, error = parse_item_quantities(items)
    if error:
        await interaction.response.send_message(embed=error_embed(error))
        return
    item_quantities = {db.normalise(k): v for k, v in item_quantities.items()}
    success, message, added = db.add_items_with_quantity(name, item_quantities)
    if not success:
        await interaction.response.send_message(embed=error_embed(message))
        return
    embed = success_embed("Items added!", f"Added {format_items(added)} to **{name}**'s inventory!",
                          _items_total_line(added, "added"))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="removeitem", description="Remove items from a character's inventory")
@app_commands.describe(name="Character name",
                       items="Items with optional quantities (e.g., 'Lantern:3, Bandage:2' or just 'Lantern')")
async def remove_item(interaction: discord.Interaction, name: str, items: str):
    if not await require_owner(interaction, name):
        return
    item_quantities, error = parse_item_quantities(items)
    if error:
        await interaction.response.send_message(embed=error_embed(error))
        return
    success, message, removed = db.remove_items_with_quantity(name, item_quantities)
    if not success:
        await interaction.response.send_message(embed=error_embed(message))
        return
    embed = success_embed("Items removed!", f"Removed {format_items(removed)} from **{name}**'s inventory!",
                          _items_total_line(removed, "removed"))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="use", description="Use items from your character's inventory")
@app_commands.describe(name="Character name",
                       items="Items with optional quantities (e.g., 'Lantern:3, Bandage:2' or just 'Lantern, Bandage')")
async def use_items(interaction: discord.Interaction, name: str, items: str):
    if not await require_owner(interaction, name):
        return
    item_quantities, error = parse_item_quantities(items)
    if error:
        await interaction.response.send_message(embed=error_embed(error))
        return
    success, message, used = db.use_items_with_quantity(name, item_quantities)
    if not success:
        await interaction.response.send_message(embed=error_embed(message))
        return
    embed = success_embed("Items used!", f"**{name}** used {format_items(used)}!", _items_total_line(used, "used"))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="giveitem", description="Give item(s) from your character to another character")
@app_commands.describe(sender_name="Your character's name",
                       items="Items with optional quantities (e.g., 'Lantern:3, Bandage' or just 'Lantern')",
                       receiver_name="The receiver's character name")
async def give_item(interaction: discord.Interaction, sender_name: str, items: str, receiver_name: str):
    if not await require_owner(interaction, sender_name):
        return
    item_quantities, error = parse_item_quantities(items)
    if error:
        await interaction.response.send_message(embed=error_embed(error))
        return
    success, message, moved = db.transfer_items(sender_name, receiver_name, item_quantities)
    if not success:
        await interaction.response.send_message(embed=error_embed(message))
        return
    embed = success_embed("Items Transferred!",
                          f"**{sender_name}** gave {format_items(moved)} to **{receiver_name}**!")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="value", description="Check the sell value of items from your inventory")
@app_commands.describe(name="Character name", items="Item names separated by commas (e.g., Rusty Key, Wild Berries)")
async def value_items(interaction: discord.Interaction, name: str, items: str):
    if not await require_owner(interaction, name):
        return
    item_list = [item.strip() for item in items.split(',') if item.strip()]
    success, message, items_info, total_value = db.get_item_values(name, item_list)
    if not success:
        await interaction.response.send_message(embed=error_embed(message))
        return

    main = CURRENCIES['main']
    lines = []
    for info in items_info[:60]:
        qty = f" ×{info['quantity']}" if info['quantity'] > 1 else ""
        if info['sell_value'] == 0:
            lines.append(f"{info['name']}{qty} · *not sellable*")
        else:
            lines.append(f"{info['name']}{qty} · {info['sell_value']:,} each")
    embed = make_embed(main['emoji'], "Item Values", section(f"{name}'s items", lines),
                       small(f"Total value: {amount_text('main', total_value, bold=False)}"))
    if "not found" in message.lower():
        embed.set_footer(text=f"⚠️ {message}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="sell", description=f"Sell items from your inventory for {MAIN_NAME}")
@app_commands.describe(name="Character name",
                       items="Items to sell, e.g: Rusty Key:3, Wild Berries  (no number = sell 1)")
async def sell_items(interaction: discord.Interaction, name: str, items: str):
    if not await require_owner(interaction, name):
        return
    item_quantities, error = parse_item_quantities(items)
    if error:
        await interaction.response.send_message(embed=error_embed(error, "Invalid input!"), ephemeral=True)
        return
    success, message, items_sold, total_earned = db.sell_items(name, item_quantities)
    if not success:
        await interaction.response.send_message(embed=error_embed(message))
        return

    lines = [f"{i['name']} ×{i['quantity']} · {i['total']:,}" if i['quantity'] > 1 else f"{i['name']} · {i['total']:,}"
             for i in items_sold[:60]]
    embed = success_embed("Items sold!", f"**{name}** earned {amount_text('main', total_earned)}!",
                          section("Sold", lines))
    if any(w in message.lower() for w in ("not found", "no value", "not enough")):
        embed.set_footer(text=f"⚠️ {message}")
    await interaction.response.send_message(embed=embed)


# ==================== MINIGAMES ====================
# One shared implementation; every entry in config.MINIGAMES becomes a command.

def _level_up_line(xp_result):
    """'✨ **Level up!** 1 → 2 · +1 roll bonus' or '' when there was no level-up."""
    if not xp_result or not xp_result['leveled_up']:
        return ""
    bonus_gain = db.level_bonus(xp_result['new_level']) - db.level_bonus(xp_result['old_level'])
    return (f"{EMOJI['level_up']} **Level up!** {xp_result['old_level']} → {xp_result['new_level']}"
            + (f" · +{bonus_gain} roll bonus" if bonus_gain else ""))


def _xp_footer(embed, xp_result):
    if xp_result:
        embed.set_footer(text=f"Level {xp_result['new_level']} · {xp_result['current_xp']}/{xp_result['xp_for_next']} XP")


class MinigameView(discord.ui.View):
    def __init__(self, game, character_name, item_name, result, user_id):
        super().__init__(timeout=60)
        self.user_id = user_id  # only the player who started it may click
        self.game = game
        self.cfg = MINIGAMES[game]
        self.character_name = character_name
        self.item_name = item_name
        self.difficulty = result['difficulty']
        self.flee_message = result['flee_message'] or self.cfg['default_flee'].format(item=item_name)
        # The original find stays visible as a quote at the top of the result
        self.found_quote = quote(result.get('description') or result.get('message'))
        self.fail_message = result['fail_message'] or self.cfg['default_fail'].format(item=item_name)
        self.attempt_button.label = self.cfg['attempt_button']
        self.leave_button.label = self.cfg['leave_button']

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            embed=error_embed(f"Only the player of **{self.character_name}** can decide here!", "Not your turn!"),
            ephemeral=True)
        return False

    async def _finish(self, interaction, embed):
        self.stop()  # ignore further clicks
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Attempt", style=discord.ButtonStyle.danger)
    async def attempt_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        bonus = db.get_level_bonus(self.character_name, self.game)
        roll = random.randint(1, 20)
        success = roll + bonus >= self.difficulty
        xp_reward = config.MINIGAME_XP_BASE + self.difficulty * config.MINIGAME_XP_PER_DIFFICULTY
        roll_text = f"**{self.character_name}** rolled **{roll}**" + (f" +{bonus}" if bonus > 0 else "")

        if success:
            db.add_minigame_item(self.character_name, self.item_name)
            title, text, color = (self.cfg['success_title'], self.cfg['success_text'].format(item=self.item_name),
                                  COLOR_DEFAULT)
            result_text = f"Obtained **{self.item_name}**"
        else:
            xp_reward //= 4
            title, text, color = self.cfg['fail_title'], self.fail_message, COLOR_ERROR
            result_text = "No item obtained"

        xp_result = db.add_xp(self.character_name, xp_reward, self.game)
        embed = make_embed(self.cfg['emoji'], title, self.found_quote, roll_text, text,
                           small(result_text, f"+{xp_reward} XP"), _level_up_line(xp_result), color=color)
        _xp_footer(embed, xp_result)
        await self._finish(interaction, embed)

    @discord.ui.button(label="Leave It", style=discord.ButtonStyle.gray)
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        xp_reward = config.MINIGAME_XP_LEAVE
        xp_result = db.add_xp(self.character_name, xp_reward, self.game)
        leave_text = self.cfg['leave_text'].format(name=self.character_name, item=self.item_name)
        embed = make_embed(self.cfg['emoji'], self.cfg['leave_title'], self.found_quote, leave_text, self.flee_message,
                           small("No item obtained", f"+{xp_reward} XP"), _level_up_line(xp_result))
        _xp_footer(embed, xp_result)
        await self._finish(interaction, embed)


async def run_minigame(interaction, name, game):
    if not await require_owner(interaction, name):
        return
    cfg = MINIGAMES[game]
    result = db.roll_minigame(name, game)
    if result is None:
        await interaction.response.send_message(embed=error_embed(f"Profile **{name}** not found!"))
        return

    item_name = result['item']
    if not item_name:
        await interaction.response.send_message(embed=make_embed(cfg['emoji'], cfg['label'], result['description']))
        return

    profile = db.get_profile(name)
    level = profile[f"{game}_level"] if profile else 1
    embed = make_embed(cfg['emoji'], cfg['label'], result['description'],
                       small(f"{cfg['found_field']} **{item_name}**", f"Difficulty **{result['difficulty']}**"))
    embed.set_footer(text=f"{profile['name'] if profile else name} · Level {level}")
    await interaction.response.send_message(embed=embed, view=MinigameView(game, name, item_name, result, interaction.user.id))


def _register_minigame_command(game, cfg):
    @bot.tree.command(name=game, description=cfg['command_description'])
    @app_commands.describe(name="Character name")
    async def _minigame_command(interaction: discord.Interaction, name: str):
        await run_minigame(interaction, name, game)


for _game, _cfg in MINIGAMES.items():
    _register_minigame_command(_game, _cfg)


# ==================== UTILITY COMMANDS ====================

def _roll_dice(expr):
    """'2d6+3' -> (rolls, modifier, total) or None if invalid / too large."""
    match_ = re.match(r'(\d+)d(\d+)([+-]\d+)?', expr.lower())
    if not match_:
        return None
    num_dice, num_sides = int(match_.group(1)), int(match_.group(2))
    modifier = int(match_.group(3)) if match_.group(3) else 0
    if num_dice < 1 or num_sides < 1 or num_dice > 100 or num_sides > 1000:
        return "too_big"
    rolls = [random.randint(1, num_sides) for _ in range(num_dice)]
    return rolls, modifier, sum(rolls) + modifier


@bot.tree.command(name="roll", description="Roll dice (e.g., 1d20, 2d6)")
@app_commands.describe(dice="Dice notation (e.g., 1d20, 2d6+5)")
async def roll_dice(interaction: discord.Interaction, dice: str):
    result = _roll_dice(dice)
    if result is None:
        await interaction.response.send_message(
            embed=error_embed("Invalid dice format!", "Error!", small("Use e.g. 1d20, 2d6 or 1d20+5")), ephemeral=True)
        return
    if result == "too_big":
        await interaction.response.send_message(
            embed=error_embed("Too many dice or sides! Keep it reasonable."), ephemeral=True)
        return
    rolls, modifier, total = result
    detail = dice.lower().strip()
    if len(rolls) > 1 or modifier != 0:
        detail += f" · rolls {', '.join(map(str, rolls))}" + (f" {modifier:+d}" if modifier else "")
    await interaction.response.send_message(embed=make_embed(EMOJI['dice'], "Roll", f"**{total}**\n{small(detail)}"))


@bot.tree.command(name="choose", description="Pick a random option from a list")
@app_commands.describe(options="Comma-separated list of options")
async def choose(interaction: discord.Interaction, options: str):
    choices = [choice.strip() for choice in options.split(',') if choice.strip()]
    if len(choices) < 2:
        await interaction.response.send_message(
            embed=error_embed("Please give at least 2 options, separated by commas!"), ephemeral=True)
        return
    embed = make_embed(EMOJI['choose'], "Random Choice", f"I choose: **{random.choice(choices)}**")
    embed.set_footer(text=f"Options: {', '.join(choices)}"[:2048])
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="location", description="Travel to a random location")
@app_commands.describe(name="Character name")
async def location(interaction: discord.Interaction, name: str):
    if not await require_owner(interaction, name):
        return
    profile = db.get_profile(name)
    if not profile:
        await interaction.response.send_message(embed=error_embed(f"Profile **{name}** not found!"))
        return
    embed = make_embed(EMOJI['location'], "Location",
                       f"**{profile['name']}** sets off and arrives at **{db.get_random_location()}**.")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="Show all available commands")
async def help_command(interaction: discord.Interaction):
    roles = " / ".join(config.ROLES)
    minigame_lines = [f"`/{game}` · {cfg['command_description']}" for game, cfg in MINIGAMES.items()]
    embed = make_embed(
        EMOJI['profile'], "Commands",
        section("Profile", [
            f"`/create` · New character ({roles})",
            "`/profile` · Profile and inventory",
            "`/list` · All your characters",
            "`/editname` · `/editrole` · Change name or role",
            "`/deleteprofile` · Delete a character",
            "`/claim` · Claim an unowned character"]),
        section("Currency", [
            "`/givecurrency` · Give currency to another character",
            f"`/exchange` · Swap {MAIN_NAME} and {PREMIUM_NAME}"]),
        section("Inventory", [
            "`/buy` · Buy items, e.g. `Lantern:2, Bandage`",
            "`/additem` · `/removeitem` · Add or remove items",
            "`/use` · Use items",
            "`/giveitem` · Give items to another character",
            f"`/value` · `/sell` · Check or sell items for {MAIN_NAME}"]),
        section("Gameplay", [
            "`/match` · Play a match",
            "`/matchreward` · Rewards for escapes or eliminations",
            "`/cnmatch` · Cryptic Notes, alone or in a group of up to 4",
            *minigame_lines,
            "`/location` · Travel somewhere random"]),
        section("Utility", [
            "`/roll` · Roll dice, e.g. `1d20`, `2d6+3`",
            "`/choose` · Pick one of several options"]),
        section("Reservations", [
            f"`/reserve` · Request a roster character in <#{config.RESERVE_CHANNEL_ID}>",
            "`/myreservations` · Your reservations and characters"]))
    embed.set_footer(text="Staff and admin commands are marked [STAFF] / [ADMIN] when you type /")
    await interaction.response.send_message(embed=embed)


# ==================== ADMIN COMMANDS ====================

@bot.tree.command(name="restart", description="[ADMIN] Restart the bot process")
async def restart_bot(interaction: discord.Interaction):
    if not await require_admin(interaction):
        return
    embed = make_embed("🔄", "Restarting...", "Restarting the bot now. It should be back in a few seconds.")
    await interaction.response.send_message(embed=embed)
    print(f"[RESTART] Triggered by {interaction.user} ({interaction.user.id})")
    await bot.close()
    os._exit(0)  # systemd (Restart=always) starts the bot again


# ==================== RESERVATIONS ====================

@bot.tree.command(name="cancelreservation", description="[ADMIN] Cancel an active reservation")
@app_commands.describe(reservation_id="The number shown as 'Reservation #…' under the confirmation")
async def cancel_reservation(interaction: discord.Interaction, reservation_id: int):
    if not await require_admin(interaction):
        return
    success, message = db.delete_reservation(reservation_id)
    embed = success_embed("Reservation Cancelled", message) if success else error_embed(message)
    await interaction.response.send_message(embed=embed, ephemeral=True)
    if success:
        schedule_roster_refresh()


# ==================== ROSTER ====================

_background_tasks = set()


def is_staff(user):
    if user.id in ADMIN_IDS:
        return True
    return any(role.id in config.STAFF_ROLE_IDS for role in getattr(user, 'roles', []))


async def require_staff(interaction):
    if is_staff(interaction.user):
        return True
    await interaction.response.send_message(embed=error_embed("Only staff can do this!", "Access Denied!"),
                                            ephemeral=True)
    return False


def render_roster_part(part):
    """Turn one part from roster_format.build_roster() into message.send()/edit() arguments."""
    if part['style'] == 'embed':
        embed = discord.Embed(title=part['title'] or None, description=part['text'],
                              color=roster_format.parse_color(part['color']))
        if part['footer']:
            embed.set_footer(text=part['footer'])
        if part['thumbnail']:
            embed.set_thumbnail(url=part['thumbnail'])
        if part['image']:
            embed.set_image(url=part['image'])
        return {'content': None, 'embeds': [embed]}
    text = part['text'] + (f"\n\n{part['footer']}" if part['footer'] else "")
    return {'content': text, 'embeds': []}


async def _open_roster_post(channel_id):
    """The forum post for a roster, reopened if archived. Returns (channel, error_text)."""
    channel = await resolve_channel(channel_id)
    if not channel:
        return None, "post not found (check the post ID)."
    if isinstance(channel, discord.ForumChannel):
        return None, "that ID is the forum itself. Use the ID of the post inside the forum."
    if isinstance(channel, discord.Thread) and channel.archived:
        # Forum posts archive themselves after inactivity; archived messages can't be edited
        try:
            await channel.edit(archived=False)
        except discord.HTTPException as e:
            return None, f"couldn't reopen the archived post (give the bot 'Manage Threads'): {e}"
    return channel, None


async def _remove_old_single_roster():
    """Once: delete the messages of the old single roster post (before Hunters/Survivors were split)."""
    old_ids = db.get_old_roster_message_ids()
    if not old_ids:
        return
    if config.ROSTER_CHANNEL_ID:
        channel = await resolve_channel(config.ROSTER_CHANNEL_ID)
        if channel:
            for message_id in old_ids:
                try:
                    await channel.get_partial_message(int(message_id)).delete()
                except discord.HTTPException:
                    pass
    db.clear_old_roster_message_ids()


async def _refresh_role_roster(role):
    """Update one role's roster post. Returns an error text, or None."""
    post_id = roster_format.post_id(db, role)
    if not post_id:
        return "no post ID set on the Roster page."
    channel, error = await _open_roster_post(post_id)
    if error:
        return error

    contents = [render_roster_part(part) for part in roster_format.build_roster(db, role)]
    stored = db.get_roster_message_ids(role)
    no_ping = discord.AllowedMentions.none()  # show mentions without pinging anyone

    if len(stored) == len(contents):
        try:
            for message_id, content in zip(stored, contents):
                await channel.get_partial_message(int(message_id)).edit(**content, allowed_mentions=no_ping)
            return None
        except discord.NotFound:
            pass  # a roster message was deleted (or the post ID changed): post everything again below

    # Number of messages changed, the post changed, or first post: repost all so the order stays right
    for message_id in stored:
        try:
            await channel.get_partial_message(int(message_id)).delete()
        except discord.HTTPException:
            pass
    new_ids = []
    for content in contents:
        new_ids.append((await channel.send(**content, allowed_mentions=no_ping)).id)
    db.set_roster_message_ids(role, new_ids)
    return None


async def refresh_roster(roles=None):
    """Update the roster posts in Discord (default: all roles). Returns an error text, or None."""
    errors = []
    async with bot.roster_lock:
        await _remove_old_single_roster()
        for role in roles or config.ROLES:
            try:
                error = await _refresh_role_roster(role)
            except discord.HTTPException as e:
                error = str(e)
            if error:
                errors.append(f"{role} roster: {error}")
    return " ".join(errors) or None


def schedule_roster_refresh():
    """Update the roster in the background so commands and buttons answer immediately."""
    async def runner():
        try:
            error = await refresh_roster()
            if error:
                print(f"[ROSTER] {error}")
        except Exception as e:
            print(f"[ROSTER] Refresh failed: {e}")
    task = asyncio.create_task(runner())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def roster_autocomplete(statuses, exclude_pending=False):
    """Autocomplete for roster characters, filtered by status (and the optional role option)."""
    async def autocomplete(interaction: discord.Interaction, current: str):
        role = getattr(interaction.namespace, 'role', None)
        characters = db.search_roster(current, statuses=statuses, role=role, exclude_pending=exclude_pending)
        return [app_commands.Choice(name=c['display'][:100], value=str(c['id'])) for c in characters]
    return autocomplete


def linked_identity(roster_id):
    """The linked identity (identity switch) of a roster character, or None."""
    character = db.get_roster_character(roster_id) if roster_id else None
    if character and character['linked_id']:
        return db.get_roster_character(character['linked_id'])
    return None


async def close_staff_message(interaction, decision_text, color):
    """Add the decision to a staff request message and remove its buttons."""
    if interaction.message and interaction.message.embeds:
        embed = interaction.message.embeds[0]
        embed.color = color
        if embed.fields:  # request posted before the new layout: keep its old look
            embed.add_field(name="Decision", value=decision_text, inline=False)
        else:
            add_block(embed, decision_text)
        await interaction.response.edit_message(embed=embed, view=None)
    else:
        await interaction.response.send_message(decision_text, ephemeral=True)


async def handle_approve(interaction, reservation_id, days):
    info = db.get_reservation_info(reservation_id)
    if info and info['status'] == 'pending':
        is_extension = False
        success, message = db.approve_reservation(reservation_id, days, interaction.user.id)
    elif info and info['status'] == 'active' and info['extension_pending']:
        is_extension = True
        success, message = db.approve_extension(reservation_id, days, interaction.user.id)
    else:
        success, message, is_extension = False, "This request was already handled.", False
    if not success:
        await interaction.response.send_message(embed=error_embed(message), ephemeral=True)
        return

    info = db.get_reservation_info(reservation_id)
    label = "Extension approved" if is_extension else "Approved"
    await close_staff_message(interaction, f"✅ {label} by {interaction.user.mention} · {days} day(s)", COLOR_DEFAULT)

    channel = await resolve_channel(int(info['channel_id']))
    if channel:
        expires = info['expires_at']
        description = f"**{info['title']}** is reserved for <@{info['user_id']}>."
        linked = linked_identity(info['roster_id'])
        if linked:
            description += f"\nThis includes the linked identity **{linked['display']}**."
        embed = make_embed("📅", f"Reservation {'Extended' if is_extension else 'Confirmed'}", description,
                           small(f"Expires <t:{expires}:F>", f"<t:{expires}:R>"))
        embed.set_footer(text=f"Reservation #{reservation_id}")
        sent = await channel.send(content=f"<@{info['user_id']}>", embed=embed)
        if not is_extension:
            db.set_reservation_message(reservation_id, sent.id)
    schedule_roster_refresh()


async def handle_deny(interaction, reservation_id, reason):
    success, message, kind = db.deny_request(reservation_id, interaction.user.id)
    if not success:
        await interaction.response.send_message(embed=error_embed(message), ephemeral=True)
        return
    info = db.get_reservation_info(reservation_id)
    is_extension = kind == 'extension'
    reason_text = f"\n**Reason:** {reason}" if reason else ""
    await close_staff_message(
        interaction, f"❌ {'Extension denied' if is_extension else 'Denied'} by {interaction.user.mention}{reason_text}",
        COLOR_ERROR)

    channel = await resolve_channel(int(info['channel_id']))
    if channel:
        if is_extension:
            text = (f"Your extension for **{info['title']}** was not approved. "
                    f"The reservation still runs out <t:{info['expires_at']}:R>.")
        else:
            text = f"Your reservation request for **{info['title']}** was not approved."
        embed = error_embed(text, f"{'Extension' if is_extension else 'Request'} Denied",
                            f"**Reason:** {reason}" if reason else None)
        await channel.send(content=f"<@{info['user_id']}>", embed=embed)


async def handle_extend_click(interaction, reservation_id):
    success, message = db.request_extension(reservation_id, interaction.user.id)
    if not success:
        await interaction.response.send_message(embed=error_embed(message), ephemeral=True)
        return
    info = db.get_reservation_info(reservation_id)
    await interaction.response.edit_message(view=None)  # the Extend button can only be used once

    staff_channel = await resolve_channel(config.STAFF_CHANNEL_ID)
    if staff_channel:
        embed = make_embed("🔁", "Extension Request",
                           f"<@{info['user_id']}> would like to extend **{info['title']}**.",
                           small(f"Currently expires <t:{info['expires_at']}:F>", f"<t:{info['expires_at']}:R>"))
        embed.set_footer(text=f"Reservation #{reservation_id}")
        sent = await staff_channel.send(embed=embed, view=request_view(reservation_id))
        db.set_request_message(reservation_id, sent.id)
    await interaction.followup.send(embed=success_embed("Extension requested!", "Staff will get back to you."),
                                    ephemeral=True)


class ApproveLengthModal(discord.ui.Modal, title="Approve with a custom length"):
    days = discord.ui.TextInput(label=f"Days (1–{config.MAX_RESERVATION_DAYS})",
                                default=str(config.RESERVATION_DAYS), max_length=3)

    def __init__(self, reservation_id):
        super().__init__()
        self.reservation_id = reservation_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            days = int(self.days.value)
        except ValueError:
            days = 0
        if not 1 <= days <= config.MAX_RESERVATION_DAYS:
            await interaction.response.send_message(
                embed=error_embed(f"Please enter a number from 1 to {config.MAX_RESERVATION_DAYS}."), ephemeral=True)
            return
        await handle_approve(interaction, self.reservation_id, days)


class DenyModal(discord.ui.Modal, title="Deny request"):
    reason = discord.ui.TextInput(label="Reason (optional, the member sees it)", style=discord.TextStyle.paragraph,
                                  required=False, max_length=500)

    def __init__(self, reservation_id):
        super().__init__()
        self.reservation_id = reservation_id

    async def on_submit(self, interaction: discord.Interaction):
        await handle_deny(interaction, self.reservation_id, self.reason.value.strip())


RESERVATION_BUTTONS = {
    'approve': (f"Approve ({config.RESERVATION_DAYS} days)", discord.ButtonStyle.success),
    'custom': ("Approve (custom length)", discord.ButtonStyle.primary),
    'deny': ("Deny", discord.ButtonStyle.danger),
    'extend': ("Extend", discord.ButtonStyle.primary),
}


class ReservationButton(discord.ui.DynamicItem[discord.ui.Button],
                        template=r'resv:(?P<action>approve|custom|deny|extend):(?P<id>[0-9]+)'):
    """Buttons on request and reminder messages. The reservation ID is part of the button,
    so they keep working after the bot restarts."""

    def __init__(self, action, reservation_id):
        label, style = RESERVATION_BUTTONS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"resv:{action}:{reservation_id}"))
        self.action = action
        self.reservation_id = reservation_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match['action'], int(match['id']))

    async def callback(self, interaction: discord.Interaction):
        if self.action == 'extend':
            await handle_extend_click(interaction, self.reservation_id)
            return
        if not await require_staff(interaction):
            return
        if self.action == 'approve':
            await handle_approve(interaction, self.reservation_id, config.RESERVATION_DAYS)
        elif self.action == 'custom':
            await interaction.response.send_modal(ApproveLengthModal(self.reservation_id))
        else:
            await interaction.response.send_modal(DenyModal(self.reservation_id))


def request_view(reservation_id):
    view = discord.ui.View(timeout=None)
    for action in ('approve', 'custom', 'deny'):
        view.add_item(ReservationButton(action, reservation_id))
    return view


class ReleasePlayerButton(discord.ui.DynamicItem[discord.ui.Button], template=r'roster:release:(?P<user>[0-9]+)'):
    """'Release all' button on the alert when a member leaves the server."""

    def __init__(self, user_id):
        super().__init__(discord.ui.Button(label="Release all their characters", style=discord.ButtonStyle.danger,
                                           custom_id=f"roster:release:{user_id}"))
        self.user_id = user_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match['user']))

    async def callback(self, interaction: discord.Interaction):
        if not await require_staff(interaction):
            return
        names = db.release_player(self.user_id)
        released = ", ".join(names) if names else "nothing left to release"
        await close_staff_message(interaction, f"Released by {interaction.user.mention}: {released}", COLOR_DEFAULT)
        schedule_roster_refresh()


@bot.tree.command(name="reserve", description="Request to reserve a character from the roster")
@app_commands.describe(character="Start typing a name or title", role="Only suggest Hunters or Survivors",
                       note="Anything staff should know (optional)")
@app_commands.choices(role=ROLE_CHOICES)
@app_commands.autocomplete(character=roster_autocomplete(('free',), exclude_pending=True))
async def reserve(interaction: discord.Interaction, character: str,
                  role: app_commands.Choice[str] = None, note: str = None):
    if interaction.channel_id != config.RESERVE_CHANNEL_ID:
        await interaction.response.send_message(
            embed=error_embed(f"Please use this command in <#{config.RESERVE_CHANNEL_ID}>."), ephemeral=True)
        return
    found = db.find_roster_character(character)
    if not found:
        await interaction.response.send_message(
            embed=error_embed(f"**{character}** isn't on the roster. Please pick one of the suggestions."),
            ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    success, message, reservation_id = db.create_reservation_request(
        interaction.guild_id, interaction.channel_id, interaction.user.id, interaction.user.display_name,
        found['id'], note)
    if not success:
        await interaction.followup.send(embed=error_embed(message), ephemeral=True)
        return

    staff_channel = await resolve_channel(config.STAFF_CHANNEL_ID)
    if not staff_channel:
        db.cancel_request(reservation_id)
        await interaction.followup.send(embed=error_embed("The staff channel wasn't found. Please tell an admin."),
                                        ephemeral=True)
        return

    active = len(db.get_member_reservations(interaction.user.id)['active'])
    linked = linked_identity(found['id'])
    embed = make_embed(
        "📨", "Reservation Request",
        f"{interaction.user.mention} ({interaction.user.display_name}) requests "
        f"**{found['display']}** ({found['role']})",
        small(f"Linked: {linked['display']} ({linked['role']}), included" if linked else "",
              f"{active}/{config.MAX_RESERVATIONS_PER_MEMBER} active reservations"),
        f"*{note[:1500]}*" if note else None)
    embed.set_footer(text=f"Request #{reservation_id}")
    sent = await staff_channel.send(embed=embed, view=request_view(reservation_id))
    db.set_request_message(reservation_id, sent.id)

    included = f" (including **{linked['display']}**)" if linked else ""
    await interaction.followup.send(
        embed=success_embed("Request sent!", f"Your request for **{found['display']}**{included} was sent to staff. "
                                             f"You'll be pinged here once it's decided."),
        ephemeral=True)


@bot.tree.command(name="myreservations", description="Show your reservations and characters")
async def my_reservations(interaction: discord.Interaction):
    data = db.get_member_reservations(interaction.user.id)
    embed = make_embed(
        "📅", "Your Reservations",
        section("Active", [f"**{r['title']}** · expires <t:{r['expires_at']}:R>"
                           + (" · extension requested" if r['extension_pending'] else "")
                           for r in data['active']] or ["*None*"]),
        section("Waiting for staff", [f"**{r['title']}**" for r in data['pending']] or ["*None*"]),
        section("Your characters", [f"**{c['display']}**" for c in data['taken']] or ["*None*"]))
    embed.description = embed.description[:4096]
    embed.set_footer(text=f"You can have up to {config.MAX_RESERVATIONS_PER_MEMBER} reservations at a time.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="approvecharacter",
                  description="[STAFF] Submission accepted: mark a character as taken and end its reservation")
@app_commands.describe(character="The reserved character", member="Only needed if the character wasn't reserved")
@app_commands.autocomplete(character=roster_autocomplete(('reserved', 'free')))
async def approve_character(interaction: discord.Interaction, character: str, member: discord.Member = None):
    if not await require_staff(interaction):
        return
    found = db.find_roster_character(character)
    if not found:
        await interaction.response.send_message(embed=error_embed("Character not found on the roster."), ephemeral=True)
        return
    success, message = db.set_roster_taken(found['id'], member.id if member else None,
                                           member.display_name if member else None)
    if not success:
        await interaction.response.send_message(embed=error_embed(message), ephemeral=True)
        return
    player_id = member.id if member else found['player_id']
    await interaction.response.send_message(embed=success_embed(
        "Character approved!", f"**{found['display']}** is now taken by <@{player_id}>. "
                               f"Their reservation has been closed."))
    schedule_roster_refresh()


@bot.tree.command(name="releasecharacter", description="[STAFF] Make a reserved or taken character free again")
@app_commands.describe(character="The character to release")
@app_commands.autocomplete(character=roster_autocomplete(('reserved', 'taken')))
async def release_character(interaction: discord.Interaction, character: str):
    if not await require_staff(interaction):
        return
    found = db.find_roster_character(character)
    if not found:
        await interaction.response.send_message(embed=error_embed("Character not found on the roster."), ephemeral=True)
        return
    success, message = db.release_roster_character(found['id'])
    embed = success_embed("Character released!", message) if success else error_embed(message)
    await interaction.response.send_message(embed=embed)
    if success:
        schedule_roster_refresh()


# ==================== EVENTS ====================

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    if not getattr(bot, 'stickies_checked', False):  # on_ready can fire again after reconnects
        bot.stickies_checked = True
        for sticky in db.get_stickies():  # messages sent while the bot was offline
            schedule_sticky(int(sticky['channel_id']), delay=0)


# ==================== STICKY MESSAGES ====================

_sticky_timers = {}   # channel_id -> waiting task (restarted by every new message)
_sticky_locks = {}    # channel_id -> lock, so a channel is never reposted twice at once


def _sticky_kwargs(sticky):
    """content / embed for channel.send(); None if there's nothing left to post."""
    kwargs = {}
    if sticky['content']:
        kwargs['content'] = sticky['content']
    if sticky['embed_id']:
        data = db.get_sticky_embed(sticky['embed_id'])
        if data:
            kwargs['embed'] = build_embed(data)
    return kwargs or None


async def repost_sticky(channel_id, force=False):
    """Move the sticky to the bottom of the channel (unless it's already the last message).
    Returns an error text or None."""
    lock = _sticky_locks.setdefault(channel_id, asyncio.Lock())
    async with lock:
        sticky = db.get_sticky(channel_id)
        if not sticky:
            return "No sticky message for this channel."
        channel = await resolve_channel(channel_id)
        if not channel:
            return "Channel not found. Check the ID and that the bot can see the channel."
        old_id = int(sticky['message_id']) if sticky['message_id'] else None
        if not force and old_id and channel.last_message_id == old_id:
            return None  # already at the bottom
        kwargs = _sticky_kwargs(sticky)
        if not kwargs:
            return "The saved embed of this sticky was deleted. Choose another one or add text."
        message = await channel.send(**kwargs, allowed_mentions=discord.AllowedMentions.none())
        db.set_sticky_message(channel_id, message.id)
        if old_id:
            try:
                await channel.get_partial_message(old_id).delete()
            except discord.HTTPException:
                pass  # already deleted by someone
        return None


def schedule_sticky(channel_id, delay=None):
    """(Re)start the quiet timer of a channel; the sticky moves down when it runs out."""
    old = _sticky_timers.get(channel_id)
    if old and not old.done():
        old.cancel()

    async def wait_then_repost():
        await asyncio.sleep(config.STICKY_DELAY_SECONDS if delay is None else delay)
        _sticky_timers.pop(channel_id, None)
        try:
            # shield: a new message must not cancel a repost that is already happening
            error = await asyncio.shield(repost_sticky(channel_id))
            if error:
                print(f"[STICKY] #{channel_id}: {error}")
        except Exception as e:
            print(f"[STICKY] #{channel_id}: {e}")

    _sticky_timers[channel_id] = asyncio.create_task(wait_then_repost())


@bot.event
async def on_message(message):
    if not message.guild:
        return
    sticky = db.get_sticky(message.channel.id)
    if not sticky or str(message.id) == sticky['message_id']:
        return  # no sticky here, or it's the sticky itself
    # Even if the sticky's own message lands here before its id is saved, the repost
    # sees it's already the last message and does nothing, so this can't loop.
    schedule_sticky(message.channel.id)


@bot.tree.command(name="testwelcome", description="[ADMIN] Test the welcome message system")
async def test_welcome(interaction: discord.Interaction):
    if not await require_admin(interaction):
        return
    print(f"[TEST] Testing welcome message triggered by {interaction.user.name}")
    await interaction.response.send_message("Testing welcome message...", ephemeral=True)
    await on_member_join(interaction.user)


@bot.event
async def on_member_join(member):
    """Send the welcome embed configured in the dashboard."""
    print(f"[WELCOME] Member joined: {member.name} (ID: {member.id})")
    try:
        welcome = db.get_welcome_embed()
        if not welcome:
            print("[WELCOME] Disabled or not configured")
            return
        (title, description, color, footer, image_url, thumbnail_url), channel_id = welcome

        replacements = {
            '{user}': member.name,
            '{mention}': member.mention,
            '{member_count}': str(member.guild.member_count),
            '{server}': member.guild.name,
            '{user_tag}': str(member),
            '{user_id}': str(member.id),
        }

        def replace_vars(text):
            if not text:
                return text
            for key, value in replacements.items():
                text = text.replace(key, value)
            return text

        embed = discord.Embed(title=replace_vars(title), description=replace_vars(description),
                              color=int(color or '000000', 16))
        if footer:
            embed.set_footer(text=replace_vars(footer))
        if image_url:
            embed.set_image(url=image_url)
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)

        channel = await resolve_channel(int(channel_id))
        if channel:
            message = await channel.send(embed=embed)
            print(f"[WELCOME] ✅ Sent welcome message! Message ID: {message.id}")
        else:
            print(f"[WELCOME] ❌ Channel not found! ID: {channel_id}")
    except Exception as e:
        print(f"[WELCOME] ❌ Error: {e}")
        import traceback
        traceback.print_exc()


@bot.event
async def on_member_remove(member):
    """Alert staff when someone with roster characters or open requests leaves."""
    try:
        characters = db.characters_of_player(member.id)
        pending = db.get_member_reservations(member.id)['pending']
        if not characters and not pending:
            return
        staff_channel = await resolve_channel(config.STAFF_CHANNEL_ID)
        if not staff_channel:
            return
        lines = [f"**{c['display']}** · {c['status']}" for c in characters]
        lines += [f"**{r['title']}** · request waiting for staff" for r in pending]
        embed = make_embed("👋", "Member Left", f"**{member}** ({member.mention}) left the server.",
                           section("They had", lines), color=COLOR_ERROR)
        embed.set_footer(text="Nothing was changed. Use the button to free their characters.")
        view = discord.ui.View(timeout=None)
        view.add_item(ReleasePlayerButton(member.id))
        await staff_channel.send(embed=embed, view=view)
    except Exception as e:
        print(f"[LEAVE] Error: {e}")


async def _reaction_role_target(payload):
    """(member, role) for a reaction on a reaction-role message, else None."""
    role_id = db.get_reaction_role(payload.message_id, str(payload.emoji))
    if not role_id:
        return None
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return None
    member = guild.get_member(payload.user_id)
    role = guild.get_role(role_id)
    if not member or not role:
        return None
    return member, role


@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return
    try:
        target = await _reaction_role_target(payload)
        if target:
            await target[0].add_roles(target[1])
    except Exception as e:
        print(f"[REACTION ROLE] Error: {e}")


@bot.event
async def on_raw_reaction_remove(payload):
    try:
        target = await _reaction_role_target(payload)
        if target:
            await target[0].remove_roles(target[1])
    except Exception as e:
        print(f"[REACTION ROLE] Error: {e}")


# ==================== BACKGROUND TASKS ====================

_status_cycle = itertools.cycle([
    discord.Activity(type=getattr(discord.ActivityType, kind), name=text)
    for kind, text in config.STATUS_MESSAGES
])


@tasks.loop(hours=config.STATUS_ROTATION_HOURS)
async def rotate_status():
    await bot.change_presence(status=discord.Status.dnd, activity=next(_status_cycle))


@rotate_status.before_loop
async def _before_rotate_status():
    await bot.wait_until_ready()


@tasks.loop(minutes=1)
async def check_reservations():
    # 1) "Ends soon" reminder with an Extend button
    for info in db.get_due_reminders():
        message_id = None
        try:
            channel = await resolve_channel(int(info['channel_id']))
            if channel:
                embed = make_embed("⏳", "Reservation Ending Soon",
                                   f"Your reservation for **{info['title']}** runs out <t:{info['expires_at']}:R>.",
                                   small("Need more time? Click Extend to ask staff"))
                view = discord.ui.View(timeout=None)
                view.add_item(ReservationButton('extend', info['id']))
                message_id = (await channel.send(content=f"<@{info['user_id']}>", embed=embed, view=view)).id
        except Exception as e:
            print(f"Failed to send reservation reminder for #{info['id']}: {e}")
        finally:
            db.mark_reminded(info['id'], message_id)  # never remind twice, even if sending failed

    # 2) Expired reservations: ping the member and free the character
    roster_changed = False
    for row in db.get_due_reservations():
        info = db.expire_reservation(row[0])
        if not info:
            continue
        is_roster = bool(info['roster_id'])
        roster_changed = roster_changed or is_roster
        try:
            channel = await resolve_channel(int(info['channel_id']))
            if not channel:
                continue
            if info['reminder_message_id']:  # the Extend button is no longer useful
                try:
                    await channel.get_partial_message(int(info['reminder_message_id'])).edit(view=None)
                except discord.HTTPException:
                    pass
            embed = error_embed(f"Your reservation for **{info['title'] or ''}** has run out.", "Reservation Expired",
                                small("The character is free again") if is_roster else None)
            await channel.send(content=f"<@{info['user_id']}>", embed=embed)
        except Exception as e:
            print(f"Failed to send reservation expiry notice for #{info['id']}: {e}")
    if roster_changed:
        schedule_roster_refresh()


@check_reservations.before_loop
async def _before_check_reservations():
    await bot.wait_until_ready()  # channel cache must be filled first


@tasks.loop(hours=24)
async def daily_checkpoint():
    """Keep the WAL file small. Runs in a thread so the bot never blocks on it."""
    try:
        await asyncio.to_thread(db.checkpoint)
        print("[DB] Daily WAL checkpoint complete")
    except Exception as e:
        print(f"[DB] checkpoint error: {e}")


# ==================== INTERNAL API (used by the dashboard) ====================

def run_on_bot(coro, timeout=10):
    """Run a coroutine on the bot's event loop from a Flask thread and wait for the result."""
    return asyncio.run_coroutine_threadsafe(coro, bot.loop).result(timeout=timeout)


def build_embed(data):
    embed = discord.Embed(title=data.get('title'), description=data.get('description'),
                          color=int(data.get('color') or '000000', 16))
    if data.get('footer_text'):
        embed.set_footer(text=data['footer_text'])
    if data.get('image_url'):
        embed.set_image(url=data['image_url'])
    if data.get('thumbnail_url'):
        embed.set_thumbnail(url=data['thumbnail_url'])
    return embed


async def _add_reactions(message, reaction_roles):
    for rr in reaction_roles:
        try:
            await message.add_reaction(rr['emoji'])
        except Exception as e:
            print(f"Failed to add reaction {rr['emoji']}: {e}")


@api.route('/send_embed', methods=['POST'])
def api_send_embed():
    try:
        data = request.json
        channel_id = int(data['channel_id'])
        embed = build_embed(data)

        async def send():
            channel = await resolve_channel(channel_id)
            if not channel:
                return None
            message = await channel.send(embed=embed)
            await _add_reactions(message, data.get('reaction_roles', []))
            return message.id

        message_id = run_on_bot(send())
        if message_id:
            return jsonify({'status': 'success', 'message_id': str(message_id)}), 200
        return jsonify({'status': 'error', 'message': 'Channel not found'}), 404
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@api.route('/update_embed', methods=['POST'])
def api_update_embed():
    try:
        data = request.json
        channel_id, message_id = int(data['channel_id']), int(data['message_id'])
        embed = build_embed(data)

        async def update():
            channel = await resolve_channel(channel_id)
            if not channel:
                return False
            message = await channel.fetch_message(message_id)
            await message.edit(embed=embed)
            return True

        if run_on_bot(update()):
            return jsonify({'status': 'success'}), 200
        return jsonify({'status': 'error', 'message': 'Channel or message not found'}), 404
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@api.route('/update_reactions', methods=['POST'])
def api_update_reactions():
    """Clear the reactions on a message and re-add the current reaction-role emojis."""
    try:
        data = request.json
        channel_id, message_id = int(data['channel_id']), int(data['message_id'])

        async def update():
            channel = await resolve_channel(channel_id)
            if not channel:
                return False
            message = await channel.fetch_message(message_id)
            await message.clear_reactions()
            await _add_reactions(message, data.get('reaction_roles', []))
            return True

        if run_on_bot(update(), timeout=15):
            return jsonify({'status': 'success'}), 200
        return jsonify({'status': 'error', 'message': 'Channel or message not found'}), 404
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@api.route('/upload_image', methods=['POST'])
def api_upload_image():
    """Upload an image to the UPLOAD_CHANNEL_ID channel and return its Discord CDN URL."""
    try:
        file = request.files.get('file')
        if not file or not file.filename:
            return jsonify({'status': 'error', 'message': 'No file provided'}), 400
        upload_channel_id = int(os.getenv('UPLOAD_CHANNEL_ID', '0'))
        if not upload_channel_id:
            return jsonify({'status': 'error', 'message': 'Upload channel not configured (UPLOAD_CHANNEL_ID)'}), 500
        file_data = file.read()

        async def upload():
            channel = await resolve_channel(upload_channel_id)
            if not channel:
                return None
            message = await channel.send(file=discord.File(io.BytesIO(file_data), filename=file.filename))
            return message.attachments[0].url if message.attachments else None

        image_url = run_on_bot(upload())
        if image_url:
            return jsonify({'status': 'success', 'url': image_url}), 200
        return jsonify({'status': 'error', 'message': 'Failed to upload'}), 500
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@api.route('/member_names', methods=['POST'])
def api_member_names():
    """{user_id: current display name} for the dashboard's 'Played by' columns."""
    try:
        ids = [str(i) for i in (request.json or {}).get('ids', []) if str(i).isdigit()]

        async def lookup():
            guild = bot.get_guild(int(config.GUILD_ID)) if str(config.GUILD_ID).isdigit() else None
            guilds = [guild] if guild else bot.guilds
            names = {}
            for user_id in ids:
                for g in guilds:
                    member = g.get_member(int(user_id))
                    if member:
                        names[user_id] = member.display_name
                        break
            return names

        return jsonify(run_on_bot(lookup())), 200
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@api.route('/refresh_roster', methods=['POST'])
def api_refresh_roster():
    """Called by the dashboard after every roster change. Optional JSON {"role": "Hunter"}."""
    try:
        role = (request.get_json(silent=True) or {}).get('role')
        roles = [role] if role in config.ROLES else None
        error = run_on_bot(refresh_roster(roles), timeout=60)
        if error:
            return jsonify({'status': 'error', 'message': error}), 400
        return jsonify({'status': 'success'}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@api.route('/sticky_post', methods=['POST'])
def api_sticky_post():
    """Post (or re-post) a channel's sticky right away, after it was saved on the dashboard."""
    try:
        channel_id = int((request.get_json(silent=True) or {}).get('channel_id'))
        error = run_on_bot(repost_sticky(channel_id, force=True))
        if error:
            return jsonify({'status': 'error', 'message': error}), 400
        return jsonify({'status': 'success'}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@api.route('/sticky_remove', methods=['POST'])
def api_sticky_remove():
    """Delete the posted sticky of a removed sticky message."""
    try:
        data = request.get_json(silent=True) or {}
        channel_id, message_id = int(data['channel_id']), data.get('message_id')

        async def remove():
            timer = _sticky_timers.pop(channel_id, None)
            if timer:
                timer.cancel()
            channel = await resolve_channel(channel_id)
            if channel and message_id:
                try:
                    await channel.get_partial_message(int(message_id)).delete()
                except discord.NotFound:
                    pass

        run_on_bot(remove())
        return jsonify({'status': 'success'}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


def run_api():
    # Bound to 127.0.0.1: only the dashboard on this machine can reach it
    api.run(host=config.BOT_API_HOST, port=config.BOT_API_PORT, debug=False)


# ==================== START ====================

if __name__ == "__main__":
    TOKEN = os.getenv('DISCORD_TOKEN')
    if not TOKEN:
        print("ERROR: Discord Token not found! Check .env file")
    else:
        threading.Thread(target=run_api, daemon=True).start()
        print(f"🌐 API server started on {config.BOT_API_HOST}:{config.BOT_API_PORT}")
        bot.run(TOKEN)
