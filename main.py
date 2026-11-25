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
from config import EMOJI, CURRENCIES, MINIGAMES, currency_label
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

def error_embed(description, title="Error!"):
    return discord.Embed(title=f"{EMOJI['error']} ┃ {title}", description=description, color=COLOR_ERROR)


def success_embed(title, description=None):
    return discord.Embed(title=f"{EMOJI['success']} ┃ {title}", description=description, color=COLOR_DEFAULT)


def amount_text(key, amount):
    """'🔍 **1,000** Clues'"""
    c = CURRENCIES[key]
    return f"{c['emoji']} **{amount:,}** {c['name']}"


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
        embed=error_embed("❌ Only admins can use this command!", "Access Denied!"), ephemeral=True)
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
    """{'A': 2, 'B': 1} -> '**A** x2, **B**'"""
    return ", ".join(f"**{name}** x{qty}" if qty > 1 else f"**{name}**" for name, qty in item_quantities.items())


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
        embed = success_embed("Profile Created!", f"Successfully created profile for **{name}** as **{role.value}**!")
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


class ProfileView(discord.ui.View):
    """Profile with 'Main Info' / 'Inventory' pages and an NSFW toggle."""

    def __init__(self, profile_data, inventory_data):
        super().__init__(timeout=180)
        self.profile_data = profile_data
        self.inventory_data = inventory_data
        self.current_page = 0
        self.show_nsfw = False

    @discord.ui.button(label="Main Info", style=discord.ButtonStyle.danger)
    async def main_info_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = 0
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="Inventory", style=discord.ButtonStyle.secondary)
    async def inventory_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = 1
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="🔞 Show NSFW", style=discord.ButtonStyle.secondary)
    async def toggle_nsfw_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.show_nsfw = not self.show_nsfw
        button.label = "👁️ Hide NSFW" if self.show_nsfw else "🔞 Show NSFW"
        button.style = discord.ButtonStyle.danger if self.show_nsfw else discord.ButtonStyle.secondary
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    def create_embed(self):
        p = self.profile_data
        if self.current_page == 0:
            embed = discord.Embed(color=COLOR_DEFAULT)
            embed.add_field(name=f"{EMOJI['profile']} ┃ **{p['name']}**'s Profile", value="", inline=False)
            embed.add_field(name=".✦ Role:", value=p['role'], inline=True)
            levels_text = "\n".join(
                f"{cfg['emoji']} **Lv.{p[f'{game}_level']}** {_progress_bar(p[f'{game}_level'], p[f'{game}_xp'])}"
                for game, cfg in MINIGAMES.items())
            embed.add_field(name=".✦ Levels:", value=levels_text or "—", inline=True)
            currency_text = "\n".join(f"{c['emoji']} {p[key]:,} {c['name']}" for key, c in CURRENCIES.items())
            embed.add_field(name=".✦ Currency:", value=currency_text, inline=True)
            return embed

        embed = discord.Embed(title=f"{EMOJI['profile']} ┃ **{p['name']}**'s Inventory", color=COLOR_DEFAULT)
        total_items = sum(item['quantity'] for items in self.inventory_data.values() for item in items)
        if not total_items:
            embed.description = "*Inventory is empty*\n\nUse `/buy` to purchase items or play minigames to collect loot!"
            return embed
        for category, items in self.inventory_data.items():
            if not items:
                continue
            if category == 'NSFW' and not self.show_nsfw:
                value = f"*{len(items)} NSFW item(s) hidden. Click '🔞 Show NSFW' to reveal.*"
            elif category == 'NSFW':
                value = "\n".join(f"||{item['item_name']}|| ― `{item['quantity']}`" for item in items)
            else:
                value = "\n".join(f"{item['item_name']} ― `{item['quantity']}`" for item in items)
            embed.add_field(name=f"·········•✦ {category}", value=value[:1024], inline=False)
        embed.set_footer(text=f"Total: {total_items} items")
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
    title = f"{EMOJI['profile']} ┃ Your Characters"
    if not profiles:
        embed = discord.Embed(title=title, description="You don't have any characters yet. Create one with `/create`!",
                              color=COLOR_DEFAULT)
        await interaction.response.send_message(embed=embed)
        return

    main_emoji, premium_emoji = CURRENCIES['main']['emoji'], CURRENCIES['premium']['emoji']

    def chunk_lines(lines, limit=1024):
        """Split lines into chunks that each fit into one embed field."""
        chunks, current, length = [], [], 0
        for line in lines:
            if current and length + len(line) + 1 > limit:
                chunks.append("\n".join(current))
                current, length = [], 0
            current.append(line)
            length += len(line) + 1
        if current:
            chunks.append("\n".join(current))
        return chunks

    # Group by role: configured roles first, then anything else
    roles = list(config.ROLES) + sorted({p[1] for p in profiles} - set(config.ROLES))
    fields = []
    for role in roles:
        lines = [f"{EMOJI.get(role, '•')} **{p[0]}** — {main_emoji} {p[2]:,} / {premium_emoji} {p[3]:,}"
                 for p in profiles if p[1] == role]
        for i, chunk in enumerate(chunk_lines(lines)):
            fields.append((f"{role}s" if i == 0 else f"{role}s (cont.)", chunk))

    pages = [fields[i:i + 25] for i in range(0, max(len(fields), 1), 25)]  # max 25 fields per embed

    def make_embed(page_num):
        embed = discord.Embed(title=title, description=f"You have **{len(profiles)}** character(s).",
                              color=COLOR_DEFAULT)
        for field_name, value in pages[page_num]:
            embed.add_field(name=field_name, value=value, inline=False)
        footer = "Use /profile [name] to view details."
        if len(pages) > 1:
            footer += f"  •  Page {page_num + 1}/{len(pages)}"
        embed.set_footer(text=footer)
        return embed

    if len(pages) == 1:
        await interaction.response.send_message(embed=make_embed(0))
        return

    class ListView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            self.page = 0

        @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
        async def prev_btn(self, inter: discord.Interaction, button: discord.ui.Button):
            self.page = max(0, self.page - 1)
            await inter.response.edit_message(embed=make_embed(self.page), view=self)

        @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
        async def next_btn(self, inter: discord.Interaction, button: discord.ui.Button):
            self.page = min(len(pages) - 1, self.page + 1)
            await inter.response.edit_message(embed=make_embed(self.page), view=self)

    await interaction.response.send_message(embed=make_embed(0), view=ListView())


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
        embed = success_embed("Owner assigned!", f"**{character}** is now owned by {user.mention}\n\n{message}")
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
    embed = success_embed("Exchange Complete!", f"**{name}** exchanged currency!")
    embed.add_field(name="Received", value=amount_text(to_currency, received), inline=False)
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
    embed = discord.Embed(title=f"{EMOJI['match']} ┃ {name}'s {result['role']} Match",
                          description=result['message'], color=COLOR_ERROR)
    embed.add_field(name="Result:", value=result['performance_text'], inline=True)
    embed.add_field(name="Rewards",
                    value=f"{amount_text('main', result['main'])}\n{amount_text('premium', result['premium'])}",
                    inline=True)
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
    embed = success_embed("Match Rewards Granted!", f"**{name}** received their match rewards!")
    embed.add_field(name="Rewards",
                    value=f"{amount_text('main', main_reward)}\n{amount_text('premium', premium_reward)}",
                    inline=False)
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


