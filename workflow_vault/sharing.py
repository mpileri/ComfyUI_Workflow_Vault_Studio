"""Plug-and-play team sharing for a vault folder.

Sharing a vault writes <vault>/shared_vault.json (studio.MARKER_FILENAME).
Every ComfyUI that opens the folder then treats it as a team vault (see
studio.policy). No group or user setup is needed: in the default "open" mode
everything the vault writes is readable and writable by everyone who can open
the folder.

Only a file's owner (or root) can change its permissions, so each artist's
ComfyUI repairs its own files: once when it first opens a shared vault (in the
background), and on demand from the health panel. That also migrates a vault
made with the single-user extension, whose files were owner-only: as each
artist opens it with this version, their files open up.
"""

import os
import stat
import threading

from . import studio, utils

_HEALED = set()  # (realpath, mode) already repaired by this process
_HEAL_LOCK = threading.Lock()


def enable(vault_root, permissions="open", group=None, admins=None):
    """Turn a vault into a shared team vault, or change its sharing settings.
    Returns (policy, error)."""
    if permissions not in studio.PERMISSION_MODES:
        return None, "Unknown permissions mode."
    group = (group or "").strip() or None
    if permissions == "group":
        if not group:
            return None, "Enter the Unix group that every artist belongs to."
        if studio._resolve_gid(group) is None:
            return None, f"There is no group called “{group}” on this computer."
    admins = studio.clean_admins(admins)
    me = studio.current_user().lower()
    if admins and me not in admins:
        return None, f"Add yourself ({me}) to the admins, or you couldn't change these settings again."

    existing = studio.read_marker(vault_root) or {}
    now = utils.now_iso()
    utils.atomic_write_json(studio.marker_path(vault_root), {
        "shared": True,
        "permissions": permissions,
        "group": group if permissions == "group" else None,
        "admins": admins,
        "trash": "vault",
        "created_by": existing.get("created_by") or me,
        "created_at": existing.get("created_at") or now,
        "updated_by": me,
        "updated_at": now,
    })
    pol = studio.policy(vault_root)
    heal(vault_root, pol)  # including the marker itself, written before the vault was shared
    return pol, None


def disable(vault_root):
    """Make the vault private again. Files keep their shared permissions;
    nothing else changes."""
    try:
        os.remove(studio.marker_path(vault_root))
    except FileNotFoundError:
        pass


def _walk(root):
    yield root, True
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            yield os.path.join(dirpath, name), True
        for name in filenames:
            yield os.path.join(dirpath, name), False


def heal(vault_root, pol=None):
    """Give everything this user owns in the vault the shared permissions.
    Returns {"fixed": n, "owned": m}. Never raises."""
    pol = pol or studio.policy(vault_root)
    fixed = owned = 0
    if not pol or os.name == "nt" or not os.path.isdir(vault_root):
        return {"fixed": 0, "owned": 0}
    euid = os.geteuid()
    for path, _is_dir in _walk(vault_root):
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode) or (euid != 0 and st.st_uid != euid):
            continue
        owned += 1
        if utils.share_path(path, pol):
            fixed += 1
    return {"fixed": fixed, "owned": owned}


def heal_in_background(vault_root):
    """heal() once per process per vault (and sharing mode), off the request
    path: a big vault on a network drive takes a while to walk."""
    pol = studio.policy(vault_root)
    if not pol or os.name == "nt":
        return
    key = (os.path.realpath(vault_root), pol["mode"], pol["group"])
    with _HEAL_LOCK:
        if key in _HEALED:
            return
        _HEALED.add(key)

    def run():
        result = heal(vault_root, pol)
        if result["fixed"]:
            print(f"[Workflow Vault] Shared {result['fixed']} of your files in {vault_root} with the team.")

    threading.Thread(target=run, name="workflow-vault-heal", daemon=True).start()


def _owner_name(uid):
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return str(uid)


MAX_SAMPLES = 5


def scan(vault_root, pol=None):
    """Find what the rest of the team can't fully use, grouped by owner.

    Returns {"problems": n, "by_owner": {owner: {"count", "samples": [rel paths]}},
    "unreadable": [rel paths this user can't read]}."""
    pol = pol or studio.policy(vault_root)
    result = {"problems": 0, "by_owner": {}, "unreadable": []}
    if not pol or os.name == "nt" or not os.path.isdir(vault_root):
        return result
    for path, is_dir in _walk(vault_root):
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            continue
        want = pol["dir_mode"] if is_dir else pol["file_mode"]
        bad = (stat.S_IMODE(st.st_mode) & want) != want
        if pol["gid"] is not None and st.st_gid != pol["gid"]:
            bad = True
        rel = os.path.relpath(path, vault_root).replace("\\", "/")
        if not os.access(path, os.R_OK | (os.X_OK if is_dir else 0)) and len(result["unreadable"]) < MAX_SAMPLES:
            result["unreadable"].append(rel)
        if not bad:
            continue
        result["problems"] += 1
        owner = result["by_owner"].setdefault(_owner_name(st.st_uid), {"count": 0, "samples": []})
        owner["count"] += 1
        if len(owner["samples"]) < MAX_SAMPLES:
            owner["samples"].append(rel)
    return result
