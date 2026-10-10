"""Shared vaults: one vault on network storage, used by many artists who each
run their own ComfyUI as their own OS user.

The upstream vault assumes a single user, and on a shared folder that breaks:

- every file was created owner-only (0600, a side effect of tempfile.mkstemp)
  and every folder 0755, so a workflow saved by one artist could not be
  opened, versioned, or deleted by anyone else;
- each ComfyUI process only serialized its own writes, so two artists saving
  at the same time could clobber each other's manifest updates;
- deletes went to the deleting artist's personal OS trash, or were permanent
  when the network mount had no trash at all.

A vault becomes shared in one of two ways:

1. Plug and play, no IT involved: a `shared_vault.json` file inside the vault
   folder, written when someone ticks "Shared team vault" in the UI (see
   sharing.py). Anyone whose ComfyUI points at that folder joins it
   automatically. Its files are made readable and writable by everyone who can
   open the folder ("open" permissions), so no Unix group is needed; the
   optional "group" mode limits that to one group instead.
2. Managed, optional: a studio config that defines the studio's vaults
   centrally (locked in the UI), plus a shared group and ShotGrid:
     1. the file named by $WORKFLOW_VAULT_STUDIO_CONFIG
     2. studio_config.json next to this extension
   with environment shortcuts usable with or without a file:
     WORKFLOW_VAULT_ROOT   one shared vault folder (shown as "Studio")
     WORKFLOW_VAULT_GROUP  the Unix group every artist shares
   See studio_config.example.json for every key.

WORKFLOW_VAULT_USER overrides the name recorded as author (default: the OS login).

Any other vault stays private and behaves exactly like upstream.
"""

import getpass
import json
import os
import re
import sys
import threading

EXTENSION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(EXTENSION_DIR, "studio_config.json")

# Bookkeeping inside a shared vault root.
MARKER_FILENAME = "shared_vault.json"  # makes a vault shared (plug and play)
TRASH_DIRNAME = ".trash"      # deleted entries/versions/examples, restorable
USERS_DIRNAME = ".users"      # per-artist favorites and view settings
LOCK_FILENAME = ".wv_lock"    # cross-process write lock

# "open": everyone who can open the folder can read and write (no IT needed).
OPEN_FILE_MODE = 0o666        # rw-rw-rw-
OPEN_DIR_MODE = 0o777         # rwxrwxrwx (no sticky bit: artists must be able to rename/delete each other's files)
# "group": only the studio's Unix group (IT creates it).
GROUP_FILE_MODE = 0o664       # rw-rw-r--
GROUP_DIR_MODE = 0o2775       # rwxrwsr-x (setgid: new files inherit the group)
PERMISSION_MODES = ("open", "group")

_ENV_KEYS = ("WORKFLOW_VAULT_STUDIO_CONFIG", "WORKFLOW_VAULT_ROOT", "WORKFLOW_VAULT_GROUP")

_LOCK = threading.Lock()
_DEPLOYMENT_CACHE = {"key": None, "config": None}
_POLICY_CACHE = {}  # cache key -> policy
_REALPATHS = {}     # vault root as given -> realpath
_ACTIVE = {"root": None}


def config_path():
    return os.environ.get("WORKFLOW_VAULT_STUDIO_CONFIG") or DEFAULT_CONFIG_PATH


def _platform_key():
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _resolve_path(value):
    """A path is a string, or {"linux": ..., "windows": ..., "darwin": ...}
    so one config can serve a mixed-OS studio where the share mounts in
    different places."""
    if isinstance(value, dict):
        value = value.get(_platform_key()) or value.get("default")
    if isinstance(value, str) and value.strip():
        return os.path.normpath(os.path.expanduser(os.path.expandvars(value.strip())))
    return None


def _real(path):
    real = _REALPATHS.get(path)
    if real is None:
        real = os.path.normcase(os.path.realpath(path))
        _REALPATHS[path] = real
    return real


def _parse_mode(value, owner_bits):
    """Octal permission bits from "664", "0664", "0o2775" or 664, or None when
    unset/invalid. The owner always keeps `owner_bits`, so a typo can't lock
    the vault's own writer out."""
    if value is None or value == "":
        return None
    try:
        mode = int(str(value), 8)
    except ValueError:
        print(f"[Workflow Vault] Ignoring invalid permission mode {value!r} in studio config.")
        return None
    if not 0 <= mode <= 0o7777:
        return None
    return mode | owner_bits