def _cn_items_text(result):
    """'⚪ **Pickaxe** ×2 · 150 each' lines plus the total, for one character's result."""
    counted = {}
    for item in result['items']:
        entry = counted.setdefault(item['name'], [item, 0])
        entry[1] += 1
    lines = []
    for name, (item, qty) in sorted(counted.items(), key=lambda kv: -kv[1][0]['sell_value']):
        tier = config.CN_TIERS.get(item['tier'], {})
        lines.append(f"{tier.get('emoji', '•')} **{name}**{f' ×{qty}' if qty > 1 else ''} · "
                     f"{tier.get('label', item['tier'])} · {item['sell_value']:,} each")
    lines.append(f"**Total value:** {amount_text('main', result['total_value'])}")
    if result['reward']:
        lines.append(f"**Survival bonus:** {amount_text('main', result['reward'])}")
    return "\n".join(lines)


def _cn_exhaustion_text(result):
    if result['runs_this_hour'] < config.CN_EXHAUSTION_RUNS:
        return ""
    text = (f"{config.CN_ICONS['exhausted']} **{result['character']}** is exhausted "
            f"({result['runs_this_hour']} runs within the hour). "
            f"Another run could easily end in death.")
    if result['exhausted']:
        text += " The exhaustion already made this run more dangerous."
    return text


