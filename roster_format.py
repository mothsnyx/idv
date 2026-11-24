"""Builds the roster messages from the database and the layout set on the dashboard.

Used by the bot (to post the roster) and by the dashboard (for the preview),
so both always look the same.
"""
import config

# Two em spaces: Discord keeps these at the start of a line (normal spaces can get swallowed)
INDENT = "  "
LIMITS = {"embed": 4000, "message": 1900}  # Discord: 4096 per embed, 2000 per message
SETTINGS_PREFIX = "roster."


def get_layout(db):
    layout = dict(config.ROSTER_DEFAULTS)
    layout.update(db.get_settings(SETTINGS_PREFIX))
    if layout.get("style") not in LIMITS:
        layout["style"] = "embed"
    return layout


def default_mention(character):
    return f"<@{character['player_id']}>"


def _format_line(layout, character, mention):
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


def build_roster(db, mention=default_mention):
    """One dict per Discord message: style, title, text, color, footer, thumbnail, image."""
    layout = get_layout(db)
    style = layout['style']
    separator = "\n\n" if layout.get('blank_lines') == "1" else "\n"
    parts = []

    for role in config.ROLES:
        def setting(key):
            return (layout.get(f"{role}_{key}") or "").strip()

        lines = []
        for i, character in enumerate(db.get_roster(role)):
            line = _format_line(layout, character, mention)
            if layout.get('alternate_indent') == "1" and i % 2:
                line = INDENT + line
            lines.append(line)
        if not lines:
            lines = [layout['empty_text']]

        # Plain messages have no embed title, so the title becomes a bold first line
        header_lines = [setting('intro')] if style == "embed" else [
            f"**{setting('title')}**" if setting('title') else "", setting('intro')]
        header = "\n".join(h for h in header_lines if h)

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

        for n, text in enumerate(chunks):
            first, last = n == 0, n == len(chunks) - 1
            parts.append({
                'role': role,
                'style': style,
                'title': setting('title') if first else "",
                'text': text,
                'color': setting('color'),
                'footer': setting('footer') if last else "",
                'thumbnail': setting('thumbnail') if first else "",
                'image': setting('image') if last else "",
            })
    return parts


def parse_color(value):
    try:
        return int((value or "000000").lstrip('#'), 16)
    except ValueError:
        return 0
