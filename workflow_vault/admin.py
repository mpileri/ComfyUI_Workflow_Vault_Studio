"""Command-line tools for the pipeline team running a shared (studio) vault.

Run from the extension folder (no ComfyUI needed):

    python vault_admin.py status
    python vault_admin.py init                 # create/initialize the studio vault
    python vault_admin.py check-permissions
    sudo python vault_admin.py fix-permissions # repair a vault made before studio mode
    python vault_admin.py trash list
    python vault_admin.py trash restore <id>
    python vault_admin.py trash purge --older-than 30

Every command takes --vault <id or path> (default: the studio's default vault).
"""

import argparse
import os
import shutil
import stat
import sys
from datetime import datetime, timedelta, timezone

from . import config, storage, studio, utils


def _fail(message):
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def _resolve_vault(arg):
    """A studio vault id, a path, or (default) the studio's default vault."""
    if arg:
        match = next((v for v in studio.vaults() if v["id"] == arg), None)
        return match["vault_root"] if match else os.path.abspath(os.path.expanduser(arg))
    vid = studio.default_vault_id()
    if not vid:
        _fail("no studio vault is configured; pass --vault <path>, or set one up in "
              f"{studio.config_path()} (or WORKFLOW_VAULT_ROOT).")
    return next(v for v in studio.vaults() if v["id"] == vid)["vault_root"]


def _modes(args):
    cfg = studio.load() or {}
    group = args.group or cfg.get("group")
    gid = studio._resolve_gid(group) if group else None
    if group and gid is None:
        _fail(f"group {group!r} does not exist on this machine.")
    return (
        gid,
        group,
        cfg.get("file_mode", studio.DEFAULT_FILE_MODE),
        cfg.get("dir_mode", studio.DEFAULT_DIR_MODE),
    )


def _walk(root, include_trash=True):
    yield root, True
    for dirpath, dirnames, filenames in os.walk(root):
        if not include_trash and dirpath == root:
            dirnames[:] = [d for d in dirnames if d != studio.TRASH_DIRNAME]
        for name in dirnames:
            yield os.path.join(dirpath, name), True
        for name in filenames:
            yield os.path.join(dirpath, name), False


# ---------------------------------------------------------------------------
# status / init / permissions
# ---------------------------------------------------------------------------

def cmd_status(args):
    cfg = studio.load()
    if not cfg:
        print("Studio mode: OFF (no studio_config.json, $WORKFLOW_VAULT_STUDIO_CONFIG or $WORKFLOW_VAULT_ROOT)")
        print(f"Looked for: {studio.config_path()}")
        return
    print("Studio mode: ON")
    print(f"  config file:      {cfg['path'] or '(none; from environment)'}")
    if cfg["error"]:
        print(f"  CONFIG ERROR:     {cfg['error']}")
    print(f"  you are:          {studio.current_user()}{' (admin)' if studio.is_admin() else ''}")
    print(f"  admins:           {', '.join(cfg['admins']) or '(none: everyone is an admin)'}")
    print(f"  group:            {cfg['group'] or '(not set: files keep their folder group)'}"
          f"{'  [MISSING on this machine]' if cfg['group'] and cfg['gid'] is None else ''}")
    print(f"  file/dir modes:   {oct(cfg['file_mode'])} / {oct(cfg['dir_mode'])}")
    print(f"  deletes go to:    {'<vault>/.trash' if cfg['trash'] == 'vault' else 'each artist’s OS trash'}")
    print(f"  personal vaults:  {'allowed' if cfg['allow_personal_vaults'] else 'off'}")
    print(f"  per-user config:  {studio.user_config_path()}")
    print("  vaults:")
    if not cfg["vaults"]:
        print("    (none)")
    for v in cfg["vaults"]:
        root = v["vault_root"]
        if not os.path.isdir(root):
            state = "MISSING"
        elif not config.is_initialized(root):
            state = "not initialized (run: init)"
        else:
            state = "ok" + ("" if os.access(root, os.W_OK) else ", NOT writable by you")
        default = " (default)" if v["id"] == cfg["default_vault"] else ""
        print(f"    {v['id']}{default}: {v['name']}  {root}  [{state}]")


