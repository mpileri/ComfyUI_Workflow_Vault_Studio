"""Command-line tools for a shared vault. None of them is needed for the
plug-and-play setup (artists share a vault from the UI); they're for
inspecting, repairing, and scripting.

Run from the extension folder (no ComfyUI needed):

    python vault_admin.py status --vault /mnt/share/vault
    python vault_admin.py init --shared --vault /mnt/share/vault   # create a shared team vault
    python vault_admin.py check-permissions --vault ...
    sudo python vault_admin.py fix-permissions --vault ...         # open up everyone's files at once
    python vault_admin.py trash list|restore <id>|purge --older-than 30
    python vault_admin.py shotgrid-sync                            # cron-able

--vault takes a folder path, or a vault id from a managed studio config
(default: that config's default vault).
"""

import argparse
import os
import shutil
import stat
import sys
import time
from datetime import datetime, timedelta, timezone

from . import config, locking, sharing, shotgrid, studio, utils


def _fail(message):
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def _resolve_vault(arg):
    """A managed vault id, a path, or (default) the managed default vault.
    Also makes it the active vault, so writes follow its sharing policy."""
    if arg:
        match = next((v for v in studio.vaults() if v["id"] == arg), None)
        root = match["vault_root"] if match else os.path.abspath(os.path.expanduser(arg))
    else:
        vid = studio.default_vault_id()
        if not vid:
            _fail("which vault? Pass --vault <folder>.")
        root = next(v for v in studio.vaults() if v["id"] == vid)["vault_root"]
    studio.set_active_vault(root)
    return root


def _policy(root, args):
    """The sharing policy to apply: --group forces group mode, otherwise the
    vault's own."""
    group = getattr(args, "group", None)
    if group:
        if studio._resolve_gid(group) is None:
            _fail(f"group {group!r} does not exist on this machine.")
        return studio._make_policy({"permissions": "group", "group": group}, False, "command line")
    pol = studio.policy(root)
    if not pol:
        _fail(f"{root} isn't a shared vault. Share it from the vault (Settings → Vaults → Team sharing), "
              f"or run: python vault_admin.py init --shared --vault {root}")
    return pol


def _walk(root):
    yield root, True
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            yield os.path.join(dirpath, name), True
        for name in filenames:
            yield os.path.join(dirpath, name), False


def _describe(pol):
    if pol["mode"] == "group":
        return f"restricted to group {pol['group']} (files {oct(pol['file_mode'])}, folders {oct(pol['dir_mode'])})"
    return f"open to everyone who can reach the folder (files {oct(pol['file_mode'])}, folders {oct(pol['dir_mode'])})"


# ---------------------------------------------------------------------------
# status / init / permissions
# ---------------------------------------------------------------------------

def cmd_status(args):
    dep = studio.deployment()
    print(f"You are: {studio.current_user()}")
    if dep:
        print(f"Managed studio config: {dep['path'] or '(from environment)'}")
        if dep["error"]:
            print(f"  CONFIG ERROR: {dep['error']}")
        print(f"  personal vaults: {'allowed' if dep['allow_personal_vaults'] else 'off'}")
        print(f"  per-user config: {studio.user_config_path()}")
        for v in dep["vaults"]:
            default = " (default)" if v["id"] == dep["default_vault"] else ""
            print(f"  vault {v['id']}{default}: {v['name']}  {v['vault_root']}")
    else:
        print("Managed studio config: none (plug-and-play: vaults are shared from the UI)")
    if not args.vault and not (dep and dep["default_vault"]):
        print("\nPass --vault <folder> to inspect a vault.")
        return
    root = _resolve_vault(args.vault)
    print(f"\nVault: {root}")
    if not os.path.isdir(root):
        print("  MISSING (not reachable from this machine)")
        return
    if not config.is_initialized(root):
        print("  not initialized (run: init)")
        return
    print(f"  writable by you: {'yes' if os.access(root, os.W_OK) else 'NO'}")
    pol = studio.policy(root)
    if not pol:
        print("  sharing: private (not shared with the team)")
        return
    print(f"  sharing: {'managed by the studio config' if pol['managed'] else 'shared team vault'}, {_describe(pol)}")
    print(f"  admins: {', '.join(pol['admins']) or '(none: everyone is an admin)'}"
          f"{'  — you are one' if studio.is_admin(vault_root=root) else ''}")
    print(f"  deletes go to: {'<vault>/.trash' if pol['trash'] == 'vault' else 'each artist’s OS trash'}")


