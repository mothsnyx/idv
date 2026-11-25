"""Builds the roster messages from the database and the layout set on the dashboard.

Hunters and Survivors each have their own forum post and their own layout
(Roster page -> one tab per role). Used by the bot (to post the roster) and by
the dashboard (for the preview), so both always look the same.
"""
import config

# Two em spaces: Discord keeps these at the start of a line (normal spaces can get swallowed)
INDENT = "  "
LIMITS = {"embed": 4000, "message": 1900}  # Discord: 4096 per embed, 2000 per message
SETTINGS_PREFIX = "roster."

# Settings every role has its own copy of (stored as "<Role>_<key>")
LAYOUT_KEYS = ("style", "line_format", "reserved_suffix", "alternate_indent", "blank_lines", "empty_text",
               "channel_id", "title", "color", "intro", "footer", "thumbnail", "image")


def get_layout(db, role):
    """Layout of one role's roster: {key: value} for every key in LAYOUT_KEYS."""
    saved = db.get_settings(SETTINGS_PREFIX)
    layout = {}
    for key in LAYOUT_KEYS:
        role_key = f"{role}_{key}"
        if role_key in saved:
            layout[key] = saved[role_key]
        elif key in saved:  # layout saved before Hunters and Survivors were split
            layout[key] = saved[key]
        else:
            layout[key] = config.ROSTER_DEFAULTS.get(role_key, config.ROSTER_DEFAULTS.get(key, ""))
    if layout["style"] not in LIMITS:
        layout["style"] = "embed"
    return layout


def save_layout(db, role, values):
    db.set_settings(SETTINGS_PREFIX, {f"{role}_{key}": values[key] for key in LAYOUT_KEYS if key in values})


def post_id(db, role):
    """The forum post (thread) ID of a role's roster, or 0 if none is set."""
    value = str(get_layout(db, role)["channel_id"]).strip()
    return int(value) if value.isdigit() else 0


def default_mention(character):
    return f"<@{character['player_id']}>"


def format_line(layout, character, mention):
    player = mention(character) if character['player_id'] else ""
    try:
        line = layout['line_format'].format(character=character['display'], name=character['name'],
                                            title=character['title'] or "", player=player)
    except (KeyError, IndexError, ValueError):  # broken placeholder in the line format
        line = f"{character['display']} ― {player}"
    line = line.rstrip()
    if character['status'] == 'reserved':
        line += layout['reserved_suffix']
    return line


def build_roster(db, role, mention=default_mention):
    """One dict per Discord message of this role's roster: style, title, text, color, footer, thumbnail, image."""
    layout = get_layout(db, role)
    style = layout['style']
    separator = "\n\n" if layout.get('blank_lines') == "1" else "\n"

    lines = []
    for i, character in enumerate(db.get_roster(role)):
        line = format_line(layout, character, mention)
        if layout.get('alternate_indent') == "1" and i % 2:
            line = INDENT + line
        lines.append(line)
    if not lines:
        lines = [layout['empty_text']]

    title, intro = layout['title'].strip(), layout['intro'].strip()
    # Plain messages have no embed title, so the title becomes a bold first line
    header = intro if style == "embed" else "\n".join(h for h in (f"**{title}**" if title else "", intro) if h)

    # Split into several messages if the list is too long for one
    chunks, chunk = [], header
    for line in lines:
        piece = separator + line if chunk else line
        if chunk and chunk != header and len(chunk) + len(piece) > LIMITS[style]:
            chunks.append(chunk)
            chunk = line
        else:
            chunk += piece
    chunks.append(chunk)

    parts = []
    for n, text in enumerate(chunks):
        first, last = n == 0, n == len(chunks) - 1
        parts.append({
            'role': role,
            'style': style,
            'title': title if first else "",
            'text': text,
            'color': layout['color'].strip(),
            'footer': layout['footer'].strip() if last else "",
            'thumbnail': layout['thumbnail'].strip() if first else "",
            'image': layout['image'].strip() if last else "",
        })
    return parts


def parse_color(value):
    try:
        return int((value or "000000").lstrip('#'), 16)
    except ValueError:
        return 0
