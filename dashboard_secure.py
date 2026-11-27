import datetime
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
from functools import wraps

import re
from urllib.parse import urlparse

import requests
from markupsafe import Markup, escape
from flask import Flask, render_template, request, redirect, url_for, flash, session, abort
from werkzeug.security import generate_password_hash, check_password_hash

import config
import roster_format
from database import Database

app = Flask(__name__)
# Set DASHBOARD_SECRET in .env to keep people logged in across restarts
app.secret_key = os.getenv("DASHBOARD_SECRET") or secrets.token_hex(32)

db = Database()

# ==================== LOGIN ====================

def _load_users():
    """Read logins from .env: DASHBOARD_USERS=name:password,name2:password2

    Passwords are never stored in the code (the repository is on GitHub).
    """
    result = {}
    for entry in os.getenv("DASHBOARD_USERS", "").split(","):
        username, sep, password = entry.strip().partition(":")
        if sep and username.strip() and password:
            result[username.strip()] = generate_password_hash(password)
    return result


users = _load_users()
if not users:
    print("⚠️  No dashboard logins configured: set DASHBOARD_USERS in .env. Nobody can log in until then.")

LOGIN_ENABLED = True

# Logins that may update and restart the bot (everyone else, e.g. "mod", doesn't even see the buttons).
# env: DASHBOARD_ADMINS=admin,otheradmin   (default: admin)
DASHBOARD_ADMINS = {u.strip() for u in os.getenv("DASHBOARD_ADMINS", "admin").split(",") if u.strip()}


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if LOGIN_ENABLED and not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def is_dashboard_admin():
    return session.get('username') in DASHBOARD_ADMINS


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_dashboard_admin():
            flash('Only dashboard admins can do that.', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function


@app.template_filter('emoji')
def emoji_filter(text):
    """Show custom Discord emojis ("<:name:id>") from config.py as images on the dashboard."""
    def to_img(match):
        animated, name, emoji_id = match.groups()
        return (f'<img src="https://cdn.discordapp.com/emojis/{emoji_id}.{"gif" if animated else "webp"}?size=32" '
                f'alt=":{name}:" title=":{name}:" style="height:1.25em;vertical-align:-0.25em;">')
    return Markup(re.sub(r'&lt;(a?):(\w+):(\d+)&gt;', to_img, str(escape(text or ''))))


@app.context_processor
def inject_config():
    """Make the server configuration available in every template."""
    return {
        'bot_name': config.BOT_NAME,
        'currencies': config.CURRENCIES,
        'minigames': config.MINIGAMES,
        'roles': config.ROLES,
        'categories': config.ITEM_CATEGORIES,
        'is_dashboard_admin': is_dashboard_admin(),
    }


@app.route('/login', methods=['GET', 'POST'])
def login():
    if not LOGIN_ENABLED:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username in users and check_password_hash(users[username], password or ''):
            session['logged_in'] = True
            session['username'] = username
            flash('Successfully logged in!', 'success')
            return redirect(url_for('index'))
        flash('Invalid username or password!', 'danger')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('Successfully logged out!', 'info')
    return redirect(url_for('login'))


# ==================== HELPERS ====================

def query(sql, params=()):
    with db._conn() as conn:
        return conn.execute(sql, params).fetchall()


def execute(sql, params=()):
    with db._conn() as conn:
        conn.execute(sql, params)
        conn.commit()


def form_int(key, default):
    try:
        return int(request.form.get(key) or default)
    except ValueError:
        return default


def rename_in_inventories(table, item_id, new_name, update_sql, update_params):
    """Run an item UPDATE and carry a name change over to everyone's inventory.

    Without this, characters who already own the item would keep it under the
    old name, which then has no category or sell value.
    """
    old = query(f"SELECT item_name FROM {table} WHERE id = ?", (item_id,))
    old_name = old[0][0] if old else None
    execute(update_sql, update_params)
    if old_name and new_name and old_name.strip() != new_name.strip():
        renamed = db.rename_inventory_item(old_name, new_name)
        if renamed:
            flash(f'Renamed {renamed} owned "{old_name}" in character inventories to "{new_name}".', 'info')


def bot_api(path, timeout=15, **kwargs):
    """POST to the bot's internal API."""
    return requests.post(f"{config.BOT_API_URL}/{path}", timeout=timeout, **kwargs)


def refresh_player_names(user_ids):
    """Ask the bot for players' current Discord names and store them.

    If the bot isn't running, the last known names are simply kept.
    """
    ids = sorted({str(i) for i in user_ids if i and str(i).isdigit()})
    if not ids:
        return
    try:
        response = bot_api('member_names', timeout=8, json={'ids': ids})
        if response.status_code == 200:
            db.update_player_names(response.json())
    except Exception:
        pass


# ==================== HOME & UPDATES ====================

@app.route('/')
@login_required
def index():
    return render_template('index.html')


REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_FILES = ("dashboard_secure.py", "database.py", "config.py", "roster_format.py", "templates/")
DB_FILE_NAME = os.path.basename(config.DB_FILE)
BACKUP_DIR = os.path.join(REPO_DIR, "backups")   # in .gitignore
BACKUPS_KEPT = 10


def _restart_services(restart_dashboard):
    subprocess.run(["sudo", "systemctl", "restart", config.BOT_SERVICE])
    if restart_dashboard:
        subprocess.run(["sudo", "systemctl", "restart", config.DASHBOARD_SERVICE])


def _git(*args, timeout=60):
    """Run git in the bot folder. Returns (success, output)."""
    result = subprocess.run(["git", "-c", "user.name=Dashboard", "-c", "user.email=dashboard@localhost", *args],
                            cwd=REPO_DIR, capture_output=True, text=True, timeout=timeout)
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def _backup_database():
    """Copy the live database into backups/ (safe while the bot is running). Keeps the newest few."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    target = os.path.join(BACKUP_DIR, f"{os.path.splitext(DB_FILE_NAME)[0]}_"
                                      f"{datetime.datetime.now():%Y-%m-%d_%H-%M-%S}.db")
    with db._conn() as source, sqlite3.connect(target) as copy:
        source.backup(copy)
    old = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".db"))
    for name in old[:-BACKUPS_KEPT]:
        os.remove(os.path.join(BACKUP_DIR, name))
    return os.path.relpath(target, REPO_DIR)


def _untrack_database():
    """The live database must never be managed by git, or a pull could overwrite or delete it.
    If git tracks it here, stop that (the file itself stays untouched). Returns True if it was tracked."""
    tracked, _ = _git("ls-files", "--error-unmatch", DB_FILE_NAME)
    if tracked:
        _git("rm", "--cached", "--quiet", DB_FILE_NAME)
    return tracked


@app.route('/admin/check-updates', methods=['POST'])
@login_required
@admin_required
def admin_check_updates():
    """Show what an update would do, without changing anything."""
    try:
        ok, output = _git("fetch", "origin", timeout=60)
        if not ok:
            flash(f"Couldn't reach GitHub: {output}", "danger")
            return redirect(url_for('index'))
        _, incoming = _git("log", "--oneline", "HEAD..origin/main")
        _, local_commits = _git("log", "--oneline", "origin/main..HEAD")
        _, local_changes = _git("status", "--porcelain", "--untracked-files=no")
        local_changes = "\n".join(l for l in local_changes.splitlines() if not l.endswith(DB_FILE_NAME))
        if incoming:
            flash(f"New on GitHub:\n{incoming}", "info")
        else:
            flash("No new updates on GitHub.", "info")
        if local_changes or local_commits:
            flash("Changes made on the Pi (will be kept and merged with the update):\n"
                  + "\n".join(x for x in (local_commits, local_changes) if x), "warning")
    except Exception as e:
        flash(f"Error: {e}", "danger")
    return redirect(url_for('index'))


@app.route('/admin/update', methods=['POST'])
@login_required
@admin_required
def admin_update():
    """Back up the database, keep the Pi's own changes, merge GitHub's changes and restart.
    .env is never touched (git ignores it). On a merge conflict nothing is changed."""
    try:
        backup = _backup_database()
        untracked = _untrack_database()

        # Save changes made directly on the Pi (to files git tracks) so the merge keeps them
        _, local_changes = _git("status", "--porcelain", "--untracked-files=no")
        if local_changes:
            _git("add", "-u")
            ok, output = _git("commit", "-m", "Changes made on the Pi (saved by the dashboard before updating)")
            if not ok and "nothing to commit" not in output:
                flash(f"Couldn't save the Pi's changes, nothing was updated: {output}", "danger")
                return redirect(url_for('index'))

        ok, output = _git("pull", "--no-rebase", "--no-edit", "origin", "main", timeout=120)
        if not ok:
            _, conflicts = _git("diff", "--name-only", "--diff-filter=U")
            if conflicts:
                _git("merge", "--abort")
                flash("The update clashes with changes made on the Pi in: " + ", ".join(conflicts.split())
                      + ". Nothing was changed and the bot keeps running. Your Pi changes are saved; "
                        "fix the clash in those files (or ask for help) and update again.", "danger")
            else:
                flash(f"Update failed, nothing was changed: {output}", "danger")
            return redirect(url_for('index'))

        notes = [f"Database backed up to {backup}."]
        if untracked:
            notes.append("The live database is no longer managed by git (the file itself is unchanged).")
        if "Already up to date" in output:
            flash("Already up to date — nothing to restart. " + " ".join(notes), "info")
            return redirect(url_for('index'))

        _, changed = _git("diff", "--name-only", "ORIG_HEAD", "HEAD")
        changed = changed.split()
        if "requirements.txt" in changed:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"],
                           cwd=REPO_DIR, timeout=600)
            notes.append("New packages installed.")
        dashboard_changed = any(f.startswith(DASHBOARD_FILES) for f in changed)
        flash(f"Updated {len(changed)} file(s), restarting {'bot and dashboard' if dashboard_changed else 'bot'}... "
              + " ".join(notes), "success")
        threading.Timer(1.5, _restart_services, args=(dashboard_changed,)).start()
    except subprocess.TimeoutExpired:
        flash("Update timed out, nothing was restarted. Try again in a moment.", "danger")
    except Exception as e:
        flash(f"Error: {e}", "danger")
    return redirect(url_for('index'))


# ==================== LOCATIONS ====================

@app.route('/locations')
@login_required
def locations():
    return render_template('locations.html', locations=query("SELECT id, name FROM locations ORDER BY name"))


@app.route('/locations/add', methods=['POST'])
@login_required
def add_location():
    name = (request.form.get('name') or '').strip()
    if name:
        try:
            execute("INSERT INTO locations (name) VALUES (?)", (name,))
            flash(f'Location "{name}" added!', 'success')
        except Exception as e:
            flash(f'Error: {e}', 'danger')
    return redirect(url_for('locations'))


@app.route('/locations/edit/<int:location_id>', methods=['POST'])
@login_required
def edit_location(location_id):
    name = (request.form.get('name') or '').strip()
    if name:
        try:
            execute("UPDATE locations SET name = ? WHERE id = ?", (name, location_id))
            flash('Location updated!', 'success')
        except Exception as e:
            flash(f'Error: {e}', 'danger')
    return redirect(url_for('locations'))


@app.route('/locations/delete/<int:location_id>', methods=['POST'])
@login_required
def delete_location(location_id):
    try:
        execute("DELETE FROM locations WHERE id = ?", (location_id,))
        flash('Location deleted!', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('locations'))


# ==================== SHOP ====================

def _shop_form():
    currency = request.form.get('currency_type', 'main')
    if currency not in config.CURRENCIES:
        currency = 'main'
    return (db.normalise(request.form.get('name')),
            form_int('price', 0),
            request.form.get('description') or None,
            db.normalise_category(request.form.get('category')),
            currency)


@app.route('/shop')
@login_required
def shop():
    items = query("SELECT id, item_name, price, description, category, currency_type FROM shop_items ORDER BY price")
    return render_template('shop.html', items=items)


@app.route('/shop/add', methods=['POST'])
@login_required
def add_shop_item():
    if request.form.get('name') and request.form.get('price'):
        try:
            values = _shop_form()
            execute("INSERT INTO shop_items (item_name, price, description, category, currency_type) "
                    "VALUES (?, ?, ?, ?, ?)", values)
            flash(f'Item "{values[0]}" added to the shop!', 'success')
        except Exception as e:
            flash(f'Error: {e}', 'danger')
    return redirect(url_for('shop'))


@app.route('/shop/edit/<int:item_id>', methods=['POST'])
@login_required
def edit_shop_item(item_id):
    if request.form.get('name') and request.form.get('price'):
        try:
            values = _shop_form()
            rename_in_inventories(
                "shop_items", item_id, values[0],
                "UPDATE shop_items SET item_name = ?, price = ?, description = ?, category = ?, currency_type = ? "
                "WHERE id = ?", values + (item_id,))
            flash('Shop item updated!', 'success')
        except Exception as e:
            flash(f'Error: {e}', 'danger')
    return redirect(url_for('shop'))


@app.route('/shop/delete/<int:item_id>', methods=['POST'])
@login_required
def delete_shop_item(item_id):
    try:
        execute("DELETE FROM shop_items WHERE id = ?", (item_id,))
        flash('Shop item deleted!', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('shop'))


# ==================== MATCH MESSAGES ====================

def _match_form():
    role = request.form.get('role')
    level = request.form.get('performance_level')
    message = (request.form.get('message') or '').strip()
    if role not in config.ROLES or level not in {'0', '1', '2', '3', '4'} or not message:
        return None
    return role, int(level), message


@app.route('/matches')
@login_required
def matches():
    messages = query("SELECT id, role, performance_level, message FROM match_messages "
                     "ORDER BY performance_level, role, id")
    return render_template('matches.html', messages=messages, result_text=config.MATCH_RESULT_TEXT)


@app.route('/matches/add', methods=['POST'])
@login_required
def add_match_message():
    values = _match_form()
    if values:
        role, level, text = values
        # Every non-empty line is its own message, so several can be pasted at once
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        try:
            for line in lines:
                execute("INSERT INTO match_messages (role, performance_level, message) VALUES (?, ?, ?)",
                        (role, level, line))
            what = 'message' if len(lines) == 1 else f'{len(lines)} messages'
            flash(f'{role} {what} for result {level} added!', 'success')
        except Exception as e:
            flash(f'Error: {e}', 'danger')
    return redirect(url_for('matches'))


@app.route('/matches/edit/<int:message_id>', methods=['POST'])
@login_required
def edit_match_message(message_id):
    values = _match_form()
    if values:
        try:
            execute("UPDATE match_messages SET role = ?, performance_level = ?, message = ? WHERE id = ?",
                    values + (message_id,))
            flash('Match message updated!', 'success')
        except Exception as e:
            flash(f'Error: {e}', 'danger')
    return redirect(url_for('matches'))


@app.route('/matches/delete/<int:message_id>', methods=['POST'])
@login_required
def delete_match_message(message_id):
    try:
        execute("DELETE FROM match_messages WHERE id = ?", (message_id,))
        flash('Match message deleted!', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('matches'))


# ==================== CRYPTIC NOTES ====================

def _cn_back():
    return redirect(url_for('cryptic_notes') + (request.form.get('anchor') or ''))


@app.route('/matches/cryptic-notes')
@login_required
def cryptic_notes():
    maps = db.get_cn_maps()
    anomalies, items = db.get_cn_codex('anomaly'), db.get_cn_codex('item')
    map_counts = {m['id']: {'anomalies': sum(1 for a in anomalies if m['id'] in a['maps']),
                            'classics': sum(1 for i in items if m['id'] in i['maps'])} for m in maps}
    return render_template('cryptic_notes.html', maps=maps, map_names={m['id']: m['name'] for m in maps},
                           map_counts=map_counts, anomalies=anomalies, items=items,
                           messages=db.get_cn_messages(), tiers=config.CN_TIERS, outcomes=config.CN_OUTCOMES,
                           kill_chance=config.CN_DANGER_KILL_CHANCE)


@app.route('/cn/map/<int:map_id>', methods=['POST'])
@login_required
def cn_save_map(map_id):
    db.update_cn_map(map_id, form_int('other_death_chance', 8))
    flash('Map settings saved!', 'success')
    return _cn_back()


@app.route('/cn/anomaly', methods=['POST'])
@app.route('/cn/anomaly/<int:entry_id>', methods=['POST'])
@login_required
def cn_save_anomaly(entry_id=None):
    success, message = db.save_cn_anomaly(entry_id, request.form.get('name'), form_int('danger', 2),
                                          request.form.get('description'), request.form.getlist('maps'))
    flash(message, 'success' if success else 'danger')
    return _cn_back()


@app.route('/cn/item', methods=['POST'])
@app.route('/cn/item/<int:entry_id>', methods=['POST'])
@login_required
def cn_save_item(entry_id=None):
    success, message = db.save_cn_item(entry_id, request.form.get('name'), request.form.get('tier'),
                                       form_int('sell_value', 0), form_int('weight', 10),
                                       request.form.get('category'), request.form.get('description'),
                                       request.form.getlist('maps'))
    flash(message, 'success' if success else 'danger')
    return _cn_back()


@app.route('/cn/<kind>/delete/<int:entry_id>', methods=['POST'])
@login_required
def cn_delete(kind, entry_id):
    if kind not in ('anomaly', 'item'):
        abort(404)
    success, message = db.delete_cn_entry(kind, entry_id)
    flash(message, 'success' if success else 'danger')
    return _cn_back()


@app.route('/cn/import', methods=['POST'])
@login_required
def cn_import():
    kind = request.form.get('kind')
    if kind not in ('anomaly', 'item'):
        abort(400)
    map_ids = request.form.getlist('maps')
    if not map_ids:
        flash('Tick at least one map for the imported entries!', 'danger')
        return _cn_back()
    added, skipped = db.import_cn_codex(kind, request.form.get('lines'), map_ids)
    flash(f"Imported {added} {'anomalies' if kind == 'anomaly' else 'classics'}.", 'success' if added else 'info')
    for message in skipped[:20]:
        flash(f'Skipped: {message}', 'warning')
    return _cn_back()


@app.route('/cn/message', methods=['POST'])
@app.route('/cn/message/<int:message_id>', methods=['POST'])
@login_required
def cn_save_message(message_id=None):
    success, message = db.save_cn_message(message_id, request.form.get('map_id'), request.form.get('outcome'),
                                          request.form.get('message'))
    flash(message, 'success' if success else 'danger')
    return _cn_back()


@app.route('/cn/message/delete/<int:message_id>', methods=['POST'])
@login_required
def cn_delete_message(message_id):
    success, message = db.delete_cn_message(message_id)
    flash(message, 'success' if success else 'danger')
    return _cn_back()


# ==================== MINIGAMES (one set of routes for every minigame in config) ====================

LOOT_COLUMNS = ("id, item_name, message, description, category, sell_value, "
                "weight, difficulty, flee_message, fail_message")


def _loot_table(game):
    if game not in config.MINIGAMES:
        abort(404)
    return db.loot_table(game)


def _loot_form():
    item_name = (request.form.get('item_name') or '').strip()
    item_name = db.normalise(item_name) if item_name else None
    category = db.normalise_category(request.form.get('category')) if item_name else None
    return (item_name,
            (request.form.get('message') or '').strip(),
            request.form.get('description') or None,
            category,
            form_int('sell_value', 0),
            form_int('weight', 10),
            form_int('difficulty', 10),
            request.form.get('flee_message') or None,
            request.form.get('fail_message') or None)


@app.route('/minigame/<game>')
@login_required
def minigame(game):
    items = query(f"SELECT {LOOT_COLUMNS} FROM {_loot_table(game)} ORDER BY item_name IS NULL, item_name")
    return render_template('minigame.html', game=game, cfg=config.MINIGAMES[game], items=items,
                           xp_base=config.MINIGAME_XP_BASE, xp_per=config.MINIGAME_XP_PER_DIFFICULTY,
                           xp_leave=config.MINIGAME_XP_LEAVE)


@app.route('/minigame/<game>/add', methods=['POST'])
@login_required
def add_minigame_item(game):
    table = _loot_table(game)
    values = _loot_form()
    if values[1]:
        try:
            execute(f"INSERT INTO {table} (item_name, message, description, category, sell_value, weight, "
                    f"difficulty, flee_message, fail_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
            flash('Outcome added!', 'success')
        except Exception as e:
            flash(f'Error: {e}', 'danger')
    return redirect(url_for('minigame', game=game))


@app.route('/minigame/<game>/edit/<int:item_id>', methods=['POST'])
@login_required
def edit_minigame_item(game, item_id):
    table = _loot_table(game)
    values = _loot_form()
    if values[1]:
        try:
            rename_in_inventories(
                table, item_id, values[0],
                f"UPDATE {table} SET item_name = ?, message = ?, description = ?, category = ?, sell_value = ?, "
                f"weight = ?, difficulty = ?, flee_message = ?, fail_message = ? WHERE id = ?",
                values + (item_id,))
            flash('Outcome updated!', 'success')
        except Exception as e:
            flash(f'Error: {e}', 'danger')
    return redirect(url_for('minigame', game=game))


@app.route('/minigame/<game>/delete/<int:item_id>', methods=['POST'])
@login_required
def delete_minigame_item(game, item_id):
    table = _loot_table(game)
    try:
        execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
        flash('Outcome deleted!', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('minigame', game=game))


# ==================== PROFILES ====================

@app.route('/profiles')
@login_required
def profiles():
    with db._conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM profiles ORDER BY name COLLATE NOCASE").fetchall()
    return render_template('profiles.html', profiles=[dict(r) for r in rows])


@app.route('/profiles/delete/<int:profile_id>', methods=['POST'])
@login_required
def delete_profile_dashboard(profile_id):
    rows = query("SELECT name FROM profiles WHERE id = ?", (profile_id,))
    if not rows:
        flash('Profile not found!', 'danger')
    else:
        success, message = db.delete_profile(rows[0][0])
        flash(f'Profile "{rows[0][0]}" and their inventory deleted!' if success else message,
              'success' if success else 'danger')
    return redirect(url_for('profiles'))


# ==================== ROSTER ====================

ROLE_SLUGS = {role.lower(): role for role in config.ROLES}  # /roster/hunter -> "Hunter"


def _role_from_slug(role_slug):
    role = ROLE_SLUGS.get((role_slug or '').lower())
    if not role:
        abort(404)
    return role


def refresh_roster_in_discord(role=None):
    """Ask the bot to update the roster posts (one role or all). Problems only produce a warning."""
    try:
        response = bot_api('refresh_roster', timeout=60, json={'role': role} if role else {})
        if response.status_code != 200:
            flash(f"Saved, but the roster in Discord wasn't updated: {response.json().get('message')}", 'warning')
    except Exception as e:
        flash(f"Saved, but the roster in Discord wasn't updated (is the bot running?): {e}", 'warning')


def _back_to_roster():
    """Return to the roster tab (Hunters / Survivors) the form was sent from."""
    path = urlparse(request.referrer or '').path
    return redirect(path if path.startswith('/roster/') else url_for('roster'))


def _roster_result(success, message):
    flash(message, 'success' if success else 'danger')
    if success:
        refresh_roster_in_discord()  # a character can affect both posts (role change, linked identity)
    return _back_to_roster()


@app.route('/roster')
@login_required
def roster():
    return redirect(url_for('roster_page', role_slug=config.ROLES[0].lower()))


@app.route('/roster/<role_slug>')
@login_required
def roster_page(role_slug):
    role = _role_from_slug(role_slug)
    refresh_player_names(c['player_id'] for c in db.get_roster())
    all_characters = db.get_roster()
    characters = [c for c in all_characters if c['role'] == role]
    counts = {status: sum(1 for c in characters if c['status'] == status) for status in ('free', 'reserved', 'taken')}
    raw_end_dates = db.get_reservation_end_dates()
    end_dates = {roster_id: datetime.datetime.fromtimestamp(ts).strftime('%d %b %Y, %H:%M')
                 for roster_id, ts in raw_end_dates.items()}
    # Values for the date picker in the Reserve form (current end date, or the normal length from now)
    end_inputs = {roster_id: datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%dT%H:%M')
                  for roster_id, ts in raw_end_dates.items()}
    default_end = (datetime.datetime.now() + datetime.timedelta(days=config.RESERVATION_DAYS)).strftime('%Y-%m-%dT%H:%M')
    names = {c['id']: c['display'] for c in all_characters}
    # For the live preview: Discord shows real mentions, the preview shows "@Name"
    preview_characters = [{'display': c['display'], 'name': c['name'], 'title': c['title'] or '',
                           'status': c['status'],
                           'player': ('@' + (c['player_name'] or c['player_id'])) if c['player_id'] else ''}
                          for c in characters]
    return render_template('roster.html', role=role, characters=characters, all_characters=all_characters,
                           counts=counts, end_dates=end_dates, end_inputs=end_inputs, default_end=default_end,
                           names=names,
                           layout=roster_format.get_layout(db, role), preview_characters=preview_characters,
                           indent=roster_format.INDENT, limits=roster_format.LIMITS)


@app.route('/roster/layout/<role_slug>', methods=['POST'])
@login_required
def roster_layout(role_slug):
    role = _role_from_slug(role_slug)
    values = {key: request.form.get(key, '') for key in
              ('line_format', 'reserved_suffix', 'empty_text', 'title', 'color', 'intro', 'footer',
               'thumbnail', 'image', 'channel_id')}
    values['channel_id'] = values['channel_id'].strip()
    values['style'] = 'message' if request.form.get('style') == 'message' else 'embed'
    values['alternate_indent'] = '1' if request.form.get('alternate_indent') else '0'
    values['blank_lines'] = '1' if request.form.get('blank_lines') else '0'
    if values['channel_id'] and not values['channel_id'].isdigit():
        flash('The post ID must be a number (right-click the forum post → Copy ID)!', 'danger')
        return _back_to_roster()
    if '{character}' not in values['line_format'] and '{name}' not in values['line_format']:
        flash('The line format needs {character} or {name}, otherwise no names would be shown!', 'danger')
        return _back_to_roster()
    roster_format.save_layout(db, role, values)
    flash(f'{role} roster layout saved!', 'success')
    refresh_roster_in_discord(role)
    return _back_to_roster()


@app.route('/roster/add', methods=['POST'])
@login_required
def add_roster_character():
    return _roster_result(*db.add_roster_character(request.form.get('name'), request.form.get('title'),
                                                   request.form.get('role')))


@app.route('/roster/import', methods=['POST'])
@login_required
def import_roster():
    role = request.form.get('role')
    if role not in config.ROLES:
        flash('Please choose Hunter or Survivor!', 'danger')
        return _back_to_roster()
    added, skipped = db.import_roster(request.form.get('lines'), role)
    flash(f'Imported {added} {role} character(s).', 'success' if added else 'info')
    for message in skipped[:20]:
        flash(f'Skipped: {message}', 'warning')
    if added:
        refresh_roster_in_discord(role)
    return _back_to_roster()


@app.route('/roster/edit/<int:character_id>', methods=['POST'])
@login_required
def edit_roster_character(character_id):
    success, message = db.update_roster_character(character_id, request.form.get('name'),
                                                  request.form.get('title'), request.form.get('role'))
    if not success:
        return _roster_result(success, message)
    linked = (request.form.get('linked_id') or '').strip()
    link_ok, link_message = db.set_roster_link(character_id, int(linked) if linked.isdigit() else None)
    if link_message:
        flash(link_message, 'success' if link_ok else 'danger')
    return _roster_result(True, message)


@app.route('/roster/taken/<int:character_id>', methods=['POST'])
@login_required
def roster_mark_taken(character_id):
    player_id = (request.form.get('player_id') or '').strip()
    if player_id and not player_id.isdigit():
        flash('The Discord user ID must be a number!', 'danger')
        return _back_to_roster()
    return _roster_result(*db.set_roster_taken(character_id, player_id or None,
                                               (request.form.get('player_name') or '').strip() or None))


@app.route('/roster/reserve/<int:character_id>', methods=['POST'])
@login_required
def roster_reserve(character_id):
    try:  # <input type="datetime-local"> sends e.g. 2026-12-10T18:00 (the Pi's local time)
        until = datetime.datetime.strptime(request.form.get('expires_at') or '', '%Y-%m-%dT%H:%M')
    except ValueError:
        flash('Please choose a valid end date!', 'danger')
        return _back_to_roster()
    return _roster_result(*db.set_roster_reserved(character_id, request.form.get('player_id'),
                                                  (request.form.get('player_name') or '').strip() or None,
                                                  int(until.timestamp())))


@app.route('/roster/release/<int:character_id>', methods=['POST'])
@login_required
def roster_release(character_id):
    return _roster_result(*db.release_roster_character(character_id))


@app.route('/roster/delete/<int:character_id>', methods=['POST'])
@login_required
def roster_delete(character_id):
    return _roster_result(*db.delete_roster_character(character_id))


@app.route('/roster/release-player', methods=['POST'])
@login_required
def roster_release_player():
    player_id = (request.form.get('player_id') or '').strip()
    if not player_id.isdigit():
        flash('Please enter a Discord user ID!', 'danger')
        return _back_to_roster()
    names = db.release_player(player_id)
    if not names:
        flash('That user has no characters on the roster.', 'info')
        return _back_to_roster()
    return _roster_result(True, f"Released: {', '.join(names)}")


@app.route('/roster/refresh/<role_slug>', methods=['POST'])
@login_required
def roster_refresh(role_slug):
    role = _role_from_slug(role_slug)
    refresh_roster_in_discord(role)
    flash(f'{role} roster sent to Discord.', 'info')
    return _back_to_roster()


# ==================== ACTIVITY CHECK ====================

@app.route('/activity-check')
@login_required
def activity_check():
    current_month = datetime.datetime.now().strftime('%Y-%m')
    month = request.args.get('month', current_month)
    all_months = db.get_all_checked_months()
    if current_month not in all_months:
        all_months = [current_month] + all_months
    db.sync_activity_with_roster()  # taken roster characters in, released ones out
    refresh_player_names(c['discord_user_id'] for c in db.get_all_activity_characters())
    characters = db.get_all_activity_characters()
    counts = {'roster': sum(1 for c in characters if not c['is_oc']),
              'oc': sum(1 for c in characters if c['is_oc'])}
    return render_template(
        'activity_check.html',
        counts=counts,
        characters=characters,
        statuses=db.get_activity_statuses_for_month(month),
        all_months=all_months,
        selected_month=month,
        current_month=current_month,
        inactive_list=db.get_inactive_characters_for_month(month))


@app.route('/activity-check/add-character', methods=['POST'])
@login_required
def add_activity_character():
    character_names = request.form.getlist('character_name[]')
    roles = request.form.getlist('role[]')
    discord_username = request.form.get('discord_username')
    discord_user_id = request.form.get('discord_user_id')
    month = request.form.get('month')

    added, errors = 0, []
    for i, character_name in enumerate(character_names):
        character_name = character_name.strip()
        if not character_name:
            continue
        role = roles[i].strip() if i < len(roles) else ''
        success, message = db.add_activity_character(character_name, role, discord_username, discord_user_id)
        if success:
            added += 1
        else:
            errors.append(f"{character_name}: {message}")

    if added == 0:
        flash('No characters were added — at least one character name is required!', 'danger')
    else:
        flash(f'Added {added} character(s)!' + (f' ({len(errors)} failed)' if errors else ''), 'success')
    for err in errors:
        flash(err, 'danger')
    return redirect(url_for('activity_check', month=month))


@app.route('/activity-check/remove-character/<int:character_id>', methods=['POST'])
@login_required
def remove_activity_character(character_id):
    month = request.form.get('month')
    success, message = db.remove_activity_character(character_id)
    flash(message, 'success' if success else 'danger')
    return redirect(url_for('activity_check', month=month))


@app.route('/activity-check/save-month', methods=['POST'])
@login_required
def save_activity_month():
    month = request.form.get('month')
    try:
        statuses = json.loads(request.form.get('statuses_json', '{}'))
    except (ValueError, TypeError):
        flash('Could not read the changes — nothing was saved.', 'danger')
        return redirect(url_for('activity_check', month=month))
    if not month:
        flash('Missing month — nothing was saved.', 'danger')
        return redirect(url_for('activity_check', month=month))

    saved, errors = 0, []
    for character_id, status in statuses.items():
        if status not in ('active', 'inactive', 'hiatus'):
            continue
        success, message = db.set_activity_status(int(character_id), month, status)
        if success:
            saved += 1
        else:
            errors.append(message)

    flash(f'Saved {saved} status update(s) for {month}!' if saved else 'No changes to save.',
          'success' if saved else 'info')
    for err in errors:
        flash(err, 'danger')
    return redirect(url_for('activity_check', month=month))


# ==================== EMBEDS & WELCOME MESSAGE ====================

EMBED_COLUMNS = "id, name, title, description, color, footer_text, image_url, thumbnail_url, channel_id, message_id"


def _get_embed(embed_id):
    rows = query(f"SELECT {EMBED_COLUMNS} FROM embeds WHERE id = ?", (embed_id,))
    return rows[0] if rows else None


def _embed_payload(embed):
    return {'title': embed[2], 'description': embed[3], 'color': embed[4], 'footer_text': embed[5],
            'image_url': embed[6], 'thumbnail_url': embed[7]}


def _upload_image(field, label):
    """Upload a file from the form via the bot. Returns the URL or None."""
    file = request.files.get(field)
    if not file or not file.filename:
        return None
    try:
        response = bot_api('upload_image', timeout=10,
                           files={'file': (file.filename, file.stream, file.mimetype)})
        if response.status_code == 200:
            flash(f'{label} uploaded successfully!', 'success')
            return response.json().get('url')
        flash(f'Failed to upload {label.lower()}: {response.text}', 'danger')
    except Exception as e:
        flash(f'Error uploading {label.lower()}: {e}', 'danger')
    return None


@app.route('/embeds')
@login_required
def embeds():
    embeds_list = query(f"SELECT {EMBED_COLUMNS} FROM embeds ORDER BY id DESC")
    welcome = query("SELECT id, enabled, embed_id, channel_id FROM welcome_settings WHERE id = 1")
    return render_template('embeds.html', embeds=embeds_list, welcome_settings=welcome[0] if welcome else None)


@app.route('/embeds/welcome/settings', methods=['POST'])
@login_required
def update_welcome_settings():
    enabled = request.form.get('enabled') == '1'
    embed_id = request.form.get('embed_id') or None
    channel_id = request.form.get('channel_id')
    try:
        execute("INSERT OR REPLACE INTO welcome_settings (id, enabled, embed_id, channel_id) VALUES (1, ?, ?, ?)",
                (enabled, embed_id, channel_id))
        flash('Welcome settings updated!', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('embeds'))


@app.route('/embeds/create', methods=['POST'])
@login_required
def create_embed():
    name = request.form.get('name')
    image_url = _upload_image('image_file', 'Image') or request.form.get('image_url')
    thumbnail_url = _upload_image('thumbnail_file', 'Thumbnail') or request.form.get('thumbnail_url')
    if name:
        try:
            execute("INSERT INTO embeds (name, title, description, color, footer_text, image_url, thumbnail_url) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (name, request.form.get('title'), request.form.get('description'),
                     request.form.get('color', '#000000').lstrip('#'), request.form.get('footer_text'),
                     image_url, thumbnail_url))
            flash(f'Embed "{name}" created!', 'success')
        except Exception as e:
            flash(f'Error: {e}', 'danger')
    return redirect(url_for('embeds'))


@app.route('/embeds/edit/<int:embed_id>', methods=['POST'])
@login_required
def edit_embed(embed_id):
    name = request.form.get('name')
    if name:
        try:
            execute("UPDATE embeds SET name = ?, title = ?, description = ?, color = ?, footer_text = ?, "
                    "image_url = ?, thumbnail_url = ? WHERE id = ?",
                    (name, request.form.get('title'), request.form.get('description'),
                     request.form.get('color', '#000000').lstrip('#'), request.form.get('footer_text'),
                     request.form.get('image_url'), request.form.get('thumbnail_url'), embed_id))
            flash('Embed updated!', 'success')
        except Exception as e:
            flash(f'Error: {e}', 'danger')
    return redirect(url_for('embeds'))


@app.route('/embeds/delete/<int:embed_id>', methods=['POST'])
@login_required
def delete_embed(embed_id):
    try:
        execute("DELETE FROM embeds WHERE id = ?", (embed_id,))
        flash('Embed deleted!', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('embeds'))


@app.route('/embeds/send/<int:embed_id>', methods=['POST'])
@login_required
def send_embed(embed_id):
    channel_id = request.form.get('channel_id')
    if not channel_id:
        flash('Channel ID is required!', 'danger')
        return redirect(url_for('embeds'))
    embed = _get_embed(embed_id)
    if not embed:
        flash('Embed not found!', 'danger')
        return redirect(url_for('embeds'))
    try:
        response = bot_api('send_embed', json={'channel_id': channel_id, 'reaction_roles': [],
                                               **_embed_payload(embed)})
        if response.status_code == 200:
            execute("UPDATE embeds SET channel_id = ?, message_id = ? WHERE id = ?",
                    (channel_id, response.json().get('message_id'), embed_id))
            flash('Embed sent to channel!', 'success')
        else:
            flash(f'Failed to send embed: {response.text}', 'danger')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('embeds'))


@app.route('/embeds/update/<int:embed_id>', methods=['POST'])
@login_required
def update_embed_message(embed_id):
    embed = _get_embed(embed_id)
    if not embed or not embed[9]:
        flash('Embed not sent yet or message ID not found!', 'danger')
        return redirect(url_for('embeds'))
    try:
        response = bot_api('update_embed', json={'channel_id': embed[8], 'message_id': embed[9],
                                                 **_embed_payload(embed)})
        if response.status_code == 200:
            flash('Embed updated in channel!', 'success')
        else:
            flash(f'Failed to update embed: {response.text}', 'danger')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('embeds'))


# ==================== REACTION ROLES ====================

def _sync_reactions(message_id):
    """Re-sync the emoji reactions on a Discord message with the saved mappings."""
    embed_rows = query("SELECT channel_id FROM embeds WHERE message_id = ?", (message_id,))
    if not embed_rows or not embed_rows[0][0]:
        flash('Mappings saved, but no channel is linked to this message, so Discord was not updated.', 'warning')
        return
    roles = [{'emoji': r[0], 'role_id': r[1]}
             for r in query("SELECT emoji, role_id FROM reaction_roles WHERE message_id = ?", (message_id,))]
    try:
        response = bot_api('update_reactions', json={'channel_id': embed_rows[0][0], 'message_id': message_id,
                                                     'reaction_roles': roles})
        if response.status_code != 200:
            flash(f'Mappings saved but failed to sync reactions in Discord: {response.text}', 'warning')
    except Exception as e:
        flash(f'Mappings saved but failed to sync reactions in Discord: {e}', 'warning')


@app.route('/reaction-roles')
@login_required
def reaction_roles():
    embeds_list = query(f"SELECT {EMBED_COLUMNS} FROM embeds ORDER BY id DESC")
    messages = query("""
        SELECT rr.message_id, MAX(e.name)
        FROM reaction_roles rr LEFT JOIN embeds e ON e.message_id = rr.message_id
        GROUP BY rr.message_id""")
    reaction_messages = [{
        'message_id': message_id,
        'embed_name': embed_name or 'Unknown Embed',
        'roles': [{'emoji': r[0], 'role_id': r[1]}
                  for r in query("SELECT emoji, role_id FROM reaction_roles WHERE message_id = ?", (message_id,))],
    } for message_id, embed_name in messages]
    return render_template('reaction_roles.html', embeds=embeds_list, reaction_messages=reaction_messages)


@app.route('/reaction-roles/create', methods=['POST'])
@login_required
def create_reaction_role():
    embed_id = request.form.get('embed_id')
    channel_id = request.form.get('channel_id')
    if not embed_id or not channel_id:
        flash('Embed and Channel ID are required!', 'danger')
        return redirect(url_for('reaction_roles'))
    embed = _get_embed(embed_id)
    if not embed:
        flash('Embed not found!', 'danger')
        return redirect(url_for('reaction_roles'))

    roles = [{'emoji': e.strip(), 'role_id': r.strip()}
             for e, r in zip(request.form.getlist('emoji[]'), request.form.getlist('role_id[]'))
             if e.strip() and r.strip()]
    if not roles:
        flash('At least one reaction role mapping is required!', 'danger')
        return redirect(url_for('reaction_roles'))

    try:
        response = bot_api('send_embed', json={'channel_id': channel_id, 'reaction_roles': roles,
                                               **_embed_payload(embed)})
        if response.status_code != 200:
            flash(f'Failed to send reaction role message: {response.text}', 'danger')
            return redirect(url_for('reaction_roles'))
        message_id = response.json().get('message_id')
        with db._conn() as conn:
            conn.execute("UPDATE embeds SET channel_id = ?, message_id = ? WHERE id = ?",
                         (channel_id, message_id, embed_id))
            conn.executemany("INSERT OR REPLACE INTO reaction_roles (message_id, emoji, role_id) VALUES (?, ?, ?)",
                             [(message_id, rr['emoji'], rr['role_id']) for rr in roles])
            conn.commit()
        flash(f'Reaction role message created with {len(roles)} role(s)!', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('reaction_roles'))


@app.route('/reaction-roles/link-existing', methods=['POST'])
@login_required
def link_existing_reaction_message():
    """Link a message that already exists in Discord to an embed (nothing is sent)."""
    embed_id = request.form.get('embed_id')
    message_id = (request.form.get('message_id') or '').strip()
    channel_id = (request.form.get('channel_id') or '').strip()
    if not (embed_id and message_id and channel_id):
        flash('Embed, message ID and channel ID are required!', 'danger')
        return redirect(url_for('reaction_roles'))
    try:
        execute("UPDATE embeds SET channel_id = ?, message_id = ? WHERE id = ?", (channel_id, message_id, embed_id))
        flash('Message linked! Add roles to it below.', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('reaction_roles'))


@app.route('/reaction-roles/add-role/<message_id>', methods=['POST'])
@login_required
def add_reaction_role(message_id):
    emoji = (request.form.get('emoji') or '').strip()
    role_id = (request.form.get('role_id') or '').strip()
    if not emoji or not role_id:
        flash('Emoji and role ID are required!', 'danger')
        return redirect(url_for('reaction_roles'))
    try:
        execute("INSERT OR REPLACE INTO reaction_roles (message_id, emoji, role_id) VALUES (?, ?, ?)",
                (message_id, emoji, role_id))
        _sync_reactions(message_id)
        flash(f'Added {emoji} → {role_id}!', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('reaction_roles'))


@app.route('/reaction-roles/remove-role/<message_id>', methods=['POST'])
@login_required
def remove_reaction_role(message_id):
    emoji = request.form.get('emoji')
    try:
        execute("DELETE FROM reaction_roles WHERE message_id = ? AND emoji = ?", (message_id, emoji))
        _sync_reactions(message_id)
        flash(f'Removed {emoji}!', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('reaction_roles'))


@app.route('/reaction-roles/delete/<message_id>', methods=['POST'])
@login_required
def delete_reaction_role(message_id):
    try:
        execute("DELETE FROM reaction_roles WHERE message_id = ?", (message_id,))
        flash('Reaction roles deleted from message!', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('reaction_roles'))


# ==================== IP WHITELIST (optional) ====================

ENABLE_IP_WHITELIST = False
ALLOWED_IPS = ['127.0.0.1', '192.168.1.100']


@app.before_request
def limit_remote_addr():
    if ENABLE_IP_WHITELIST and request.remote_addr not in ALLOWED_IPS:
        return "Access Denied", 403


if __name__ == '__main__':
    print(f"Dashboard for {config.BOT_NAME} on port {config.DASHBOARD_PORT} (login enabled: {LOGIN_ENABLED})")
    # debug=False: the debug reloader starts a second process = a second SQLite writer
    app.run(debug=False, host='0.0.0.0', port=config.DASHBOARD_PORT)