def run_cn_group(cn_map, names):
    """Run Cryptic Notes for one or more characters (same anomaly for everyone) and build the result embed."""
    anomaly = db.pick_cn_anomaly(cn_map['id'])
    results = [r for r in (db.run_cryptic_notes(n, cn_map, anomaly) for n in names) if r]
    all_dead = bool(results) and all(r['died'] for r in results)
    embed = discord.Embed(title=f"{config.CN_EMOJI} ┃ Cryptic Notes · {cn_map['name']}",
                          color=COLOR_ERROR if all_dead else COLOR_DEFAULT)
    icons = config.CN_ICONS
    anomaly_line = (f"**Anomaly:** {anomaly['name']} · danger "
                    f"{icons['danger_full'] * anomaly['danger']}{icons['danger_empty'] * (5 - anomaly['danger'])}"
                    if anomaly else "")

    if len(results) == 1:  # solo run: the story is the main text
        r = results[0]
        embed.description = f"{anomaly_line}\n\n{r['message']}".strip()[:4000]
        if r['died']:
            embed.add_field(name=f"{icons['died']} {CN_SHORT[r['outcome']]}", value=config.CN_RESPAWN_TEXT,
                            inline=False)
        else:
            embed.add_field(name=f"Classics found ({len(r['items'])})", value=_cn_items_text(r)[:1024], inline=False)
        if _cn_exhaustion_text(r):
            embed.add_field(name="Exhausted", value=_cn_exhaustion_text(r), inline=False)
        return embed

    embed.description = anomaly_line or None
    for r in results:  # group run: one section per character
        icon = icons['died'] if r['died'] else icons['survived']
        body = r['message'] + "\n" + (config.CN_RESPAWN_TEXT if r['died'] else _cn_items_text(r))
        if _cn_exhaustion_text(r):
            body += "\n" + _cn_exhaustion_text(r)
        embed.add_field(name=f"{icon} {r['character']} · {CN_SHORT[r['outcome']]}", value=body[:1024], inline=False)
    survivors = sum(1 for r in results if not r['died'])
    embed.set_footer(text=f"{survivors}/{len(results)} made it out")
    return embed


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

    def invite_embed(self):
        lines = "\n".join(f"{self.ICONS[self.answers[n]]} **{n}** · <@{self.owners[n]}>" for n in self.members)
        embed = discord.Embed(
            title=f"{config.CN_EMOJI} ┃ Cryptic Notes · {self.cn_map['name']}",
            description=(f"<@{self.leader_id}> wants to enter **{self.cn_map['name']}** as a group.\n"
                         f"Each invited player has to accept for their own character.\n\n{lines}"),
            color=COLOR_DEFAULT)
        embed.set_footer(text=f"Invite expires in {config.CN_INVITE_TIMEOUT // 60} minutes · "
                              f"the leader can start early with everyone who accepted")
        return embed

    async def start(self, interaction):
        if self.done:
            return
        self.done = True
        self.stop()
        await interaction.response.edit_message(content=None, embed=run_cn_group(self.cn_map, self.team()), view=None)

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
        embed = self.invite_embed()
        embed.description += "\n\n**Cancelled by the leader.**"
        embed.set_footer(text=None)
        await interaction.response.edit_message(content=None, embed=embed, view=None)

    async def on_timeout(self):
        if self.done or not self.message:
            return
        self.done = True
        embed = self.invite_embed()
        embed.description += "\n\n**The invite expired.** Use /cnmatch again to try once more."
        embed.set_footer(text=None)
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
        await interaction.response.send_message(embed=run_cn_group(cn_map, [leader['name']]))
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
        await interaction.response.send_message(embed=run_cn_group(cn_map, view.team()))
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
    embed = success_embed("Purchase successful!",
                          f"**{name}** purchased {format_items(purchased)} for {amount_text(currency, total_price)}!")
    total_items = sum(purchased.values())
    if total_items > 1:
        embed.add_field(name="Total Items", value=f"{total_items} items", inline=True)
        embed.add_field(name="Total Cost", value=f"{total_price:,} {CURRENCIES[currency]['name']}", inline=True)
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
    embed = success_embed("Items added!", f"Added {format_items(added)} to **{name}**'s inventory!")
    if sum(added.values()) > 1:
        embed.add_field(name="Total Items Added", value=f"{sum(added.values())} items", inline=True)
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
    embed = success_embed("Items removed!", f"Removed {format_items(removed)} from **{name}**'s inventory!")
    if sum(removed.values()) > 1:
        embed.add_field(name="Total Items Removed", value=f"{sum(removed.values())} items", inline=True)
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
    embed = success_embed("Items used!", f"**{name}** used {format_items(used)}!")
    if sum(used.values()) > 1:
        embed.add_field(name="Total Items Used", value=f"{sum(used.values())} items", inline=True)
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
    embed = discord.Embed(title=f"{main['emoji']} ┃ Item Values", description=f"**{name}**'s sellable items:",
                          color=COLOR_DEFAULT)
    for info in items_info[:23]:  # leave room for the total field (max 25 fields)
        if info['sell_value'] == 0:
            value_text = "❌ Not sellable"
        else:
            value_text = (f"{main['emoji']} {info['sell_value']:,} {main['name']} each\n"
                          f"**Total:** {info['total']:,} {main['name']} ({info['quantity']}x)")
        embed.add_field(name=f"• {info['name']}", value=value_text, inline=False)
    embed.add_field(name="━━━━━━━━━━━━━━", value=f"**Total Value:** {amount_text('main', total_value)}", inline=False)
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

    main_name = CURRENCIES['main']['name']
    embed = success_embed("Items sold!", f"**{name}** earned {amount_text('main', total_earned)}!")
    items_text = "\n".join(
        f"• {i['name']} x{i['quantity']} → {i['sell_value']:,} {main_name} each = **{i['total']:,} {main_name}**"
        for i in items_sold)
    embed.add_field(name="Items Sold", value=items_text[:1024], inline=False)
    embed.add_field(name="Total Earned", value=amount_text('main', total_earned), inline=False)
    if any(w in message.lower() for w in ("not found", "no value", "not enough")):
        embed.set_footer(text=f"⚠️ {message}")
    await interaction.response.send_message(embed=embed)


