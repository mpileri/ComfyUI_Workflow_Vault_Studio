"""REST-style backend endpoints for the Workflow Vault extension."""

import asyncio
import functools
import json
import os
import re
from urllib.parse import urlsplit

from aiohttp import web
from server import PromptServer

from . import (config, entries, examples, exporting, folders, locking, media, prefs, storage, studio,
               utils, versions, video)

routes = PromptServer.instance.routes

# Per-file upload ceiling for thumbnails and example media (defense against
# accidental/oversized uploads filling memory or disk).
MAX_UPLOAD_BYTES = 256 * 1024 * 1024  # 256 MB
MAX_MULTIPART_BYTES = 768 * 1024 * 1024  # 768 MB total per request

# Mutating endpoints perform read/modify/write cycles across several plain
# files. Serialize those cycles per vault root so concurrent browser tabs do
# not clobber each other's manifest/folder/example updates inside one ComfyUI
# process, and (via a lock file in the vault) across every other artist's
# ComfyUI using the same shared vault.
_WRITE_LOCKS = {}

# How long a save waits for another process's write to finish before giving
# up with "busy". Batch compression can hold the lock for a while.
VAULT_LOCK_TIMEOUT = 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _error(message, status=400):
    return web.json_response({"error": message}, status=status)


# ---------------------------------------------------------------------------
# Cross-site request protection
# ---------------------------------------------------------------------------
#
# ComfyUI ships no CSRF protection, and several vault endpoints accept
# multipart/form-data — a request type any web page can submit cross-origin
# with no preflight to stop it. The JSON endpoints are no safer: aiohttp's
# request.json() parses the body regardless of Content-Type, so a plain
# text/plain form post reaches them too.
#
# Unguarded, a page the user happens to be browsing could repoint the vault
# root at any writable directory, delete entries, or pop a native folder
# dialog on their desktop. Every mutating route therefore goes through _post()
# below rather than routes.post() directly.

def _is_same_origin(request):
    """True when a request demonstrably did not originate from another site.

    Two independent browser-set signals, neither forgeable by page script:

    - ``Sec-Fetch-Site`` is sent by every current browser. Only "same-origin"
      and "none" (direct navigation) are accepted; "same-site" is refused too,
      since ports don't distinguish sites and another local server would
      otherwise qualify.
    - ``Origin`` is sent on every browser POST, cross-origin form submissions
      included, and is compared against the host the request was addressed to.

    A request carrying neither header is allowed: that's a non-browser client
    (curl, a script), and a browser-driven CSRF attack cannot produce one.
    """
    fetch_site = request.headers.get("Sec-Fetch-Site")
    if fetch_site and fetch_site not in ("same-origin", "none"):
        return False

    origin = request.headers.get("Origin")
    if not origin:
        return True
    # Rejects "null" (sandboxed iframes, file://) along with any real mismatch,
    # since urlsplit gives it an empty netloc.
    return urlsplit(origin).netloc == request.host


def _guard_csrf(handler):
    @functools.wraps(handler)
    async def wrapped(request):
        if not _is_same_origin(request):
            return _error("Blocked: this request appears to come from another site.", 403)
        try:
            return await handler(request)
        except locking.VaultBusy:
            return _error(
                "The vault is busy: someone else is saving a large change right now. "
                "Try again in a moment.", 503)

    return wrapped


def _post(path):
    """Register a CSRF-guarded POST route. Mutating endpoints use this instead
    of routes.post() so the check can never be forgotten on a new route."""
    def decorator(handler):
        return routes.post(path)(_guard_csrf(handler))

    return decorator


async def _read_json(request):
    """Parse a JSON request body. Returns (body, error_response); body is None
    on error so callers can `if err: return err`."""
    try:
        body = await request.json()
    except Exception:
        return None, _error("Invalid JSON in request body.", 400)
    if not isinstance(body, dict):
        return None, _error("Request body must be a JSON object.", 400)
    return body, None


def _require_vault():
    """Returns (vault_root, error_response). vault_root is None on error."""
    vault_root = config.get_vault_root()
    if not vault_root:
        return None, _error("Vault root is not configured. Set one in Settings.", 400)
    if not config.is_initialized(vault_root):
        return None, _error("Vault root is not initialized.", 400)
    return vault_root, None


def _require_entry(vault_root, entry_id):
    """Returns (slug, manifest, error_response)."""
    slug, manifest = storage.find_slug_by_id(vault_root, entry_id)
    if not slug:
        return None, None, _error("Entry not found.", 404)
    return slug, manifest, None


class _VaultWriteLock:
    """This process's asyncio lock for a vault, then the vault's lock file
    (shared with every other ComfyUI using it). Raises locking.VaultBusy if
    the file lock isn't free within VAULT_LOCK_TIMEOUT seconds."""

    def __init__(self, local_lock, vault_root):
        self._local = local_lock
        self._root = vault_root
        self._file = None

    async def __aenter__(self):
        await self._local.acquire()
        try:
            # Only real vaults get a lock file: a folder the user is merely
            # proposing as a vault must stay untouched until they confirm.
            if self._root and config.is_initialized(self._root):
                self._file = await _acquire_file_lock(self._root)
        except BaseException:
            self._local.release()
            raise
        return self

    async def __aexit__(self, *exc):
        try:
            if self._file:
                self._file.release()
                self._file = None
        finally:
            self._local.release()


async def _acquire_file_lock(vault_root):
    lock = locking.VaultFileLock(vault_root)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + VAULT_LOCK_TIMEOUT
    while True:
        # Off the event loop: on a network mount the open/lock calls are RPCs.
        if await asyncio.to_thread(lock.try_acquire):
            return lock
        if loop.time() >= deadline:
            lock.release()
            raise locking.VaultBusy()
        await asyncio.sleep(0.2)


