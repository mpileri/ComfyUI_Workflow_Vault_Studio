# Shared team vaults

This fork of [ComfyUI Workflow Vault](https://github.com/avillabon/ComfyUI_Workflow_Vault)
lets a whole team share one vault on a network drive. Each artist runs their
own ComfyUI as their own user (Linux, Windows or macOS).

Vaults that aren't shared behave exactly like the original single-user vault.

## Quick start (plug and play, no IT needed)

1. **Install on each artist's machine.** In ComfyUI Manager, choose
   *Install via Git URL* and paste `https://github.com/mpileri/ComfyUI_Workflow_Vault_Studio`
   (or `git clone` it into `custom_nodes`, see the README). Then restart
   ComfyUI. Remove the original Workflow Vault first if it's installed: the two
   can't run side by side.
2. **One artist shares a vault.** Open the Vault and choose a folder on the
   network drive that everyone can open. Turn on **Shared team vault**, then
   click **Use this folder**.
   - Already using a vault there? Go to **Settings → Vaults → Team sharing →
     Share this vault with my team** instead.
3. **Everyone else joins.** Open the Vault and choose **the same folder**.
   It's recognized as a team vault automatically.

That's all. Nobody creates groups, changes user accounts or edits config
files. The vault remembers that it's shared in a small `shared_vault.json`
inside the folder.

Optionally, in **Settings → Vaults → Team sharing**, list the logins of a few
**admins** (include yourself). Admins manage tags, compression, projects and
sharing, and can delete anyone's work. Everyone else can delete only what they
saved. With no admins listed, everyone can do everything.

## What sharing changes

| Problem with sharing the original vault | Cause | In a shared vault |
|---|---|---|
| A workflow saved by one artist can't be opened by anyone else | Every file was created owner-only (`0600`, a side effect of `tempfile.mkstemp`) and folders `0755` | Everything the vault writes is readable and writable by everyone who can open the folder |
| Two artists saving at the same time can lose one save | Each ComfyUI only coordinated its *own* writes | A lock file in the vault (`.wv_lock`), honored across machines over NFS and SMB |
| Deletes vanish into one artist's personal trash, or become permanent on network drives | It used the OS trash | Deletes go to the vault's own `.trash/` folder and can be restored |
| One artist's favorites, card size or sort order change it for everyone | Stored in shared files | Stored per artist in `.users/<login>.json` |
| No idea who saved what | Not recorded | Entries, versions and examples record who created and last changed them, shown in the UI |
| Editing an entry silently overwrites a colleague's change | Last write wins | The save is refused with "changed by X since you opened it" |

Outside shared vaults, files are now created with normal permissions instead
of owner-only.

## Who is who

The vault records every save under the artist's name and uses that name for
personal favorites, admins, and the optional "Add my name to the workflow
name" (`comp_cleanup_j.doe`). The name comes from:

1. **The artist's ComfyUI login**, when ComfyUI runs with its user accounts
   (`--multi-user`, where each artist picks or creates their user in the
   browser). This is what tells artists apart when several share one ComfyUI.
2. Otherwise `$WORKFLOW_VAULT_USER`, if set.
3. Otherwise the computer login of whoever runs ComfyUI.

ComfyUI's user accounts aren't passwords: anyone can pick any user in the
browser. The names keep honest people from overwriting each other; they don't
stop someone determined.

## Who can open a shared vault

- **Default: everyone who can open the folder** can read and change the vault
  (files `rw-rw-rw-`, folders `rwxrwxrwx`). The network share's own access
  rules still decide who can reach the folder in the first place, so put the
  vault in a folder only your team can reach.
- **Restricted to one Unix group** (optional): if IT creates a group for the
  artists, switch **Team sharing → Who can open it** to *Only one Unix group*
  and enter its name. The group needs the same GID on every machine. Files then
  become `rw-rw-r--`, and folders `rwxrwsr-x` in that group.
- The admin role is a guardrail in the vault's UI and API, not real security:
  anyone who can write to the folder can still change its files directly.

## Sharing a vault made with the original extension

Its files are owner-only, and only a file's owner (or root) can change that.
After you share the vault:

- Each artist's own files open up **automatically** the first time they open
  the vault with this version. This happens in the background. They can also
  click **Fix my files** in Settings → Storage → Vault health.
- **Vault health → Check vault** lists whose files are still locked.
- To fix everyone's files at once, someone with root on a machine that mounts
  the share can run (on the file server itself if the share uses
  `root_squash`):
  ```bash
  sudo python vault_admin.py fix-permissions --vault /path/to/vault
  ```
- **Make sure everyone upgrades.** The original extension keeps saving
  owner-only files.

## Managed setup (optional, with IT)

A pipeline team can instead define the studio's vaults centrally. These vaults
are locked in the UI, so they can't be renamed, repointed or removed. Copy
`studio_config.example.json` to `studio_config.json` in the extension folder,
or point every machine at a shared copy:

```bash
export WORKFLOW_VAULT_STUDIO_CONFIG=/mnt/pipeline/comfyui/studio_config.json
```

| Key | Meaning |
|---|---|
| `vaults` | The studio's vaults: `id`, `name`, `path`. `path` is a string, or `{"linux": ..., "windows": ..., "darwin": ...}` when the share mounts in different places per OS. |
| `default_vault` | Which vault artists land in. Defaults to the first one. |
| `allow_personal_vaults` | Let artists add their own vaults too. Default `false` when vaults are defined. |
| `permissions` | `"open"` (default without a group) or `"group"` (default when `group` is set). |
| `group` | The shared Unix group, for group mode. |
| `file_mode` / `dir_mode` | Override the octal permissions, e.g. `"664"` / `"2775"`. |
| `trash` | `"vault"` (default): deletes go to `<vault>/.trash/`. `"os"`: the artist's own trash. |
| `admins` | Logins allowed to make vault-wide changes and delete anyone's work. Empty: everyone. |
| `shotgrid` | Optional ShotGrid connection (see [ShotGrid sync](#shotgrid-sync)). |

Edits are picked up without restarting ComfyUI. For a single vault, you can
use environment variables instead of a file (`WORKFLOW_VAULT_ROOT`, plus
`WORKFLOW_VAULT_GROUP` for group mode). With a managed config, each artist's own
choices are stored in `~/.config/comfyui-workflow-vault/` (`%APPDATA%` on
Windows) rather than in the extension folder.

## ShotGrid sync

Projects, assets, sequences and shots can be pulled from ShotGrid (Flow
Production Tracking) instead of being typed in. Add to the studio config:

```json
"shotgrid": {
  "url": "https://yourstudio.shotgrid.autodesk.com",
  "script_name": "comfyui_vault",
  "script_key_file": "/etc/comfyui/shotgrid_key",
  "projects": ["DEMO"]
}
```

`url` and `script_name` can also come from environment variables
(`$SHOTGRID_URL`, `$SHOTGRID_SCRIPT_NAME`), with the key in
`$SHOTGRID_SCRIPT_KEY`. In that case no config file is needed.

- Create a **script** in ShotGrid (Admin → Scripts) with read access to
  Projects, Assets, Sequences and Shots; `script_name` is its name.
- The **script key is a password.** Never put it inside the vault folder or a
  config file everyone can read. Use `$SHOTGRID_SCRIPT_KEY`, or a key file only
  the pipeline account can read (`script_key_file`).
- `projects` (optional) limits the sync to these project names or codes.
- Install the client where the sync runs: `pip install shotgun-api3`.

Run it from cron on a machine that holds the key:

```bash
python vault_admin.py shotgrid-sync --vault /path/to/vault
```

Admins can also sync from the vault sidebar (sync icon) on a machine that has
the key and the package. The sync only adds and updates:
- ShotGrid-linked items follow ShotGrid's codes and names.
- Omitted shots and assets (`omt`) are archived, and each asset gets its
  ShotGrid asset type.
- Shots without a sequence go under `NO_SEQUENCE`.
- Projects, assets, sequences and shots created by hand with the same code are
  linked, not duplicated.
- Nothing is ever deleted, since a shot may already hold workflows.

## Network storage notes

- **NFS:** cross-machine locking needs the lock manager (built into NFSv4;
  `rpc.statd`/`lockd` for NFSv3), so don't mount with `nolock`. Because of
  attribute caching, a colleague's new entry can take up to the mount's
  `actimeo` (often 30–60 s) to appear in your grid.
- **SMB/CIFS from Linux:** without Unix extensions, the mount options decide
  permissions. A typical team mount already gives everyone read/write
  (`file_mode=0666,dir_mode=0777`). Don't use `nobrl`: it disables the
  byte-range locks the vault lock relies on.
- **Windows clients:** permissions come from the server's ACLs. Cross-process
  locking isn't used on Windows, so avoid two Windows artists saving to the
  same entry in the same second.

## Admin tool reference

None of these commands is needed for the plug-and-play setup; they're for
inspecting, repairing and scripting. Run them from the extension folder (no
ComfyUI needed). `--vault` takes a folder, or a vault id from a managed config.

```text
python vault_admin.py status --vault <folder>              sharing, admins, your role
python vault_admin.py init --shared --vault <folder>       create a shared team vault
python vault_admin.py init --group G --vault <folder>      ...restricted to Unix group G
python vault_admin.py check-permissions --vault <folder>   whose files aren't shared yet
python vault_admin.py fix-permissions --vault <folder>     open up everyone's files (run as root)
python vault_admin.py trash list --vault <folder>          deleted items, who deleted them, when
python vault_admin.py trash restore <id> --vault <folder>  put one back where it was
python vault_admin.py trash purge --older-than 30 ...      permanently delete old trash
python vault_admin.py shotgrid-sync --vault <folder>       pull projects, assets, sequences and shots
```

## Environment variables

| Variable | Purpose |
|---|---|
| `WORKFLOW_VAULT_USER` | Name recorded as author (default: the OS login). Useful when ComfyUI runs under a service account. |
| `WORKFLOW_VAULT_STUDIO_CONFIG` | Managed setup: path to the studio config file |
| `WORKFLOW_VAULT_ROOT` | Managed setup: a single studio vault folder |
| `WORKFLOW_VAULT_GROUP` | Managed setup: the Unix group, for group mode |
| `SHOTGRID_URL`, `SHOTGRID_SCRIPT_NAME`, `SHOTGRID_SCRIPT_KEY` | ShotGrid connection |

## Tests

```bash
pip install aiohttp Pillow
python -m unittest discover -s tests -v
```