# ==================== MINIGAMES ====================
# One shared implementation; every entry in config.MINIGAMES becomes a command.

def _add_xp_info(embed, xp_result):
    if not xp_result:
        return
    if xp_result['leveled_up']:
        bonus_gain = db.level_bonus(xp_result['new_level']) - db.level_bonus(xp_result['old_level'])
        value = f"Level {xp_result['old_level']} → **Level {xp_result['new_level']}**"
        if bonus_gain:
            value += f"\n+{bonus_gain} roll bonus!"
        embed.add_field(name=f"{EMOJI['level_up']} LEVEL UP!", value=value, inline=False)
        embed.set_footer(text=f"Next level: {xp_result['current_xp']}/{xp_result['xp_for_next']} XP")
    else:
        embed.set_footer(text=f"Level {xp_result['new_level']} | XP: {xp_result['current_xp']}/{xp_result['xp_for_next']}")


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

    def _title(self, text):
        return f"{self.cfg['emoji']} ┃ {text}"

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
            embed = discord.Embed(title=self._title(self.cfg['success_title']),
                                  description=f"{roll_text}\n\n{self.cfg['success_text'].format(item=self.item_name)}",
                                  color=COLOR_DEFAULT)
            embed.add_field(name="Obtained", value=f"**{self.item_name}**", inline=True)
        else:
            xp_reward //= 4
            embed = discord.Embed(title=self._title(self.cfg['fail_title']),
                                  description=f"{roll_text}\n\n{self.fail_message}", color=COLOR_ERROR)
            embed.add_field(name="Result", value="No item obtained", inline=True)

        xp_result = db.add_xp(self.character_name, xp_reward, self.game)
        embed.add_field(name="XP Gained", value=f"+{xp_reward} XP", inline=True)
        _add_xp_info(embed, xp_result)
        await self._finish(interaction, embed)

    @discord.ui.button(label="Leave It", style=discord.ButtonStyle.gray)
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        xp_reward = config.MINIGAME_XP_LEAVE
        xp_result = db.add_xp(self.character_name, xp_reward, self.game)
        leave_text = self.cfg['leave_text'].format(name=self.character_name, item=self.item_name)
        embed = discord.Embed(title=self._title(self.cfg['leave_title']),
                              description=f"{leave_text}\n\n{self.flee_message}", color=COLOR_DEFAULT)
        embed.add_field(name="Result", value="No item obtained", inline=True)
        embed.add_field(name="XP Gained", value=f"+{xp_reward} XP", inline=True)
        _add_xp_info(embed, xp_result)
        await self._finish(interaction, embed)


