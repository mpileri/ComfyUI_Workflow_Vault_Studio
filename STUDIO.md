# Studio mode: one shared vault for the whole studio

This fork of [ComfyUI Workflow Vault](https://github.com/avillabon/ComfyUI_Workflow_Vault)
adds a **studio mode**: one vault on network storage, used by every artist,
each running their own ComfyUI as their own Linux (or Windows/macOS) user.

Without a studio config, the extension behaves exactly like the original
single-user vault.

## What was wrong with sharing the original vault

Pointing several artists' ComfyUI at the same vault folder breaks in a few ways:

| Problem | Cause | Fix in studio mode |
|---|---|---|
| A workflow saved by one artist can't be opened by anyone else | Every file was written through `tempfile.mkstemp`, which always creates files `0600` (owner-only), and folders were `0755` | Files are written `0664` and folders `2775` (setgid), owned by the studio's group |
| Two artists saving at the same time can lose one save | Each ComfyUI only serialized its *own* writes | A lock file in the vault (`.wv_lock`), honored across machines over NFS and SMB |
| Deletes vanish into one artist's personal trash, or become permanent on network mounts | It used the OS trash | Deletes go to the vault's shared `.trash/`, restorable by an admin |
| One artist's favorites, card size or sort order change it for everyone | Stored in the shared manifest / `vault_settings.json` | Stored per artist in `.users/<login>.json` |
| Anyone can repoint or "remove" the shared vault from the UI | The vault list was a per-install setting | Studio vaults are defined centrally and locked in the UI |
| No idea who saved what | Not recorded | Entries, versions and examples record `created_by` / `updated_by`, shown in the UI |
| Editing an entry silently overwrites a colleague's change made meanwhile | Last write wins | The save is refused with "changed by X since you opened it" |

The permission fix (no more owner-only files) applies outside studio mode too.

## Setup (pipeline / IT)

### 1. A shared group

Every artist must be in one Unix group, with **the same GID on every machine**
(from LDAP/AD/NIS, or created by hand with a fixed GID):

```bash
sudo groupadd --gid 3000 comfyusers
sudo usermod -aG comfyusers alice      # for each artist; they must log in again
```

### 2. Install the extension

Clone https://github.com/mpileri/ComfyUI_Workflow_Vault_Studio into
`ComfyUI/custom_nodes/` on every workstation (or once in a shared ComfyUI
install) and install its requirements, as described in the README. Remove the
original Workflow Vault first if it's installed: the two can't run side by side.

### 3. The studio config

Copy `studio_config.example.json` to `studio_config.json` **in the extension
folder**, or put it anywhere and point every machine at it with an environment
variable (handy when the config lives on the pipeline share):

```bash
export WORKFLOW_VAULT_STUDIO_CONFIG=/mnt/pipeline/comfyui/studio_config.json
```

| Key | Meaning |
|---|---|
| `vaults` | The shared vaults: `id`, `name`, and `path`. `path` is a string, or `{"linux": ..., "windows": ..., "darwin": ...}` when the share mounts in different places per OS. |
| `default_vault` | Which vault artists land in the first time. Defaults to the first one. |
| `allow_personal_vaults` | Let artists add their own private vaults too. Default `false` when studio vaults are defined. |
| `group` | The shared Unix group. New files and folders are given this group. |
| `file_mode` / `dir_mode` | Octal permissions for new files / folders. Defaults `664` / `2775`. |
| `trash` | `"vault"` (default): deletes go to `<vault>/.trash/`. `"os"`: the deleting artist's own trash. |
| `admins` | Logins allowed to make vault-wide changes (rename/delete tags, compression settings, accent color, batch re-compress, cleanup, projects/sequences/shots, ShotGrid sync) and to delete anyone's work. Empty: everyone is an admin. |
| `shotgrid` | Optional ShotGrid connection for syncing projects (see [ShotGrid sync](#shotgrid-sync)). |

Edits to the config are picked up without restarting ComfyUI.

For a single vault you can skip the file and use environment variables only:

```bash
export WORKFLOW_VAULT_ROOT=/mnt/pipeline/comfyui/workflow_vault
export WORKFLOW_VAULT_GROUP=comfyusers
```

### 4. Create the vault folder

```bash
cd ComfyUI/custom_nodes/ComfyUI_Workflow_Vault_Studio
python vault_admin.py init        # creates the folder with the right group and setgid
python vault_admin.py status      # shows the config and each vault's state
```

The first artist to open the vault would also initialize it, but `init` makes
sure the folder itself has the right group and setgid bit from the start.

### 5. Moving an existing vault (made with the original extension)

Its files are owner-only, so fix them once, as root (or on the file server if
the share uses `root_squash`):

```bash
sudo python vault_admin.py fix-permissions --dry-run   # see what would change
sudo python vault_admin.py fix-permissions
python vault_admin.py check-permissions                # should say OK
```

**Settings → Storage → Vault health → Check vault** also lists permission
problems in studio mode.

## ShotGrid sync

Projects, sequences and shots can be pulled from ShotGrid (Flow Production
Tracking) instead of being typed in. Add to the studio config:

```json
"shotgrid": {
  "url": "https://yourstudio.shotgrid.autodesk.com",
  "script_name": "comfyui_vault",
  "script_key_file": "/etc/comfyui/shotgrid_key",
  "projects": ["DEMO"]
}
```

- Create a **script** in ShotGrid (Admin → Scripts) with read access to
  Projects, Sequences and Shots; `script_name` is its name.
- The **script key is a password**. Don't put it in the studio config (every
  artist can read that file): put it in `$SHOTGRID_SCRIPT_KEY` or in a file only
  the pipeline account can read (`script_key_file`). `url` and `script_name`
  can also come from `$SHOTGRID_URL` / `$SHOTGRID_SCRIPT_NAME`.
- `projects` (optional) limits the sync to these project names or codes;
  without it, every active non-template project is synced.
- Install the client where the sync runs: `pip install shotgun-api3`.

Run it from cron on the pipeline machine that holds the key:

```bash
python vault_admin.py shotgrid-sync
```

Admins can also sync from the vault sidebar (sync icon) when that machine has
the key and the package. The sync only adds and updates: ShotGrid-linked items
follow ShotGrid's codes and names, omitted shots (`omt`) are archived, shots
without a sequence go under `NO_SEQUENCE`, and nothing is ever deleted, since a
shot may already hold workflows. Projects/sequences/shots created by hand with
the same code are linked rather than duplicated.

## Network storage notes

- **NFS:** cross-machine locking needs the lock manager (built into NFSv4;
  `rpc.statd`/`lockd` for NFSv3). Don't mount with `nolock`. Attribute caching
  can delay a colleague's new entry by up to the mount's `actimeo` (often
  30–60 s) before it shows up in your grid.
- **SMB/CIFS from Linux:** with Unix extensions, permissions work as above.
  Without them, the mount options decide permissions; use something like
  `uid=…,gid=comfyusers,file_mode=0664,dir_mode=0775` and don't use `nobrl`
  (it disables the byte-range locks the vault lock relies on).
- **Windows clients:** permissions come from the server's ACLs (give the artists'
  group Modify on the vault folder). Cross-process locking is not used on
  Windows, so avoid two Windows artists saving to the same entry at the same
  second.
- Artists' ComfyUI processes don't need any special umask.

## Admin tool reference

Run from the extension folder; no ComfyUI needed. All commands take
`--vault <id or path>` (default: the studio's default vault).

```text
python vault_admin.py status                       config, your role, each vault's state
python vault_admin.py init [--group G] [--force]   create/initialize a vault folder
python vault_admin.py check-permissions            list what other artists can't use
python vault_admin.py fix-permissions [--dry-run]  apply the group + modes everywhere
python vault_admin.py trash list                   deleted items, who deleted them, when
python vault_admin.py trash restore <id>           put one back where it was
python vault_admin.py trash purge --older-than 30  permanently delete old trash
python vault_admin.py shotgrid-sync                pull projects, sequences and shots from ShotGrid
```

## What artists see

- The studio vault opens directly; there's no folder to choose.
- **Settings → Vaults** shows who they're working as and what's shared.
  Studio vaults carry a *Studio* badge and can't be renamed, repointed or removed.
- Admin-only settings are greyed out for non-admins, with the list of admins.
- Entry details and versions show who created and last updated them.
- Favorites and view settings are personal.
- Deleting their own entries/versions works; deleting a colleague's needs an admin.

## Environment variables

| Variable | Purpose |
|---|---|
| `WORKFLOW_VAULT_STUDIO_CONFIG` | Path to the studio config file |
| `WORKFLOW_VAULT_ROOT` | A single shared vault folder (shown as "Studio") |
| `WORKFLOW_VAULT_GROUP` | The shared group, if not in the config file |
| `WORKFLOW_VAULT_USER` | Name recorded as author (default: the OS login). Useful when ComfyUI runs under a service account. |
| `SHOTGRID_URL`, `SHOTGRID_SCRIPT_NAME`, `SHOTGRID_SCRIPT_KEY` | ShotGrid connection (see [ShotGrid sync](#shotgrid-sync)) |

## Limits

- The admin role is a guardrail in the vault UI and API, not a security
  boundary: anyone with write access to the share can still change files
  directly. Use filesystem permissions for real access control.
- Per-artist settings are stored in `~/.config/comfyui-workflow-vault/`
  (`%APPDATA%` on Windows) and in `<vault>/.users/`.

## Tests

```bash
pip install aiohttp Pillow
python -m unittest discover -s tests -v
```
