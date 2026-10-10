"""Per-artist state inside a shared (studio) vault.

Favorites and view settings (card size, sort, which card fields show...) are
personal. Upstream stores them in each entry's manifest and the shared
vault_settings.json, so in a studio one artist starring a workflow, or
switching to small cards, changed it for everybody. In studio mode they live
in <vault>/.users/<login>.json instead: one file per artist, so nobody's
choices overwrite anyone else's, and they follow the artist to any machine.
"""

import os
import re

from . import studio, utils

# vault_settings.json keys that are a matter of personal taste rather than
# vault policy. In studio mode they are read from and saved to the artist's
# own file; everything else (accent color, compression...) stays vault-wide.
PERSONAL_SETTING_KEYS = ("show_archived", "grid_columns", "card_size", "window_size", "sort", "card_fields")


def _path(vault_root):
    name = re.sub(r"[^A-Za-z0-9._-]", "_", studio.current_user()) or "unknown"
    return os.path.join(vault_root, studio.USERS_DIRNAME, f"{name}.json")


def load(vault_root):
    data = utils.read_json(_path(vault_root), default=None)
    if not isinstance(data, dict):
        data = {}
    favorites = data.get("favorites")
    settings = data.get("settings")
    return {
        "user": studio.current_user(),
        "favorites": [f for f in favorites if isinstance(f, str)] if isinstance(favorites, list) else [],
        "settings": settings if isinstance(settings, dict) else {},
    }


def _save(vault_root, data):
    utils.atomic_write_json(_path(vault_root), data)


def favorites(vault_root):
    return set(load(vault_root)["favorites"])


def set_favorite(vault_root, entry_id, on):
    data = load(vault_root)
    favs = [f for f in data["favorites"] if f != entry_id]
    if on:
        favs.append(entry_id)
    if favs != data["favorites"]:
        data["favorites"] = favs
        _save(vault_root, data)


def personal_settings(vault_root):
    stored = load(vault_root)["settings"]
    return {k: stored[k] for k in PERSONAL_SETTING_KEYS if k in stored}


def update_settings(vault_root, updates):
    updates = {k: v for k, v in updates.items() if k in PERSONAL_SETTING_KEYS}
    if not updates:
        return
    data = load(vault_root)
    merged = {**data["settings"], **updates}
    if merged != data["settings"]:
        data["settings"] = merged
        _save(vault_root, data)