def cmd_init(args):
    root = _resolve_vault(args.vault)
    gid, group, file_mode, dir_mode = _modes(args)
    if os.path.isdir(root) and not config.is_empty(root) and not config.is_initialized(root) and not args.force:
        _fail(f"{root} is not empty and isn't a vault yet. Re-run with --force to create the vault files in it.")
    os.makedirs(root, exist_ok=True)
    config.initialize_vault(root)
    problems = 0
    for path, is_dir in _walk(root):
        try:
            if gid is not None:
                os.chown(path, -1, gid)
            os.chmod(path, dir_mode if is_dir else file_mode)
        except OSError as e:
            problems += 1
            print(f"could not set permissions on {path}: {e}", file=sys.stderr)
    print(f"Vault ready at {root}" + (f" (group {group})" if group else ""))
    if problems:
        print("Some paths are owned by other users; run fix-permissions with sudo.")
    if not group:
        print("note: no group configured. Every artist must share the folder's group for studio mode to work;"
              " set \"group\" in the studio config, or pass --group.")


def cmd_check_permissions(args):
    root = _resolve_vault(args.vault)
    if not os.path.isdir(root):
        _fail(f"{root} does not exist.")
    if not studio.enabled():
        _fail("studio mode is off, so there is no shared permission policy to check against.")
    count, samples = storage.scan_permissions(root)
    if not count:
        print(f"OK: everything under {root} is shared correctly.")
        return
    for s in samples:
        print(f"{s['path']}: {s['problem']}")
    if count > len(samples):
        print(f"...and {count - len(samples)} more.")
    print(f"\n{count} problem(s). Fix them with: sudo python vault_admin.py fix-permissions")
    sys.exit(2)


def cmd_fix_permissions(args):
    root = _resolve_vault(args.vault)
    if not os.path.isdir(root):
        _fail(f"{root} does not exist.")
    gid, group, file_mode, dir_mode = _modes(args)
    if gid is None:
        print("warning: no group configured; only permission bits will be fixed, not group ownership.")
    changed = failed = 0
    for path, is_dir in _walk(root):
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            continue
        want = dir_mode if is_dir else file_mode
        need_group = gid is not None and st.st_gid != gid
        need_mode = stat.S_IMODE(st.st_mode) != want
        if not (need_group or need_mode):
            continue
        rel = os.path.relpath(path, root)
        if args.dry_run:
            what = []
            if need_group:
                what.append(f"group -> {group}")
            if need_mode:
                what.append(f"mode {oct(stat.S_IMODE(st.st_mode))} -> {oct(want)}")
            print(f"would fix {rel}: {', '.join(what)}")
            changed += 1
            continue
        try:
            if need_group:
                os.chown(path, -1, gid)
            os.chmod(path, want)
            changed += 1
        except OSError as e:
            failed += 1
            print(f"could not fix {rel}: {e}", file=sys.stderr)
    verb = "would fix" if args.dry_run else "fixed"
    print(f"{verb} {changed} path(s){f', {failed} failed' if failed else ''}.")
    if failed:
        print("Files owned by other users can only be changed by their owner or root: re-run with sudo.")
        sys.exit(2)


# ---------------------------------------------------------------------------
# trash
# ---------------------------------------------------------------------------

def _trash_items(root):
    tdir = os.path.join(root, studio.TRASH_DIRNAME)
    if not os.path.isdir(tdir):
        return []
    items = []
    for name in sorted(os.listdir(tdir)):
        item_root = os.path.join(tdir, name)
        info = utils.read_json(os.path.join(item_root, "trash_info.json"), default=None)
        if os.path.isdir(item_root) and isinstance(info, dict) and info.get("original_path"):
            items.append({"id": name, "root": item_root, **info})
    return items