async def run_minigame(interaction, name, game):
    if not await require_owner(interaction, name):
        return
    cfg = MINIGAMES[game]
    result = db.roll_minigame(name, game)
    if result is None:
        await interaction.response.send_message(embed=error_embed(f"Profile **{name}** not found!"))
        return

    embed = discord.Embed(title=f"{cfg['emoji']} ┃ {cfg['label']}", description=result['description'],
                          color=COLOR_DEFAULT)
    item_name = result['item']
    if not item_name:
        await interaction.response.send_message(embed=embed)
        return

    profile = db.get_profile(name)
    level = profile[f"{game}_level"] if profile else 1
    embed.add_field(name=cfg['found_field'], value=f"**{item_name}**", inline=True)
    embed.add_field(name="Difficulty", value=f"**{result['difficulty']}**", inline=True)
    embed.set_footer(text=f"{name} (Level {level})")
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
        await interaction.response.send_message("Invalid dice format! Use format like: 1d20, 2d6, or 1d20+5")
        return
    if result == "too_big":
        await interaction.response.send_message("Too many dice or sides! Keep it reasonable.")
        return
    rolls, modifier, total = result
    embed = discord.Embed(title=f"{EMOJI['dice']} ┃ Roll", color=COLOR_DEFAULT)
    if len(rolls) > 1 or modifier != 0:
        modifier_str = f" {modifier:+d}" if modifier else ""
        embed.add_field(name="Result:", value=f"**{total}**", inline=False)
        embed.add_field(name="", value=f"-# Rolling {dice.lower()}\n-# Rolls: {', '.join(map(str, rolls))}{modifier_str}",
                        inline=False)
    else:
        embed.add_field(name="Result:", value=f"**{rolls[0]}**", inline=False)
        embed.add_field(name="", value=f"-# Rolling {dice.lower()}", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="choose", description="Pick a random option from a list")
@app_commands.describe(options="Comma-separated list of options")
async def choose(interaction: discord.Interaction, options: str):
    choices = [choice.strip() for choice in options.split(',') if choice.strip()]
    if len(choices) < 2:
        await interaction.response.send_message("Please provide at least 2 options separated by commas!")
        return
    embed = discord.Embed(title=f"{EMOJI['choose']} ┃ Random Choice",
                          description=f"I choose: **{random.choice(choices)}**", color=COLOR_DEFAULT)
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
    embed = discord.Embed(title=f"{EMOJI['location']} ┃ Location",
                          description=f"**{profile['name']}** sets off and arrives at **{db.get_random_location()}**.",
                          color=COLOR_DEFAULT)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="Show all available commands")
async def help_command(interaction: discord.Interaction):
    roles = " / ".join(config.ROLES)
    embed = discord.Embed(title=f"{EMOJI['profile']} ┃ Bot Commands Help",
                          description="Here are all available commands for the bot:", color=COLOR_DEFAULT)
    embed.add_field(name=".✦  Profile Management", value=(
        f"`/create [name] [role]` - Create a new character ({roles})\n"
        "`/profile [name]` - View character profile and inventory\n"
        "`/list` - Show all your character profiles\n"
        "`/editname [name] [new_name]` - Change character name\n"
        "`/editrole [name] [new_role]` - Change character role\n"
        "`/deleteprofile [name]` - Delete a character profile\n"
        "`/claim [name]` - Claim an unowned character"), inline=False)
    embed.add_field(name=".✦  Currency", value=(
        f"`/addcurrency` / `/removecurrency` - [Admin] Add or remove {MAIN_NAME} or {PREMIUM_NAME}\n"
        "`/givecurrency` - Give currency to another character\n"
        f"`/exchange` - Exchange {MAIN_NAME} and {PREMIUM_NAME}"), inline=False)
    embed.add_field(name=".✦  Inventory & Shop", value=(
        "`/buy [name] [items]` - Buy items (e.g. `Lantern:2, Bandage`)\n"
        "`/additem` / `/removeitem` - Add or remove items\n"
        "`/use [name] [items]` - Use items\n"
        "`/giveitem` - Give items to another character\n"
        f"`/value` / `/sell` - Check value of / sell items for {MAIN_NAME}"), inline=False)
    minigame_lines = "\n".join(f"`/{game} [name]` - {cfg['command_description']}" for game, cfg in MINIGAMES.items())
    embed.add_field(name=".✦  Gameplay", value=(
        "`/match [name]` - Play a match and earn rewards\n"
        "`/matchreward [name] [count]` - Rewards for escapes / eliminations\n"
        "`/cnmatch [location] [character]` - Cryptic Notes run, alone or in a group of up to 4\n"
        f"{minigame_lines}\n"
        "`/location [name]` - Travel to a random location"), inline=False)
    embed.add_field(name=".✦  Utility", value=(
        "`/roll [dice]` - Roll dice (e.g., 1d20, 2d6+3)\n"
        "`/choose [options]` - Pick a random option from a comma-separated list"), inline=False)
    embed.add_field(name=".✦  Reservations", value=(
        f"`/reserve [character]` - Request a roster character (in <#{config.RESERVE_CHANNEL_ID}>)\n"
        "`/myreservations` - Your reservations, requests and characters"), inline=False)
    embed.set_footer(text="💡 Tip: Use /profile to see your inventory organized by categories!")
    await interaction.response.send_message(embed=embed)