def _resolve_gid(name):
    if not name or os.name == "nt":
        return None
    try:
        import grp

        if str(name).isdigit():
            return int(name)
        return grp.getgrnam(str(name)).gr_gid
    except (ImportError, KeyError):
        print(f"[Workflow Vault] Group {name!r} does not exist on this machine; "
              "files will keep the folder's group.")
        return None


def _clean_name(name, fallback):
    name = " ".join(str(name or "").split())[:60]
    return name or fallback


def clean_admins(admins):
    if not isinstance(admins, (list, tuple)):
        return []
    return sorted({str(a).strip().lower() for a in admins if str(a).strip()})


# ---------------------------------------------------------------------------
# Managed deployment (optional studio config)
# ---------------------------------------------------------------------------

def _normalize(raw, path, error):
    vaults = []
    seen = set()
    for i, v in enumerate(raw.get("vaults") or []):
        if not isinstance(v, dict):
            continue
        root = _resolve_path(v.get("path", v.get("vault_root")))
        if not root:
            continue
        vid = re.sub(r"[^A-Za-z0-9_.-]", "_", str(v.get("id") or f"studio_{i + 1}").strip())
        if not vid or vid in seen:
            continue
        seen.add(vid)
        vaults.append({"id": vid, "name": _clean_name(v.get("name"), vid), "vault_root": root, "locked": True})

    env_root = _resolve_path(os.environ.get("WORKFLOW_VAULT_ROOT"))
    if env_root:
        vaults = [v for v in vaults if v["id"] != "studio"]
        vaults.insert(0, {"id": "studio", "name": "Studio", "vault_root": env_root, "locked": True})

    default_vault = raw.get("default_vault")
    if not any(v["id"] == default_vault for v in vaults):
        default_vault = vaults[0]["id"] if vaults else None

    allow_personal = raw.get("allow_personal_vaults")
    if not isinstance(allow_personal, bool):
        allow_personal = not vaults  # no studio vault defined -> artists bring their own

    return {
        "path": path,
        "error": error,
        "vaults": vaults,
        "default_vault": default_vault,
        "allow_personal_vaults": allow_personal,
        # Sharing policy for the managed vaults (see _make_policy).
        "permissions": raw.get("permissions"),
        "group": raw.get("group") or os.environ.get("WORKFLOW_VAULT_GROUP") or None,
        "file_mode": _parse_mode(raw.get("file_mode"), 0o600),
        "dir_mode": _parse_mode(raw.get("dir_mode"), 0o700),
        "trash": raw.get("trash"),
        "admins": clean_admins(raw.get("admins")),
        "shotgrid": raw.get("shotgrid") if isinstance(raw.get("shotgrid"), dict) else None,
    }


def deployment():
    """The managed studio config, or None when there isn't one.

    Cached, and re-read whenever the file or the environment changes, so the
    pipeline team can edit the config without restarting every ComfyUI."""
    path = config_path()
    try:
        st = os.stat(path)
        file_key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        file_key = (path, None, None)
    key = (file_key, tuple(os.environ.get(k) for k in _ENV_KEYS))
    with _LOCK:
        if _DEPLOYMENT_CACHE["key"] == key:
            return _DEPLOYMENT_CACHE["config"]

    raw, error, found = {}, None, file_key[1] is not None
    if found:
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("the top level must be a JSON object")
        except (OSError, ValueError) as e:
            # Keep the managed setup: silently falling back would send artists
            # off to private vaults nobody else can see.
            error = f"The studio config at {path} could not be read: {e}"
            print(f"[Workflow Vault] {error}")
            raw = {}

    if found or os.environ.get("WORKFLOW_VAULT_ROOT"):
        config = _normalize(raw, path if found else None, error)
    else:
        config = None
    with _LOCK:
        _DEPLOYMENT_CACHE["key"], _DEPLOYMENT_CACHE["config"] = key, config
    return config


def vaults():
    cfg = deployment()
    return [dict(v) for v in cfg["vaults"]] if cfg else []


def default_vault_id():
    cfg = deployment()
    return cfg["default_vault"] if cfg else None


def allow_personal_vaults():
    cfg = deployment()
    return True if cfg is None else cfg["allow_personal_vaults"]


# ---------------------------------------------------------------------------
# Per-vault sharing policy
# ---------------------------------------------------------------------------