def _parse_time(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def cmd_trash_list(args):
    root = _resolve_vault(args.vault)
    items = _trash_items(root)
    if not items:
        print("The vault trash is empty.")
        return
    for it in items:
        print(f"{it['id']}\n    {it['original_path']}  (deleted by {it.get('deleted_by', '?')} at {it.get('deleted_at', '?')})")


def cmd_trash_restore(args):
    root = _resolve_vault(args.vault)
    item = next((it for it in _trash_items(root) if it["id"] == args.id), None)
    if not item:
        _fail(f"no trash item {args.id!r}. See: python vault_admin.py trash list")
    rel = os.path.normpath(item["original_path"])
    if os.path.isabs(rel) or rel.startswith(".."):
        _fail("trash item has an invalid original path.")
    src = os.path.join(item["root"], rel)
    dest = os.path.join(root, rel)
    if not os.path.exists(src):
        _fail(f"the trashed data is missing: {src}")
    if os.path.exists(dest):
        _fail(f"{rel} already exists in the vault; move it aside first.")
    if not os.path.isdir(os.path.dirname(dest)):
        _fail(f"{os.path.dirname(rel)} no longer exists in the vault (was its entry renamed or deleted?).")
    os.rename(src, dest)
    shutil.rmtree(item["root"], ignore_errors=True)
    print(f"Restored {rel}.")


def cmd_trash_purge(args):
    root = _resolve_vault(args.vault)
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.older_than)
    doomed = []
    for it in _trash_items(root):
        when = _parse_time(it.get("deleted_at"))
        if when and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when and when < cutoff:
            doomed.append(it)
    if not doomed:
        print(f"Nothing in the trash is older than {args.older_than} day(s).")
        return
    for it in doomed:
        print(f"  {it['original_path']}  (deleted {it.get('deleted_at')})")
    if not args.yes:
        answer = input(f"Permanently delete these {len(doomed)} item(s)? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return
    for it in doomed:
        shutil.rmtree(it["root"], ignore_errors=True)
    print(f"Purged {len(doomed)} item(s).")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="vault_admin.py", description="Manage a shared (studio) Workflow Vault.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_text):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--vault", help="studio vault id or folder path (default: the studio's default vault)")
        p.set_defaults(func=func)
        return p

    add("status", cmd_status, "show the studio configuration and the state of each vault")
    p = add("init", cmd_init, "create and initialize a vault folder with shared permissions")
    p.add_argument("--group", help="Unix group to own the vault (default: from the studio config)")
    p.add_argument("--force", action="store_true", help="initialize inside a non-empty folder")
    add("check-permissions", cmd_check_permissions, "list files and folders other artists can't fully use")
    p = add("fix-permissions", cmd_fix_permissions, "give the whole vault the studio's group and permissions")
    p.add_argument("--group", help="Unix group to own the vault (default: from the studio config)")
    p.add_argument("--dry-run", action="store_true", help="only show what would change")

    trash = sub.add_parser("trash", help="list, restore or purge deleted items").add_subparsers(dest="trash_command", required=True)
    for name, func, help_text in (
        ("list", cmd_trash_list, "list deleted entries, versions and examples"),
        ("restore", cmd_trash_restore, "move a deleted item back into the vault"),
        ("purge", cmd_trash_purge, "permanently delete old trash"),
    ):
        p = trash.add_parser(name, help=help_text)
        p.add_argument("--vault", help="studio vault id or folder path (default: the studio's default vault)")
        p.set_defaults(func=func)
        if name == "restore":
            p.add_argument("id", help="the trash item id shown by `trash list`")
        if name == "purge":
            p.add_argument("--older-than", type=int, required=True, metavar="DAYS")
            p.add_argument("--yes", action="store_true", help="don't ask for confirmation")

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
