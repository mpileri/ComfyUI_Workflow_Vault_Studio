"""Low-level helpers shared across the vault backend: ids, slugs, atomic
file writes, and path-safety checks."""

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone

from . import studio


def _read_umask():
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


# Read once at import (single-threaded startup): os.umask can only be read by
# setting it, which would race with other threads later on.
_UMASK = _read_umask()


# ---------------------------------------------------------------------------
# Shared-folder permissions
# ---------------------------------------------------------------------------
#
# tempfile.mkstemp (behind every atomic write) creates files 0600 whatever the
# umask, and that alone made a vault on a shared folder unusable: whoever saved
# an entry was the only person who could read it. New files now get normal
# permissions (0666 minus the umask), and in studio mode the studio's group and
# group-writable modes, so every artist can read, version, and delete them.

def new_file_mode(shared=True):
    cfg = studio.load() if shared else None
    return cfg["file_mode"] if cfg else 0o666 & ~_UMASK


def _studio_gid(shared):
    cfg = studio.load() if shared else None
    return cfg["gid"] if cfg else None


def share_path(path):
    """Give a file or folder the studio's group and permissions. A no-op
    outside studio mode, on Windows, and for paths owned by someone else (only
    the owner, or root, may change them). Never raises."""
    cfg = studio.load()
    if not cfg or os.name == "nt":
        return
    try:
        st = os.lstat(path)
    except OSError:
        return
    if stat.S_ISLNK(st.st_mode):
        return
    euid = os.geteuid()
    if euid != 0 and st.st_uid != euid:
        return
    if cfg["gid"] is not None and st.st_gid != cfg["gid"]:
        try:
            os.chown(path, -1, cfg["gid"])
        except OSError:
            pass
    want = cfg["dir_mode"] if stat.S_ISDIR(st.st_mode) else cfg["file_mode"]
    try:
        # chown may have cleared setgid, so re-read rather than trust st_mode.
        if stat.S_IMODE(os.lstat(path).st_mode) != want:
            os.chmod(path, want)
    except OSError:
        pass


def share_tree(root):
    """share_path() for a folder and everything under it."""
    if not studio.enabled():
        return
    share_path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            share_path(os.path.join(dirpath, name))


def makedirs(path, shared=True):
    """os.makedirs(exist_ok=True), then give every folder it created the
    studio's group and setgid permissions (os.makedirs' mode is filtered by the
    umask, and can't set setgid at all)."""
    path = os.path.abspath(path)
    created = []
    probe = path
    while not os.path.isdir(probe):
        created.append(probe)
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    os.makedirs(path, exist_ok=True)
    if shared:
        for d in reversed(created):
            share_path(d)


def _prepare_temp_file(fd, shared):
    """Fix the 0600 that mkstemp forces on every new file."""
    if os.name == "nt":
        return
    gid = _studio_gid(shared)
    if gid is not None:
        try:
            os.fchown(fd, -1, gid)
        except OSError:
            pass
    try:
        os.fchmod(fd, new_file_mode(shared))
    except OSError:
        # Some network mounts (e.g. CIFS without Unix extensions) refuse
        # chmod; their permissions come from the server, so carry on.
        pass


def open_in_file_manager(path):
    """Reveal a folder in the OS file manager (server-side = the user's
    machine for a local ComfyUI). Returns (ok, error)."""
    if not path or not os.path.isdir(path):
        return False, "Folder not found."
    try:
        if sys.platform.startswith("win"):
            os.startfile(os.path.normpath(path))  # noqa: B606 - intentional, local single-user tool
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True, None
    except Exception as e:
        return False, f"Could not open folder: {e}"


def _select_in_explorer(path):
    """Open an Explorer window with `path` selected, via the shell API
    (SHOpenFolderAndSelectItems). This is the supported way to do it and, unlike
    running `explorer /select,<path>`, it never builds a command line, so quoting,
    spaces, accented letters and symbols in the path can't change what runs or
    break the call. Returns True on success.

    Explorer's command-line form was tried as a list argument too, but it only
    works for paths without spaces or non-ASCII characters (it falls back to
    opening Documents), so the shell API is used instead."""
    import ctypes

    try:
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        shell32.ILCreateFromPathW.restype = ctypes.c_void_p
        shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
        shell32.ILFree.restype = None
        shell32.ILFree.argtypes = [ctypes.c_void_p]
        shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long  # HRESULT
        shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong]
        ole32.CoInitialize.restype = ctypes.c_long
        ole32.CoInitialize.argtypes = [ctypes.c_void_p]
        ole32.CoUninitialize.restype = None
        ole32.CoUninitialize.argtypes = []
    except Exception:
        return False

    # S_OK / S_FALSE both need a matching CoUninitialize; a negative result (the
    # thread already has a different COM mode) means we must not call it.
    hr_init = ole32.CoInitialize(None)
    try:
        pidl = shell32.ILCreateFromPathW(path)
        if not pidl:
            return False
        try:
            # cidl=0 with a full item path selects that item in its parent folder.
            return shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) >= 0
        finally:
            shell32.ILFree(pidl)
    finally:
        if hr_init >= 0:
            ole32.CoUninitialize()