def _make_policy(settings, managed, source):
    group = settings.get("group") or None
    mode = settings.get("permissions")
    if mode not in PERMISSION_MODES:
        mode = "group" if group else "open"
    if mode == "group":
        file_mode, dir_mode = GROUP_FILE_MODE, GROUP_DIR_MODE
    else:
        file_mode, dir_mode = OPEN_FILE_MODE, OPEN_DIR_MODE
    return {
        "enabled": True,
        "managed": managed,
        "source": source,
        "mode": mode,
        "group": group if mode == "group" else None,
        "gid": _resolve_gid(group) if mode == "group" else None,
        "file_mode": settings.get("file_mode") or file_mode,
        "dir_mode": settings.get("dir_mode") or dir_mode,
        "trash": settings.get("trash") if settings.get("trash") in ("vault", "os") else "vault",
        "admins": clean_admins(settings.get("admins")),
    }


def marker_path(vault_root):
    return os.path.join(vault_root, MARKER_FILENAME)


def read_marker(vault_root):
    """The vault's own sharing settings (a dict), or None when it has none."""
    try:
        with open(marker_path(vault_root), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("shared", True) else None


def policy(vault_root):
    """How a vault is shared, or None for a private vault.

    A vault listed in the managed studio config uses that config; any other
    vault is shared when it carries a shared_vault.json."""
    if not vault_root:
        return None
    dep = deployment()
    real = _real(vault_root)
    if dep and any(_real(v["vault_root"]) == real for v in dep["vaults"]):
        key = ("managed", real, id(dep))
        with _LOCK:
            hit = _POLICY_CACHE.get(key)
        if hit is None:
            hit = _make_policy(dep, True, dep["path"] or "environment")
            with _LOCK:
                _POLICY_CACHE[key] = hit
        return hit
    try:
        st = os.stat(marker_path(vault_root))
    except OSError:
        return None
    key = ("marker", real, st.st_mtime_ns, st.st_size)
    with _LOCK:
        hit = _POLICY_CACHE.get(key)
    if hit is None:
        data = read_marker(vault_root)
        hit = _make_policy(data, False, marker_path(vault_root)) if data is not None else False
        with _LOCK:
            _POLICY_CACHE[key] = hit
    return hit or None


def set_active_vault(vault_root):
    """Record which vault this ComfyUI is using. Writes that don't name a vault
    (atomic file writes, folder creation) follow its sharing policy."""
    _ACTIVE["root"] = vault_root


def active_vault():
    return _ACTIVE["root"]


def load():
    """The active vault's sharing policy, or None when it's private."""
    return policy(_ACTIVE["root"])


def enabled():
    return load() is not None


# ---------------------------------------------------------------------------
# Who is working
# ---------------------------------------------------------------------------

def current_user():
    """Who is saving. Each artist runs ComfyUI as themselves, so the OS login
    is the natural identity; WORKFLOW_VAULT_USER overrides it (e.g. when
    ComfyUI runs under a service account)."""
    name = os.environ.get("WORKFLOW_VAULT_USER")
    if not name:
        try:
            name = getpass.getuser()
        except Exception:
            name = ""
    return (name or "unknown").strip() or "unknown"


def is_admin(user=None, vault_root=None):
    """Admins manage vault-wide things (tags, compression, cleanup, projects,
    sharing) and can delete anything. With no admins listed, everyone is one."""
    pol = policy(vault_root) if vault_root else load()
    if not pol or not pol["admins"]:
        return True
    return (user or current_user()).lower() in pol["admins"]


def admins(vault_root=None):
    pol = policy(vault_root) if vault_root else load()
    return pol["admins"] if pol else []


def can_delete(*records):
    """Admins can delete anything; anyone else only what they created."""
    if is_admin():
        return True
    me = current_user().lower()
    return any(((r or {}).get("created_by") or "").lower() == me for r in records)


def user_config_path():
    """Per-artist settings (which vault is active, personal vaults) for a
    managed deployment, where the extension folder is often a shared install
    that can't hold one person's choices."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "comfyui-workflow-vault", "vault_config.json")


def trash_label():
    return "shared vault trash"


def public_info():
    """What the UI needs to adapt to the active vault's sharing."""
    dep = deployment()
    pol = load()
    info = {
        "enabled": bool(pol),
        "user": current_user(),
        "deployment": bool(dep),
        "allow_personal_vaults": allow_personal_vaults(),
        "config_path": dep["path"] if dep else None,
        "error": dep["error"] if dep else None,
        "is_admin": is_admin(),
        "admins": pol["admins"] if pol else [],
    }
    if pol:
        info.update({
            "managed": pol["managed"],
            "mode": pol["mode"],
            "group": pol["group"],
            "trash": pol["trash"],
        })
    return info