def _write_lock(vault_root):
    key = os.path.realpath(vault_root or "__workflow_vault_extension__")
    lock = _WRITE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _WRITE_LOCKS[key] = lock
    return _VaultWriteLock(lock, vault_root)


def _require_admin(action):
    """Studio mode with admins configured: vault-wide operations are theirs.
    Returns an error response, or None when allowed."""
    if studio.is_admin():
        return None
    admins = ", ".join(studio.load()["admins"])
    return _error(f"Only vault admins can {action} ({admins}).", 403)


def _require_can_delete(what, *records):
    if studio.can_delete(*records):
        return None
    admins = ", ".join(studio.load()["admins"])
    return _error(f"You can only delete {what} you created. Ask a vault admin ({admins}).", 403)


class _MultipartError(Exception):
    """Raised when a multipart request is malformed or a file is too large."""


async def _parse_multipart(request):
    """Returns (data: dict, files: dict[name -> {filename, bytes}]).

    Raises _MultipartError on malformed bodies or oversized uploads; callers
    wrap the call and translate it into a clean 400 response."""
    data = {}
    files = {}
    request_total = {"bytes": 0}
    try:
        reader = await request.multipart()
        async for part in reader:
            if part.name == "data":
                text = await part.text()
                try:
                    data = json.loads(text) if text else {}
                except ValueError:
                    raise _MultipartError("Invalid JSON in 'data' field.")
                if not isinstance(data, dict):
                    raise _MultipartError("'data' field must be a JSON object.")
            elif part.filename:
                content = await _read_part_limited(part, request_total)
                files[part.name] = {"filename": part.filename, "bytes": content}
            else:
                await part.read(decode=False)
    except _MultipartError:
        raise
    except Exception:
        raise _MultipartError("Could not parse upload.")
    # Carry each source file's original date (browser File.lastModified, in ms)
    # onto its file dict as POSIX seconds, so converted files keep their date.
    mtimes = data.get("file_mtimes") if isinstance(data.get("file_mtimes"), dict) else {}
    for name, f in files.items():
        ts = mtimes.get(name)
        if isinstance(ts, (int, float)) and ts > 0:
            f["mtime"] = ts / 1000.0
    return data, files