def cmd_init(args):
    root = _resolve_vault(args.vault)
    if os.path.isdir(root) and not config.is_empty(root) and not config.is_initialized(root) and not args.force:
        _fail(f"{root} is not empty and isn't a vault yet. Re-run with --force to create the vault files in it.")
    os.makedirs(root, exist_ok=True)
    config.initialize_vault(root)
    if args.shared or args.group:
        if args.group and studio._resolve_gid(args.group) is None:
            _fail(f"group {args.group!r} does not exist on this machine.")
        pol, err = sharing.enable(root, "group" if args.group else "open", args.group, [])
        if err:
            _fail(err)
    pol = studio.policy(root)
    if pol:
        sharing.heal(root, pol)
        print(f"Shared vault ready at {root}: {_describe(pol)}.")
        print("Artists join it by choosing this folder in the vault.")
    else:
        print(f"Private vault ready at {root}. Add --shared to share it with the team.")


def cmd_check_permissions(args):
    root = _resolve_vault(args.vault)
    if not os.path.isdir(root):
        _fail(f"{root} does not exist.")
    pol = _policy(root, args)
    report = sharing.scan(root, pol)
    if not report["problems"] and not report["unreadable"]:
        print(f"OK: everything under {root} is shared ({_describe(pol)}).")
        return
    for owner, info in sorted(report["by_owner"].items()):
        print(f"{info['count']} path(s) owned by {owner} aren't shared, e.g. {', '.join(info['samples'])}")
    if report["unreadable"]:
        print(f"You can't read: {', '.join(report['unreadable'])}")
    print("\nEach artist's files are fixed automatically when they open the vault; "
          "to fix everyone's at once: sudo python vault_admin.py fix-permissions")
    sys.exit(2)


def cmd_fix_permissions(args):
    root = _resolve_vault(args.vault)
    if not os.path.isdir(root):
        _fail(f"{root} does not exist.")
    pol = _policy(root, args)
    gid, file_mode, dir_mode = pol["gid"], pol["file_mode"], pol["dir_mode"]
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
                what.append(f"group -> {pol['group']}")
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
        print("Files owned by other users can only be changed by their owner or root: re-run with sudo "
              "(or let each artist open the vault, which fixes their own files).")
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


# ---------------------------------------------------------------------------
# ShotGrid
# ---------------------------------------------------------------------------

def cmd_shotgrid_sync(args):
    root = _resolve_vault(args.vault)
    if not config.is_initialized(root):
        _fail(f"{root} is not an initialized vault (run: init).")
    ok, reason = shotgrid.status()
    if not ok:
        _fail(reason)
    data = shotgrid.fetch()
    lock = locking.VaultFileLock(root)
    deadline = time.time() + 120
    while not lock.try_acquire():
        if time.time() > deadline:
            lock.release()
            _fail("the vault stayed busy for 2 minutes; try again later.")
        time.sleep(0.5)
    try:
        counts = shotgrid.sync(root, data)
    finally:
        lock.release()
    print(f"ShotGrid sync done: {shotgrid.summary(counts)}.")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="vault_admin.py", description="Manage a shared (studio) Workflow Vault.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_text):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--vault", help="studio vault id or folder path (default: the studio's default vault)")
        p.set_defaults(func=func)
        return p

    add("status", cmd_status, "show the studio configuration and the state of each vault")
    p = add("init", cmd_init, "create and initialize a vault folder")
    p.add_argument("--shared", action="store_true", help="share it with the team (open to everyone who can reach the folder)")
    p.add_argument("--group", help="share it, restricted to this Unix group instead")
    p.add_argument("--force", action="store_true", help="initialize inside a non-empty folder")
    p = add("check-permissions", cmd_check_permissions, "list files and folders other artists can't fully use")
    p.add_argument("--group", help="check against group mode with this Unix group")
    p = add("fix-permissions", cmd_fix_permissions, "give the whole vault its shared permissions (run as root to fix everyone's files)")
    p.add_argument("--group", help="use group mode with this Unix group (default: the vault's own sharing)")
    p.add_argument("--dry-run", action="store_true", help="only show what would change")

    add("shotgrid-sync", cmd_shotgrid_sync, "pull projects, sequences and shots from ShotGrid")

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