def reveal_in_file_manager(path):
    """Open the OS file manager with a specific file selected/highlighted.
    Returns (ok, error)."""
    if not path or not os.path.exists(path):
        return False, "File not found."
    try:
        if sys.platform.startswith("win"):
            path = os.path.normpath(path)
            if not _select_in_explorer(path):
                # The shell call failed (or ctypes is unavailable): open the folder
                # holding the file instead. os.startfile takes the path directly, so
                # nothing here is ever parsed as a command line.
                os.startfile(os.path.dirname(path))  # noqa: B606 - local single-user tool
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            # Most Linux file managers can't select a file from the CLI; open
            # the containing folder instead.
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
        return True, None
    except Exception as e:
        return False, f"Could not reveal file: {e}"


def pick_folder_dialog(title="Choose a folder"):
    """Open a native OS folder-picker dialog. Returns (path, error) — path is
    None with no error if the user cancelled.

    Tries tkinter first (present on most desktop Python installs, all
    platforms). ComfyUI's official Windows *portable* build ships an
    embeddable Python with no tkinter, so on Windows we fall back to the
    shell's folder-browse dialog directly via ctypes — stdlib-only, no
    pywin32 or other dependency, so it works on that same embeddable build."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        if sys.platform.startswith("win"):
            try:
                return _pick_folder_win32(title), None
            except Exception as e:
                return None, f"Native folder browser failed: {e}"
        return None, "Native folder browser is unavailable (tkinter not installed)."

    root = tk.Tk()
    root.withdraw()
    root.wm_attributes("-topmost", True)
    path = filedialog.askdirectory(title=title)
    root.destroy()
    return (path or None), None


def _pick_folder_win32(title):
    """The classic SHBrowseForFolder shell dialog via ctypes. restype/argtypes
    are set explicitly on every call — without them ctypes assumes a 32-bit
    `int` return, which silently truncates the 64-bit PIDL pointer on modern
    Windows and corrupts the result."""
    import ctypes

    BIF_RETURNONLYFSDIRS = 0x00000001
    BIF_NEWDIALOGSTYLE = 0x00000040

    class BROWSEINFOW(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", ctypes.c_void_p),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", ctypes.c_wchar_p),
            ("lpszTitle", ctypes.c_wchar_p),
            ("ulFlags", ctypes.c_uint),
            ("lpfn", ctypes.c_void_p),
            ("lParam", ctypes.c_void_p),
            ("iImage", ctypes.c_int),
        ]

    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
    shell32.SHGetPathFromIDListW.restype = ctypes.c_int
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    ole32.CoTaskMemFree.restype = None
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    display_name = ctypes.create_unicode_buffer(260)
    bi = BROWSEINFOW()
    bi.pszDisplayName = ctypes.cast(display_name, ctypes.c_wchar_p)
    bi.lpszTitle = title
    bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE

    pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
    if not pidl:
        return None  # user cancelled

    path_buf = ctypes.create_unicode_buffer(260)
    shell32.SHGetPathFromIDListW(pidl, path_buf)
    ole32.CoTaskMemFree(pidl)
    return path_buf.value or None


def set_file_times(path, mtime, ctime=None):
    """Stamp a file's modified/access time (and, on Windows, its creation time)
    so a converted file keeps its source file's date. Best-effort: never raises.

    `mtime`/`ctime` are POSIX timestamps (seconds). `ctime` defaults to `mtime`
    (uploads only know one date); the batch path passes the real creation time.
    """
    if mtime is None:
        return
    try:
        os.utime(path, (mtime, mtime))
    except OSError:
        pass
    if os.name == "nt":
        _set_windows_creation_time(path, mtime if ctime is None else ctime)


def _set_windows_creation_time(path, ctime):
    try:
        import ctypes
        from ctypes import wintypes

        # POSIX seconds -> Windows FILETIME (100ns ticks since 1601-01-01).
        ticks = int((ctime + 11644473600) * 10_000_000)
        if ticks < 0:
            return
        ft = wintypes.FILETIME(ticks & 0xFFFFFFFF, (ticks >> 32) & 0xFFFFFFFF)

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.restype = wintypes.HANDLE
        GENERIC_WRITE = 0x40000000
        OPEN_EXISTING = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        handle = kernel32.CreateFileW(
            ctypes.c_wchar_p(str(path)), GENERIC_WRITE, 0, None,
            OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None,
        )
        if not handle or handle == INVALID_HANDLE_VALUE:
            return
        try:
            kernel32.SetFileTime(handle, ctypes.byref(ft), None, None)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        pass


def _uses_vault_trash():
    cfg = studio.load()
    return bool(cfg and cfg["trash"] == "vault")


def trash_label():
    """The user-facing name of where deletes go, for UI copy."""
    if _uses_vault_trash():
        return studio.trash_label()
    if sys.platform.startswith("win"):
        return "Recycle Bin"
    return "Trash"


def _move_to_vault_trash(vault_root, path):
    """Move a path into <vault>/.trash/<when>_<who>_<id>/, keeping its path
    relative to the vault so restoring is a move back. A trash_info.json beside
    it records what it was and who deleted it.

    On a shared vault the deleting artist's personal OS trash is the wrong
    place: colleagues can't see it, and network mounts often have none (which
    used to turn every delete permanent)."""
    rel = os.path.relpath(path, vault_root)
    user = studio.current_user()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    item_id = f"{stamp}_{re.sub(r'[^A-Za-z0-9._-]', '_', user)}_{uuid.uuid4().hex[:6]}"
    item_root = os.path.join(vault_root, studio.TRASH_DIRNAME, item_id)
    dest = os.path.join(item_root, rel)
    makedirs(os.path.dirname(dest))
    try:
        atomic_write_json(os.path.join(item_root, "trash_info.json"), {
            "original_path": rel.replace("\\", "/"),
            "deleted_by": user,
            "deleted_at": now_iso(),
        })
        os.rename(path, dest)
    except OSError:
        import shutil

        shutil.rmtree(item_root, ignore_errors=True)
        raise
    return item_id


def send_to_trash(path, vault_root=None):
    """Delete a file/folder, preferring a trash so the delete is recoverable.
    Returns "vault_trash" (studio mode: moved into the vault's shared .trash),
    "trash" if it reached the OS trash, or "permanent" if no trash mechanism
    was available on this platform and it was removed for good instead. Raises
    OSError if the path can't be removed at all.

    In studio mode, a path inside `vault_root` always goes to the vault's own
    trash and is never deleted permanently: if the move fails, so does the
    delete.

    OS trash support is best-effort and dependency-free, per platform:
      - Windows: SHFileOperationW          -> Recycle Bin
      - macOS:   Finder via osascript      -> Trash
      - Linux:   `gio trash` / `trash-put` -> XDG Trash
    The cross-platform `send2trash` package, if it happens to be installed, is
    tried first since it handles every platform correctly."""
    path = os.path.normpath(path)
    if not os.path.exists(path):
        raise OSError("Path not found.")

    if vault_root and _uses_vault_trash() and is_path_inside(vault_root, path):
        _move_to_vault_trash(vault_root, path)
        return "vault_trash"

    if _move_to_trash(path):
        return "trash"

    # Fallback: permanent removal (no trash available on this platform/setup).
    import shutil

    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)
    return "permanent"


def _move_to_trash(path):
    """Best-effort move to the OS trash. Returns True on success, False if no
    trash mechanism worked (caller then falls back to a permanent delete)."""
    # 1. send2trash when available — correct on every platform it supports.
    try:
        from send2trash import send2trash as _s2t

        _s2t(path)
        return not os.path.exists(path)
    except ImportError:
        pass
    except Exception:
        # Installed but failed (e.g. unsupported filesystem); try OS natives.
        pass

    # 2. Platform-native, no extra dependencies.
    if sys.platform.startswith("win"):
        return _windows_recycle(path)
    if sys.platform == "darwin":
        return _macos_trash(path)
    return _linux_trash(path)


def _macos_trash(path):
    """Move a path to the macOS Trash by asking Finder via osascript. The path
    is passed as an argument (not interpolated) to avoid quoting/injection."""
    script = (
        "on run argv\n"
        "tell application \"Finder\" to delete (POSIX file (item 1 of argv) as alias)\n"
        "end run"
    )
    try:
        res = subprocess.run(
            ["osascript", "-e", script, os.path.abspath(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        )
        return res.returncode == 0 and not os.path.exists(path)
    except Exception:
        return False


def _linux_trash(path):
    """Move a path to the XDG Trash using whichever helper is installed. `gio`
    (glib) is near-universal on desktop Linux and implements the freedesktop
    Trash spec correctly, including cross-filesystem handling."""
    import shutil as _shutil

    candidates = (
        ["gio", "trash", "--", path],
        ["trash-put", "--", path],
        ["trash", "--", path],
    )
    for argv in candidates:
        if not _shutil.which(argv[0]):
            continue
        try:
            res = subprocess.run(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30
            )
            if res.returncode == 0 and not os.path.exists(path):
                return True
        except Exception:
            continue
    return False


def _windows_recycle(path):
    """Send a path to the Windows Recycle Bin via SHFileOperationW. Returns
    True on success, False if the shell call failed (caller then falls back to
    a permanent delete)."""
    try:
        import ctypes
        from ctypes import wintypes

        FO_DELETE = 3
        FOF_SILENT = 0x0004
        FOF_NOCONFIRMATION = 0x0010
        FOF_ALLOWUNDO = 0x0040  # this is what routes it to the Recycle Bin
        FOF_NOERRORUI = 0x0400

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_uint16),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        op = SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        # pFrom must be a double-null-terminated list of paths.
        op.pFrom = os.path.abspath(path) + "\0\0"
        op.pTo = None
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI

        shell32 = ctypes.windll.shell32
        shell32.SHFileOperationW.argtypes = [ctypes.c_void_p]
        shell32.SHFileOperationW.restype = ctypes.c_int
        res = shell32.SHFileOperationW(ctypes.byref(op))
        return res == 0 and not op.fAnyOperationsAborted
    except Exception:
        return False


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stamp_created(record, now=None):
    """Set created/updated time and author on a new manifest, version, or
    example. Returns the timestamp used."""
    now = now or now_iso()
    user = studio.current_user()
    record["created_at"] = now
    record["created_by"] = user
    record["updated_at"] = now
    record["updated_by"] = user
    return now


def touch(record, now=None):
    """Mark a record as just changed, and by whom. Returns the timestamp."""
    now = now or now_iso()
    record["updated_at"] = now
    record["updated_by"] = studio.current_user()
    return now


def generate_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:20].upper()}"


_SLUG_INVALID_RE = re.compile(r"[^a-z0-9]+")


def slugify(name):
    slug = (name or "").strip().lower()
    slug = _SLUG_INVALID_RE.sub("_", slug)
    slug = slug.strip("_")
    return slug or "untitled"


def unique_slug(base_slug, existing_slugs):
    if base_slug not in existing_slugs:
        return base_slug
    i = 2
    while f"{base_slug}_{i}" in existing_slugs:
        i += 1
    return f"{base_slug}_{i}"


def atomic_write_text(path, text, encoding="utf-8", shared=True):
    """Write via a temp file + rename so readers never see a partial file.
    `shared=False` is for per-user files outside the vault (no studio group)."""
    directory = os.path.dirname(path)
    makedirs(directory, shared=shared)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=directory)
    try:
        _prepare_temp_file(fd, shared)
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def atomic_write_json(path, data, shared=True):
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False), shared=shared)


def atomic_write_bytes(path, data):
    directory = os.path.dirname(path)
    makedirs(directory)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=directory)
    try:
        _prepare_temp_file(fd, True)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def read_json(path, default=None):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError) as e:
        # A single corrupt/unreadable file shouldn't take down a whole vault
        # scan; skip it and let callers fall back to their default.
        print(f"[Workflow Vault] Skipping unreadable JSON file: {path} ({e})")
        return default


def read_text(path, default=""):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        print(f"[Workflow Vault] Skipping unreadable file: {path} ({e})")
        return default


def is_path_inside(base, target):
    base = os.path.realpath(base)
    target = os.path.realpath(target)
    if base == target:
        return True
    try:
        common = os.path.commonpath([base, target])
    except ValueError:
        return False
    return common == base