async def _read_part_limited(part, request_total=None):
    """Read a multipart file part in chunks, enforcing per-file and request caps."""
    chunks = []
    total = 0
    if request_total is None:
        request_total = {"bytes": 0}
    while True:
        chunk = await part.read_chunk()
        if not chunk:
            break
        total += len(chunk)
        request_total["bytes"] += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise _MultipartError(
                f"File '{part.filename}' exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit."
            )
        if request_total["bytes"] > MAX_MULTIPART_BYTES:
            raise _MultipartError(
                f"Upload exceeds the {MAX_MULTIPART_BYTES // (1024 * 1024)} MB request limit."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _collect_indexed_files(files, prefix, labels=None):
    if not isinstance(labels, list):
        labels = None
    result = []
    i = 0
    while f"{prefix}{i}" in files:
        item = dict(files[f"{prefix}{i}"])
        if labels and i < len(labels):
            item["label"] = labels[i]
        result.append(item)
        i += 1
    return result


def _profile_fields():
    """Profile list + active id (and studio mode info), merged into every
    state/settings response so the UI never needs a second request."""
    info = config.list_profiles()
    return {"profiles": info["profiles"], "active_profile": info["active"], "studio": studio.public_info()}


def _full_state(vault_root):
    state = storage.build_state(vault_root)
    state["vault_root"] = vault_root
    state["initialized"] = True
    state["settings"] = config.load_vault_settings(vault_root)
    state["pillow_available"] = media.pillow_available()
    state["trash_label"] = utils.trash_label()
    state.update(_profile_fields())
    return state


def _prepare_vault_root(new_root, confirm):
    """Validate a folder the user wants to use as a vault root and initialize it
    if needed. Returns an error response, or None once it is ready to use.

    A non-empty folder that isn't already a vault needs explicit confirmation
    (409 + needs_confirmation) before vault files are created inside it."""
    ok, err = config.validate_vault_root(new_root)
    if not ok:
        return _error(err)

    if config.is_empty(new_root) or not os.path.exists(new_root):
        config.initialize_vault(new_root)
    elif not config.is_initialized(new_root):
        if not confirm:
            return web.json_response({
                "needs_confirmation": True,
                "message": (
                    "This folder does not appear to be a Workflow Vault.\n\n"
                    "Vault files will be created inside it:\n"
                    "  vault_settings.json\n"
                    "  folders.json\n"
                    "  entries/\n\n"
                    "Existing files will not be modified.\n\n"
                    "Continue?"
                ),
            }, status=409)
        config.initialize_vault(new_root)
    return None


# ---------------------------------------------------------------------------
# Settings / vault initialization
# ---------------------------------------------------------------------------

async def _studio_vault_problem():
    """Studio mode, active vault set by the studio: initialize it the first
    time anyone opens it, or explain why it can't be used. Returns a message
    for the UI, or None when the vault is ready."""
    cfg = studio.load()
    if cfg and cfg["error"]:
        return cfg["error"]
    profile = config.active_profile()
    if not profile:
        if cfg and not cfg["allow_personal_vaults"]:
            return ("Studio mode is on but no studio vault is configured. "
                    f"Add one to {cfg['path'] or 'the studio config'} (or set WORKFLOW_VAULT_ROOT).")
        return None
    if not profile.get("locked"):
        return None
    root = profile["vault_root"]
    if not os.path.isdir(root):
        return (f"The studio vault folder “{root}” isn't reachable from this machine. "
                "Check that the network share is mounted, or ask your pipeline team.")
    if config.is_initialized(root):
        return None
    if not os.access(root, os.W_OK):
        return (f"The studio vault folder “{root}” isn't set up yet and you can't write to it. "
                "Ask your pipeline team to run: python vault_admin.py init")
    async with _write_lock(root):
        if not config.is_initialized(root):
            config.initialize_vault(root)
    return None


@routes.get("/workflow-vault/state")
async def get_state(request):
    studio_error = await _studio_vault_problem() if studio.enabled() else None
    vault_root = config.get_vault_root()
    if studio_error or not vault_root or not config.is_initialized(vault_root):
        return web.json_response({
            "vault_root": vault_root,
            "initialized": False,
            "settings": dict(config.DEFAULT_VAULT_SETTINGS),
            "folders": [],
            "entries": [],
            "tags": [],
            "extension_dir": config.EXTENSION_DIR,
            "studio_error": studio_error,
            **_profile_fields(),
        })
    return web.json_response(_full_state(vault_root))


@routes.get("/workflow-vault/settings")
async def get_settings(request):
    vault_root = config.get_vault_root()
    initialized = bool(vault_root and config.is_initialized(vault_root))
    settings = config.load_vault_settings(vault_root) if initialized else dict(config.DEFAULT_VAULT_SETTINGS)
    return web.json_response({
        "vault_root": vault_root,
        "initialized": initialized,
        "settings": settings,
        **_profile_fields(),
    })


@_post("/workflow-vault/settings")
async def post_settings(request):
    body, err = await _read_json(request)
    if err:
        return err
    # save_vault_settings is a read-modify-write of vault_settings.json;
    # serialize it like every other mutating route so concurrent saves (two
    # tabs, or rapid-fire UI toggles) don't drop updates.
    target_root = (body.get("vault_root") or "").strip() if "vault_root" in body else config.get_vault_root()
    async with _write_lock(target_root):
        return _apply_settings(body)


def _apply_settings(body):
    vault_root = None

    if "vault_root" in body:
        new_root = (body.get("vault_root") or "").strip()
        problem = config.check_set_vault_root(new_root)
        if problem:
            return _error(problem, 403)
        err_response = _prepare_vault_root(new_root, body.get("confirm"))
        if err_response:
            return err_response

        problem = config.set_vault_root(new_root)
        if problem:
            return _error(problem, 403)
        vault_root = new_root

    if vault_root is None:
        vault_root = config.get_vault_root()
        if not vault_root:
            return _error("Vault root is not configured.")

    updates = {}
    if "show_archived" in body:
        updates["show_archived"] = bool(body["show_archived"])
    if "default_thumbnail_behavior" in body:
        updates["default_thumbnail_behavior"] = body["default_thumbnail_behavior"]
    if "grid_columns" in body:
        try:
            grid_columns = int(body["grid_columns"])
        except (TypeError, ValueError):
            return _error("Invalid grid_columns.")
        if grid_columns not in (2, 3, 4):
            return _error("Invalid grid_columns.")
        updates["grid_columns"] = grid_columns
    if "card_size" in body:
        if body["card_size"] not in config.VALID_CARD_SIZES:
            return _error("Invalid card_size.")
        updates["card_size"] = body["card_size"]
    if "window_size" in body:
        if body["window_size"] not in config.VALID_WINDOW_SIZES:
            return _error("Invalid window_size.")
        updates["window_size"] = body["window_size"]
    if "sort" in body:
        if body["sort"] not in config.VALID_SORTS:
            return _error("Invalid sort.")
        updates["sort"] = body["sort"]
    if "accent_color" in body:
        color = str(body.get("accent_color") or "").strip()
        if not re.match(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$", color):
            return _error("Invalid accent color.")
        updates["accent_color"] = color
    if "card_fields" in body:
        cf = body.get("card_fields")
        if not isinstance(cf, dict):
            return _error("Invalid card_fields.")
        updates["card_fields"] = {k: bool(v) for k, v in cf.items() if k in config.CARD_FIELD_KEYS}
    if "compress_examples_on_upload" in body:
        updates["compress_examples_on_upload"] = bool(body["compress_examples_on_upload"])
    if "example_compress_format" in body:
        if body["example_compress_format"] not in config.VALID_COMPRESS_FORMATS:
            return _error("Invalid example_compress_format.")
        updates["example_compress_format"] = body["example_compress_format"]
    if "compress_thumbnail_source" in body:
        updates["compress_thumbnail_source"] = bool(body["compress_thumbnail_source"])

    if updates:
        # The settings form posts every field; only a real change to a
        # vault-wide one (not a personal view setting) needs an admin.
        if config.shared_setting_changes(vault_root, updates):
            err = _require_admin("change vault-wide settings such as the accent color or compression")
            if err:
                return err
        config.save_vault_settings(vault_root, updates)

    return web.json_response({
        "vault_root": vault_root,
        "initialized": True,
        "settings": config.load_vault_settings(vault_root),
        **_profile_fields(),
    })


@_post("/workflow-vault/initialize")
async def post_initialize(request):
    body, err = await _read_json(request)
    if err:
        return err
    vault_root = (body.get("vault_root") or "").strip()
    ok, err = config.validate_vault_root(vault_root)
    if not ok:
        return _error(err)
    problem = config.check_set_vault_root(vault_root)
    if problem:
        return _error(problem, 403)
    async with _write_lock(vault_root):
        config.initialize_vault(vault_root)
        config.set_vault_root(vault_root)
        return web.json_response({
            "vault_root": vault_root,
            "initialized": True,
            "settings": config.load_vault_settings(vault_root),
            **_profile_fields(),
        })


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
#
# Each profile is its own vault root. Switching only repoints vault_config.json
# at another root; no vault data is copied, moved, or deleted by any of these
# routes. All of them serialize on the extension-level lock because they
# read-modify-write vault_config.json.

def _profiles_response(**extra):
    return web.json_response({**_profile_fields(), **extra})


@routes.get("/workflow-vault/profiles")
async def get_profiles(request):
    return _profiles_response()


@_post("/workflow-vault/profiles")
async def post_create_profile(request):
    """Create a profile backed by a vault folder and (by default) switch to it."""
    body, err = await _read_json(request)
    if err:
        return err
    name = config.clean_profile_name(body.get("name"))
    vault_root = (body.get("vault_root") or "").strip()
    if not vault_root:
        return _error("Choose a folder for the vault.")
    async with _write_lock(None):
        problem = config.validate_new_profile(name, vault_root)
        if problem:
            return _error(problem)
        err_response = _prepare_vault_root(vault_root, body.get("confirm"))
        if err_response:
            return err_response
        profile = config.create_profile(name, vault_root, activate=body.get("activate", True) is not False)
        return _profiles_response(profile=profile)


@_post("/workflow-vault/profiles/{profile_id}/activate")
async def post_activate_profile(request):
    async with _write_lock(None):
        profile, err_msg = config.activate_profile(request.match_info["profile_id"])
        if err_msg:
            return _error(err_msg, 404 if err_msg == config.PROFILE_NOT_FOUND else 400)
        return _profiles_response(profile=profile)


@_post("/workflow-vault/profiles/{profile_id}/rename")
async def post_rename_profile(request):
    body, err = await _read_json(request)
    if err:
        return err
    async with _write_lock(None):
        profile, err_msg = config.rename_profile(request.match_info["profile_id"], body.get("name"))
        if err_msg:
            return _error(err_msg, 404 if err_msg == config.PROFILE_NOT_FOUND else 400)
        return _profiles_response(profile=profile)


@_post("/workflow-vault/profiles/{profile_id}/delete")
async def post_delete_profile(request):
    """Remove a profile from the list. The folder and its contents stay on disk."""
    async with _write_lock(None):
        err_msg = config.remove_profile(request.match_info["profile_id"])
        if err_msg:
            return _error(err_msg, 404 if err_msg == config.PROFILE_NOT_FOUND else 400)
        return _profiles_response()


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------

@_post("/workflow-vault/entries")
async def post_create_entry(request):
    vault_root, err = _require_vault()
    if err:
        return err

    try:
        data, files = await _parse_multipart(request)
    except _MultipartError as e:
        return _error(str(e))

    if "thumbnail" in files:
        data["thumbnail"] = files["thumbnail"]
    if "thumbnail_source" in files:
        data["thumbnail_source"] = files["thumbnail_source"]
    if "compare_image" in files:
        data["compare_image"] = files["compare_image"]
    if "compare_image_source" in files:
        data["compare_image_source"] = files["compare_image_source"]

    examples_in = data.get("examples") or []
    if not isinstance(examples_in, list) or not all(isinstance(e, dict) for e in examples_in):
        return _error("'examples' must be a list of JSON objects.")
    for i, example in enumerate(examples_in):
        example["input_files"] = _collect_indexed_files(files, f"example_{i}_input_", example.get("input_labels"))
        example["output_files"] = _collect_indexed_files(files, f"example_{i}_output_", example.get("output_labels"))

    # Studio mode: a favorite is the saving artist's own, not the entry's.
    favorite = bool(data.pop("favorite", False)) if studio.enabled() else None

    async with _write_lock(vault_root):
        entry, err_msg = entries.create_entry(vault_root, data)
        if err_msg:
            return _error(err_msg)
        if favorite:
            prefs.set_favorite(vault_root, entry["id"], True)
            entry["favorite"] = True
        return web.json_response(entry)


@_post("/workflow-vault/entries/{entry_id}/metadata")
async def post_entry_metadata(request):
    vault_root, err = _require_vault()
    if err:
        return err
    entry_id = request.match_info["entry_id"]

    try:
        data, files = await _parse_multipart(request)
    except _MultipartError as e:
        return _error(str(e))
    base_updated_at = data.pop("base_updated_at", None)
    async with _write_lock(vault_root):
        slug, manifest, err = _require_entry(vault_root, entry_id)
        if err:
            return err
        conflict = _edit_conflict(manifest, base_updated_at)
        if conflict:
            return conflict
        if studio.enabled() and "favorite" in data:
            prefs.set_favorite(vault_root, entry_id, bool(data.pop("favorite")))
            # Starring alone touches nothing shared (and must not bump
            # "last updated" for everyone else).
            data.pop("file_mtimes", None)
            if not data and not files:
                return web.json_response(storage.build_entry_state(vault_root, slug))
        new_slug, err_msg, warnings = entries.update_entry_metadata(
            vault_root, manifest, slug, data, files.get("thumbnail"), files.get("thumbnail_source"),
            compare_image_file=files.get("compare_image"),
            compare_image_source_file=files.get("compare_image_source"),
        )
        if err_msg:
            return _error(err_msg)
        state = storage.build_entry_state(vault_root, new_slug)
        if warnings:
            state["warnings"] = warnings
        return web.json_response(state)


def _edit_conflict(manifest, base_updated_at):
    """409 when someone else changed the entry after the editor opened it, so
    saving the form would silently undo their change. The UI sends the
    updated_at it rendered as base_updated_at; edits by the same artist
    never count as a conflict."""
    current = manifest.get("updated_at")
    if not base_updated_at or not current or base_updated_at == current:
        return None
    who = manifest.get("updated_by")
    if who and who == studio.current_user():
        return None
    by = f" by {who}" if who else ""
    return web.json_response({
        "error": (f"“{manifest.get('name')}” was changed{by} since you opened it. "
                  "Copy anything you want to keep, reload the entry to see the latest version, "
                  "then re-apply your edits."),
        "conflict": True,
    }, status=409)


@_post("/workflow-vault/entries/{entry_id}/archive")
async def post_archive_entry(request):
    vault_root, err = _require_vault()
    if err:
        return err
    entry_id = request.match_info["entry_id"]
    slug, manifest, err = _require_entry(vault_root, entry_id)
    if err:
        return err

    if request.can_read_body:
        body, err = await _read_json(request)
        if err:
            return err
    else:
        body = {}
    archived = bool(body.get("archived", True))
    async with _write_lock(vault_root):
        slug, manifest, err = _require_entry(vault_root, entry_id)
        if err:
            return err
        entries.set_archived(vault_root, manifest, slug, archived, body.get("restore_status"))
        return web.json_response(storage.build_entry_state(vault_root, slug))


@_post("/workflow-vault/entries/{entry_id}/duplicate")
async def post_duplicate_entry(request):
    """Clone an entry into a new one under a user-supplied unique name, keeping
    only the source's current version. File copying runs off the event loop."""
    vault_root, err = _require_vault()
    if err:
        return err
    entry_id = request.match_info["entry_id"]
    slug, _manifest, err = _require_entry(vault_root, entry_id)
    if err:
        return err
    body, err = await _read_json(request)
    if err:
        return err

    new_name = (body.get("name") or "").strip()
    async with _write_lock(vault_root):
        slug, _manifest, err = _require_entry(vault_root, entry_id)
        if err:
            return err
        state, err_msg = await asyncio.to_thread(entries.duplicate_entry, vault_root, slug, new_name)
        if err_msg:
            return _error(err_msg)
        return web.json_response(state)


@_post("/workflow-vault/entries/{entry_id}/delete")
async def post_delete_entry(request):
    """Delete a whole entry from the vault, sending its folder to the Recycle
    Bin where supported. CPU/IO work runs off the event loop."""
    vault_root, err = _require_vault()
    if err:
        return err
    entry_id = request.match_info["entry_id"]
    slug, _manifest, err = _require_entry(vault_root, entry_id)
    if err:
        return err

    async with _write_lock(vault_root):
        slug, manifest, err = _require_entry(vault_root, entry_id)
        if err:
            return err
        err = _require_can_delete("entries", manifest)
        if err:
            return err
        method, err_msg = await asyncio.to_thread(entries.delete_entry, vault_root, slug)
        if err_msg:
            return _error(err_msg)
        return web.json_response({"ok": True, "method": method})


async def _stream_zip_response(request, zip_path, filename):
    headers = {
        "Content-Type": "application/zip",
        "Content-Disposition": f'attachment; filename="{filename}"',
    }
    resp = web.StreamResponse(headers=headers)
    await resp.prepare(request)
    try:
        with open(zip_path, "rb") as fh:
            while True:
                chunk = await asyncio.to_thread(fh.read, 1024 * 1024)
                if not chunk:
                    break
                await resp.write(chunk)
        await resp.write_eof()
        return resp
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass


@routes.get("/workflow-vault/entries/{entry_id}/export")
async def get_export_entry(request):
    vault_root, err = _require_vault()
    if err:
        return err
    entry_id = request.match_info["entry_id"]
    slug, manifest, err = _require_entry(vault_root, entry_id)
    if err:
        return err

    edir = storage.entry_dir(vault_root, slug)
    zip_path = await asyncio.to_thread(exporting.build_zip_file, edir, slug)
    return await _stream_zip_response(request, zip_path, f"{exporting.download_name(slug, 'entry')}.zip")


@routes.get("/workflow-vault/footprint")
async def get_footprint(request):
    vault_root, err = _require_vault()
    if err:
        return err
    # Walk off the event loop so a large vault doesn't block other requests.
    data = await asyncio.get_event_loop().run_in_executor(
        None, storage.compute_footprint, vault_root
    )
    return web.json_response(data)


@routes.get("/workflow-vault/health")
async def get_health(request):
    vault_root, err = _require_vault()
    if err:
        return err
    data = await asyncio.to_thread(storage.health_report, vault_root)
    return web.json_response(data)


@_post("/workflow-vault/health/cleanup-staging")
async def post_cleanup_staging(request):
    vault_root, err = _require_vault()
    if err:
        return err
    err = _require_admin("clean up interrupted saves")
    if err:
        return err
    async with _write_lock(vault_root):
        data = await asyncio.to_thread(storage.cleanup_staging_entries, vault_root)
        report = storage.health_report(vault_root)
    return web.json_response({"ok": not data["failed"], **data, "health": report})


@routes.get("/workflow-vault/export")
async def get_export_vault(request):
    vault_root, err = _require_vault()
    if err:
        return err
    arc_root = os.path.basename(os.path.normpath(vault_root)) or "vault"
    zip_path = await asyncio.to_thread(exporting.build_zip_file, vault_root, arc_root, True)
    return await _stream_zip_response(request, zip_path, f"{exporting.download_name(arc_root, 'vault')}.zip")


@_post("/workflow-vault/entries/{entry_id}/reveal-media")
async def post_reveal_media(request):
    vault_root, err = _require_vault()
    if err:
        return err
    entry_id = request.match_info["entry_id"]
    slug, manifest, err = _require_entry(vault_root, entry_id)
    if err:
        return err
    body, err = await _read_json(request)
    if err:
        return err

    abs_path, err_msg = media.resolve_media_path(vault_root, entry_id, body.get("path"))
    if err_msg:
        return _error(err_msg, 404)
    ok, rerr = utils.reveal_in_file_manager(abs_path)
    if not ok:
        return _error(rerr)
    return web.json_response({"ok": True})


@_post("/workflow-vault/entries/{entry_id}/convert-media")
async def post_convert_media(request):
    """Re-encode an undecodable video to browser-safe H.264, alongside the original.

    The encode runs off the event loop and OUTSIDE the vault write lock — it
    only writes a brand-new file nothing references yet, and a long 4K clip
    would otherwise stall every other tab's writes for minutes. Only the
    reference update needs the lock.
    """
    vault_root, err = _require_vault()
    if err:
        return err
    entry_id = request.match_info["entry_id"]
    slug, manifest, err = _require_entry(vault_root, entry_id)
    if err:
        return err
    body, err = await _read_json(request)
    if err:
        return err

    plan, err_msg = media.plan_h264_conversion(vault_root, entry_id, body.get("path"))
    if err_msg:
        return _error(err_msg)

    ok, cerr = await asyncio.to_thread(video.convert_to_h264, plan["src_abs"], plan["dest_abs"])
    if not ok:
        return _error(cerr)

    async with _write_lock(vault_root):
        result, err_msg = await asyncio.to_thread(
            media.finish_h264_conversion, vault_root, entry_id, plan
        )
        if err_msg:
            return _error(err_msg)
        return web.json_response({"ok": True, "codec": plan["codec"], **result,
                                  **_full_state(vault_root)})


@_post("/workflow-vault/entries/{entry_id}/open-folder")
async def post_open_entry_folder(request):
    vault_root, err = _require_vault()
    if err:
        return err
    entry_id = request.match_info["entry_id"]
    slug, manifest, err = _require_entry(vault_root, entry_id)
    if err:
        return err
    ok, err_msg = utils.open_in_file_manager(storage.entry_dir(vault_root, slug))
    if not ok:
        return _error(err_msg)
    return web.json_response({"ok": True})


@_post("/workflow-vault/browse-folder")
async def post_browse_folder(request):
    """Open a native OS folder-picker dialog and return the chosen path.

    Runs off the event loop so the server stays responsive while the dialog
    is open. Returns {"path": "..."} (None if the user cancelled) or an error
    if no native picker is available on this platform/install."""
    path, err = await asyncio.to_thread(utils.pick_folder_dialog, "Choose vault folder")
    if err:
        return _error(err)
    return web.json_response({"ok": True, "path": path})


@_post("/workflow-vault/compress-examples")
async def post_compress_examples(request):
    """Batch-compress every existing example image across the vault."""
    vault_root, err = _require_vault()
    if err:
        return err
    if not media.pillow_available():
        return _error("Image compression is unavailable (Pillow not installed).")
    err = _require_admin("re-compress every image in the vault")
    if err:
        return err
    settings = config.load_vault_settings(vault_root)
    fmt = settings.get("example_compress_format", "webp")
    if fmt not in config.VALID_COMPRESS_FORMATS:
        fmt = "webp"
    # CPU-bound — run off the event loop so the server stays responsive. The
    # same sweep also re-encodes archival thumbnail sources (always WebP); the
    # two stat sets are summed so the completion summary covers everything.
    def _compress_all():
        a = examples.compress_all_examples(vault_root, fmt)
        b = entries.compress_all_thumbnail_sources(vault_root)
        return {k: a[k] + b[k] for k in a}

    async with _write_lock(vault_root):
        stats = await asyncio.to_thread(_compress_all)
        return web.json_response({"ok": True, **stats})


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

@_post("/workflow-vault/entries/{entry_id}/versions")
async def post_create_version(request):
    vault_root, err = _require_vault()
    if err:
        return err
    entry_id = request.match_info["entry_id"]
    slug, manifest, err = _require_entry(vault_root, entry_id)
    if err:
        return err

    body, err = await _read_json(request)
    if err:
        return err
    async with _write_lock(vault_root):
        slug, manifest, err = _require_entry(vault_root, entry_id)
        if err:
            return err
        version, err_msg = versions.create_version(
            vault_root, manifest, slug, body.get("workflow"),
            label=body.get("label"),
            custom_label=body.get("custom_label"),
            notes=body.get("notes", ""),
            make_current=body.get("make_current", True),
        )
        if err_msg:
            return _error(err_msg)
        storage.write_manifest(vault_root, slug, manifest)
        return web.json_response(storage.build_entry_state(vault_root, slug))


@_post("/workflow-vault/entries/{entry_id}/versions/{version_id}/overwrite")
async def post_overwrite_version(request):
    vault_root, err = _require_vault()
    if err:
        return err
    entry_id = request.match_info["entry_id"]
    version_id = request.match_info["version_id"]
    slug, manifest, err = _require_entry(vault_root, entry_id)
    if err:
        return err

    body, err = await _read_json(request)
    if err:
        return err
    async with _write_lock(vault_root):
        slug, manifest, err = _require_entry(vault_root, entry_id)
        if err:
            return err
        version, err_msg = versions.overwrite_version(
            vault_root, manifest, slug, version_id, body.get("workflow"), body.get("notes"))
        if err_msg:
            return _error(err_msg)
        storage.write_manifest(vault_root, slug, manifest)
        return web.json_response(storage.build_entry_state(vault_root, slug))


@_post("/workflow-vault/entries/{entry_id}/versions/{version_id}/promote")
async def post_promote_version(request):
    vault_root, err = _require_vault()
    if err:
        return err
    entry_id = request.match_info["entry_id"]
    version_id = request.match_info["version_id"]
    slug, manifest, err = _require_entry(vault_root, entry_id)
    if err:
        return err

    async with _write_lock(vault_root):
        slug, manifest, err = _require_entry(vault_root, entry_id)
        if err:
            return err
        version, err_msg = versions.promote_version(vault_root, manifest, slug, version_id)
        if err_msg:
            return _error(err_msg)
        storage.write_manifest(vault_root, slug, manifest)
        return web.json_response(storage.build_entry_state(vault_root, slug))


@_post("/workflow-vault/entries/{entry_id}/versions/{version_id}")
async def post_update_version(request):
    vault_root, err = _require_vault()
    if err:
        return err
    entry_id = request.match_info["entry_id"]
    version_id = request.match_info["version_id"]
    slug, manifest, err = _require_entry(vault_root, entry_id)
    if err:
        return err

    body, err = await _read_json(request)
    if err:
        return err
    async with _write_lock(vault_root):
        slug, manifest, err = _require_entry(vault_root, entry_id)
        if err:
            return err
        version, err_msg = versions.update_version_notes(vault_root, manifest, slug, version_id, body.get("notes", ""))
        if err_msg:
            return _error(err_msg)
        storage.write_manifest(vault_root, slug, manifest)
        return web.json_response(storage.build_entry_state(vault_root, slug))


@_post("/workflow-vault/entries/{entry_id}/versions/{version_id}/delete")
async def post_delete_version(request):
    vault_root, err = _require_vault()
    if err:
        return err
    entry_id = request.match_info["entry_id"]
    version_id = request.match_info["version_id"]
    slug, manifest, err = _require_entry(vault_root, entry_id)
    if err:
        return err

    async with _write_lock(vault_root):
        slug, manifest, err = _require_entry(vault_root, entry_id)
        if err:
            return err
        version = next((v for v in storage.list_versions(vault_root, slug) if v.get("id") == version_id), None)
        err = _require_can_delete("versions", manifest, version)
        if err:
            return err
        _new_current, err_msg = versions.delete_version(vault_root, manifest, slug, version_id)
        if err_msg:
            return _error(err_msg)
        storage.write_manifest(vault_root, slug, manifest)
        return web.json_response(storage.build_entry_state(vault_root, slug))


@routes.get("/workflow-vault/entries/{entry_id}/versions/{version_id}/workflow")
async def get_version_workflow(request):
    vault_root, err = _require_vault()
    if err:
        return err
    entry_id = request.match_info["entry_id"]
    version_id = request.match_info["version_id"]
    slug, manifest, err = _require_entry(vault_root, entry_id)
    if err:
        return err

    workflow, err_msg = versions.get_version_workflow(vault_root, slug, version_id)
    if err_msg:
        return _error(err_msg, 404)
    return web.json_response(workflow)


# ---------------------------------------------------------------------------
# Examples
# ---------------------------------------------------------------------------

@_post("/workflow-vault/entries/{entry_id}/examples")
async def post_create_example(request):
    vault_root, err = _require_vault()
    if err:
        return err
    entry_id = request.match_info["entry_id"]
    slug, manifest, err = _require_entry(vault_root, entry_id)
    if err:
        return err

    try:
        data, files = await _parse_multipart(request)
    except _MultipartError as e:
        return _error(str(e))
    input_files = _collect_indexed_files(files, "input_", data.get("input_labels"))
    output_files = _collect_indexed_files(files, "output_", data.get("output_labels"))

    async with _write_lock(vault_root):
        slug, manifest, err = _require_entry(vault_root, entry_id)
        if err:
            return err
        example, err_msg, skipped = examples.create_example(vault_root, manifest, slug, data, input_files, output_files)
        if err_msg:
            return _error(err_msg)
        storage.write_manifest(vault_root, slug, manifest)
        state = storage.build_entry_state(vault_root, slug)
        if skipped:
            state["skipped_files"] = skipped
        return web.json_response(state)


@_post("/workflow-vault/entries/{entry_id}/examples/reorder")
async def post_reorder_examples(request):
    vault_root, err = _require_vault()
    if err:
        return err
    entry_id = request.match_info["entry_id"]
    slug, manifest, err = _require_entry(vault_root, entry_id)
    if err:
        return err

    body, err = await _read_json(request)
    if err:
        return err
    order = body.get("order")
    if not isinstance(order, list):
        return _error("'order' must be a list of example ids.")

    async with _write_lock(vault_root):
        slug, manifest, err = _require_entry(vault_root, entry_id)
        if err:
            return err
        ok, err_msg = examples.reorder_examples(vault_root, manifest, slug, order)
        if not ok:
            return _error(err_msg)
        storage.write_manifest(vault_root, slug, manifest)
        return web.json_response(storage.build_entry_state(vault_root, slug))


@_post("/workflow-vault/entries/{entry_id}/examples/{example_id}")
async def post_update_example(request):
    vault_root, err = _require_vault()
    if err:
        return err
    entry_id = request.match_info["entry_id"]
    example_id = request.match_info["example_id"]
    slug, manifest, err = _require_entry(vault_root, entry_id)
    if err:
        return err

    try:
        data, files = await _parse_multipart(request)
    except _MultipartError as e:
        return _error(str(e))
    # Malformed specs would read as "keep nothing" and delete the example's
    # media, so reject them outright.
    for key in ("inputs", "outputs"):
        if key in data:
            specs = data[key]
            if not isinstance(specs, list) or not all(isinstance(s, dict) for s in specs):
                return _error(f"'{key}' must be a list of JSON objects.")
    new_input_files = _collect_indexed_files(files, "new_input_", data.get("new_input_labels"))
    new_output_files = _collect_indexed_files(files, "new_output_", data.get("new_output_labels"))

    async with _write_lock(vault_root):
        slug, manifest, err = _require_entry(vault_root, entry_id)
        if err:
            return err
        example, err_msg, skipped = examples.update_example(
            vault_root, manifest, slug, example_id, data, new_input_files, new_output_files)
        if err_msg:
            return _error(err_msg)
        storage.write_manifest(vault_root, slug, manifest)
        state = storage.build_entry_state(vault_root, slug)
        if skipped:
            state["skipped_files"] = skipped
        return web.json_response(state)


@_post("/workflow-vault/entries/{entry_id}/examples/{example_id}/delete")
async def post_delete_example(request):
    vault_root, err = _require_vault()
    if err:
        return err
    entry_id = request.match_info["entry_id"]
    example_id = request.match_info["example_id"]
    slug, manifest, err = _require_entry(vault_root, entry_id)
    if err:
        return err

    async with _write_lock(vault_root):
        slug, manifest, err = _require_entry(vault_root, entry_id)
        if err:
            return err
        ok, err_msg = examples.delete_example(vault_root, manifest, slug, example_id)
        if not ok:
            return _error(err_msg)
        storage.write_manifest(vault_root, slug, manifest)
        return web.json_response(storage.build_entry_state(vault_root, slug))


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------

@_post("/workflow-vault/folders")
async def post_create_folder(request):
    vault_root, err = _require_vault()
    if err:
        return err
    body, err = await _read_json(request)
    if err:
        return err
    async with _write_lock(vault_root):
        folder, err_msg = folders.create_folder(vault_root, body.get("name"), body.get("parent_id"))
        if err_msg:
            return _error(err_msg)
        return web.json_response({"folder": folder, "folders": storage.read_folders(vault_root)})


@_post("/workflow-vault/folders/{folder_id}")
async def post_update_folder(request):
    vault_root, err = _require_vault()
    if err:
        return err
    folder_id = request.match_info["folder_id"]
    body, err = await _read_json(request)
    if err:
        return err

    kwargs = {}
    if "name" in body:
        kwargs["name"] = body["name"]
    if "parent_id" in body:
        kwargs["parent_id"] = body["parent_id"]

    async with _write_lock(vault_root):
        folder, err_msg = folders.update_folder(vault_root, folder_id, **kwargs)
        if err_msg:
            return _error(err_msg)
        return web.json_response({"folder": folder, "folders": storage.read_folders(vault_root)})


@_post("/workflow-vault/folders/{folder_id}/delete")
async def post_delete_folder(request):
    vault_root, err = _require_vault()
    if err:
        return err
    folder_id = request.match_info["folder_id"]

    async with _write_lock(vault_root):
        ok, err_msg = folders.delete_folder(vault_root, folder_id)
        if not ok:
            return _error(err_msg)
        return web.json_response(_full_state(vault_root))


# ---------------------------------------------------------------------------
# Tags (vault-wide)
# ---------------------------------------------------------------------------

@_post("/workflow-vault/tags/rename")
async def post_rename_tag(request):
    vault_root, err = _require_vault()
    if err:
        return err
    body, err = await _read_json(request)
    if err:
        return err
    err = _require_admin("rename tags across the whole vault")
    if err:
        return err
    async with _write_lock(vault_root):
        count, err_msg = entries.rename_tag(vault_root, body.get("from"), body.get("to"))
        if err_msg:
            return _error(err_msg)
        return web.json_response({"updated": count, **_full_state(vault_root)})


@_post("/workflow-vault/tags/delete")
async def post_delete_tag(request):
    vault_root, err = _require_vault()
    if err:
        return err
    body, err = await _read_json(request)
    if err:
        return err
    err = _require_admin("delete tags across the whole vault")
    if err:
        return err
    async with _write_lock(vault_root):
        count, err_msg = entries.delete_tag(vault_root, body.get("tag"))
        if err_msg:
            return _error(err_msg)
        return web.json_response({"updated": count, **_full_state(vault_root)})


@_post("/workflow-vault/convert-folders-to-tags")
async def post_convert_folders_to_tags(request):
    """Explicit one-time migration of legacy folder paths into plain tags.

    Additive and non-destructive: folders.json and entry folder_id values are
    left untouched. Distinct path (not /folders/...) so it never collides with
    the dynamic /workflow-vault/folders/{folder_id} route."""
    vault_root, err = _require_vault()
    if err:
        return err
    body = {}
    if request.can_read_body:
        body, err = await _read_json(request)
        if err:
            return err
    allowed = body.get("tags") if isinstance(body.get("tags"), list) else None
    err = _require_admin("convert legacy folders to tags")
    if err:
        return err
    async with _write_lock(vault_root):
        result = await asyncio.to_thread(entries.convert_folders_to_tags, vault_root, allowed)
        return web.json_response({"ok": True, **result, **_full_state(vault_root)})


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------

@routes.get("/workflow-vault/media")
async def get_media(request):
    vault_root = config.get_vault_root()
    if not vault_root or not config.is_initialized(vault_root):
        return _error("Vault is not configured.", 404)

    entry_id = request.query.get("entry_id")
    rel_path = request.query.get("path")

    abs_path, err_msg = media.resolve_media_path(vault_root, entry_id, rel_path)
    if err_msg:
        return _error(err_msg, 404)

    return web.FileResponse(abs_path)