# ==================== ADMIN COMMANDS ====================

@bot.tree.command(name="restart", description="[ADMIN] Restart the bot process")
async def restart_bot(interaction: discord.Interaction):
    if not await require_admin(interaction):
        return
    embed = discord.Embed(title="🔄 ┃ Restarting...",
                          description="Restarting the bot now. It should be back in a few seconds.",
                          color=COLOR_DEFAULT)
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
        embed.add_field(name="Decision", value=decision_text, inline=False)
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
        embed = discord.Embed(title=f"📅 ┃ Reservation {'Extended' if is_extension else 'Confirmed'}",
                              description=description, color=COLOR_DEFAULT)
        embed.add_field(name="Expires", value=f"<t:{expires}:F> ┃ <t:{expires}:R>", inline=False)
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
        embed = discord.Embed(title=f"{EMOJI['error']} ┃ {'Extension' if is_extension else 'Request'} Denied",
                              description=text + reason_text, color=COLOR_ERROR)
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
        embed = discord.Embed(title="🔁 ┃ Extension Request",
                              description=f"<@{info['user_id']}> would like to extend **{info['title']}**.",
                              color=COLOR_DEFAULT)
        embed.add_field(name="Currently expires",
                        value=f"<t:{info['expires_at']}:F> ┃ <t:{info['expires_at']}:R>", inline=False)
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
    embed = discord.Embed(title="📨 ┃ Reservation Request", color=COLOR_DEFAULT)
    embed.add_field(name="Member", value=f"{interaction.user.mention} ({interaction.user.display_name})", inline=False)
    embed.add_field(name="Character", value=f"**{found['display']}** ({found['role']})", inline=False)
    linked = linked_identity(found['id'])
    if linked:
        embed.add_field(name="Linked identity (included)", value=f"**{linked['display']}** ({linked['role']})",
                        inline=False)
    if note:
        embed.add_field(name="Note", value=note[:1024], inline=False)
    embed.add_field(name="Active reservations",
                    value=f"{active}/{config.MAX_RESERVATIONS_PER_MEMBER}", inline=False)
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
    embed = discord.Embed(title="📅 ┃ Your Reservations", color=COLOR_DEFAULT)
    embed.add_field(name="Active", inline=False, value="\n".join(
        f"• **{r['title']}** – expires <t:{r['expires_at']}:F> (<t:{r['expires_at']}:R>)"
        + (" · extension requested" if r['extension_pending'] else "")
        for r in data['active']) or "None")
    embed.add_field(name="Waiting for staff", inline=False,
                    value="\n".join(f"• **{r['title']}**" for r in data['pending']) or "None")
    embed.add_field(name="Your characters", inline=False,
                    value="\n".join(f"• **{c['display']}**" for c in data['taken']) or "None")
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
        lines = [f"• **{c['display']}** ({c['status']})" for c in characters]
        lines += [f"• **{r['title']}** (request waiting for staff)" for r in pending]
        embed = discord.Embed(title="👋 ┃ Member Left",
                              description=f"**{member}** ({member.mention}) left the server. They had:\n"
                                          + "\n".join(lines),
                              color=COLOR_ERROR)
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
                embed = discord.Embed(
                    title="⏳ ┃ Reservation Ending Soon",
                    description=(f"Your reservation for **{info['title']}** runs out <t:{info['expires_at']}:R>.\n"
                                 f"Need more time? Click **Extend** to ask staff."),
                    color=COLOR_DEFAULT)
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
            text = f"Your reservation for **{info['title'] or ''}** has run out."
            if is_roster:
                text += " The character is free again."
            embed = discord.Embed(title=f"{EMOJI['error']} ┃ Reservation Expired", description=text, color=COLOR_ERROR)
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
