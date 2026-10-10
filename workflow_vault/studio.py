"""Studio mode: one shared vault on network storage, used by many artists who
each run their own ComfyUI as their own OS user.

The upstream vault assumes a single user, and on a shared folder that breaks:

- every file was created owner-only (0600, a side effect of tempfile.mkstemp)
  and every folder 0755, so a workflow saved by one artist could not be
  opened, versioned, or deleted by anyone else;
- each ComfyUI process only serialized its own writes, so two artists saving
  at the same time could clobber each other's manifest updates;
- deletes went to the deleting artist's personal OS trash, or were permanent
  when the network mount had no trash at all.

Studio mode fixes all three. It switches on when a studio config is found;
without one the vault behaves exactly like upstream (single-user).

Where the config comes from (first match wins for the file):
  1. the file named by $WORKFLOW_VAULT_STUDIO_CONFIG
  2. studio_config.json next to this extension
Environment shortcuts, usable with or without a file:
  WORKFLOW_VAULT_ROOT   one shared vault folder (shown as "Studio")
  WORKFLOW_VAULT_GROUP  the Unix group every artist shares
  WORKFLOW_VAULT_USER   name recorded as author (default: the OS login)

See studio_config.example.json for every key.
"""

import getpass
import json
import os
import re
import sys
import threading

EXTENSION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(EXTENSION_DIR, "studio_config.json")

# Hidden bookkeeping inside a shared vault root.
TRASH_DIRNAME = ".trash"      # deleted entries/versions/examples, restorable
USERS_DIRNAME = ".users"      # per-artist favorites and view settings
LOCK_FILENAME = ".wv_lock"    # cross-process write lock

DEFAULT_FILE_MODE = 0o664     # rw-rw-r--
DEFAULT_DIR_MODE = 0o2775     # rwxrwsr-x (setgid: new files inherit the group)

_ENV_KEYS = ("WORKFLOW_VAULT_STUDIO_CONFIG", "WORKFLOW_VAULT_ROOT", "WORKFLOW_VAULT_GROUP")

_LOCK = threading.Lock()
_CACHE = {"key": None, "config": None}


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


def _parse_mode(value, default, owner_bits):
    """Octal permission bits from "664", "0664", "0o2775" or 664. The owner
    always keeps `owner_bits`, so a typo can't lock the vault's own writer out."""
    if value is None or value == "":
        return default
    try:
        mode = int(str(value), 8)
    except ValueError:
        print(f"[Workflow Vault] Ignoring invalid permission mode {value!r} in studio config.")
        return default
    if not 0 <= mode <= 0o7777:
        return default
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
        print(f"[Workflow Vault] Studio group {name!r} does not exist on this machine; "
              "files will keep the folder's group.")
        return None


def _clean_name(name, fallback):
    name = " ".join(str(name or "").split())[:60]
    return name or fallback


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

    group = raw.get("group") or os.environ.get("WORKFLOW_VAULT_GROUP") or None
    trash = raw.get("trash") if raw.get("trash") in ("vault", "os") else "vault"
    admins = sorted({str(a).strip().lower() for a in raw.get("admins") or [] if str(a).strip()})

    return {
        "enabled": True,
        "path": path,
        "error": error,
        "vaults": vaults,
        "default_vault": default_vault,
        "allow_personal_vaults": allow_personal,
        "group": group,
        "gid": _resolve_gid(group),
        "file_mode": _parse_mode(raw.get("file_mode"), DEFAULT_FILE_MODE, 0o600),
        "dir_mode": _parse_mode(raw.get("dir_mode"), DEFAULT_DIR_MODE, 0o700),
        "trash": trash,
        "admins": admins,
    }


def load():
    """The resolved studio config, or None when studio mode is off.

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
        if _CACHE["key"] == key:
            return _CACHE["config"]

    raw, error, found = {}, None, file_key[1] is not None
    if found:
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("the top level must be a JSON object")
        except (OSError, ValueError) as e:
            # Stay in studio mode: silently falling back to single-user mode
            # would send artists off to private vaults nobody else can see.
            error = f"The studio config at {path} could not be read: {e}"
            print(f"[Workflow Vault] {error}")
            raw = {}

    if found or os.environ.get("WORKFLOW_VAULT_ROOT"):
        config = _normalize(raw, path if found else None, error)
    else:
        config = None
    with _LOCK:
        _CACHE["key"], _CACHE["config"] = key, config
    return config


def enabled():
    return load() is not None


def vaults():
    cfg = load()
    return [dict(v) for v in cfg["vaults"]] if cfg else []


def default_vault_id():
    cfg = load()
    return cfg["default_vault"] if cfg else None


def allow_personal_vaults():
    cfg = load()
    return True if cfg is None else cfg["allow_personal_vaults"]


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


def is_admin(user=None):
    """Admins manage vault-wide things (tags, compression, cleanup) and can
    delete anything. With no admins configured, everyone is an admin."""
    cfg = load()
    if not cfg or not cfg["admins"]:
        return True
    return (user or current_user()).lower() in cfg["admins"]


def can_delete(*records):
    """Admins can delete anything; anyone else only what they created."""
    if is_admin():
        return True
    me = current_user().lower()
    return any(((r or {}).get("created_by") or "").lower() == me for r in records)


def user_config_path():
    """Per-artist settings (which vault is active, personal vaults). In studio
    mode the extension folder is usually a shared install, so it can't hold
    one person's choices."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "comfyui-workflow-vault", "vault_config.json")


def trash_label():
    return "shared vault trash"


def public_info():
    """What the UI needs to adapt to studio mode."""
    cfg = load()
    if not cfg:
        return {"enabled": False}
    return {
        "enabled": True,
        "user": current_user(),
        "is_admin": is_admin(),
        "admins": cfg["admins"],
        "allow_personal_vaults": cfg["allow_personal_vaults"],
        "trash": cfg["trash"],
        "group": cfg["group"],
        "config_path": cfg["path"],
        "error": cfg["error"],
    }
