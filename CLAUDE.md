# CLAUDE.md

Guidance for Claude Code (and anyone else) working on this repository.

## What this is

The **studio (multi-user) edition** of ComfyUI Workflow Vault, a fork of
[avillabon/ComfyUI_Workflow_Vault](https://github.com/avillabon/ComfyUI_Workflow_Vault)
(GPL-3.0). It adds two things on top of the original:

- **Shared team vaults:** one vault in a network folder. Every artist runs
  their own ComfyUI as their own OS user. It's plug and play: the folder's
  `shared_vault.json` makes it shared, and an optional managed studio config
  can define vaults centrally. See [STUDIO.md](STUDIO.md).
- **Library and Projects:** two sidebar trees. The Library is organized in
  nested folders (`folders.json`). Projects hold assets and sequences side by
  side, and sequences hold shots (`projects.json`). Each place keeps its own
  copies of workflows, with an optional ShotGrid sync. See the README.

A vault that isn't shared must behave exactly like the original single-user
vault.

## Rules

- **Ask the maintainer before every `git push`.** Say what will be pushed and
  where, and wait for a clear yes; one yes covers one push. Committing locally
  is fine.
- **No real show, client or studio names, and no other sensitive production
  material, anywhere.** That covers code, tests, docs, UI placeholders, sample
  data, commit messages and history. Use placeholders: project `DEMO` /
  "Demo Show", `SQ010`, `SH0010`, users `alice` / `bob`, paths like
  `/mnt/pipeline/...`, group `comfyusers`. Before pushing, grep the new commits
  (`git log -p upstream/main..HEAD`) for sensitive terms.
- **Keep crediting the original author:** the `LICENSE`, the fork notice at
  the top of the README, and the "original by" credit in the sidebar footer.
- **The maintainer is new to Git and GitHub.** Explain steps plainly, one at a
  time. Never handle their passwords or tokens; they sign in themselves.
- **Stay mergeable with `upstream`:** prefer additive changes over rewrites of
  original code.

## Layout

| Path | Contents |
|---|---|
| `workflow_vault/api.py` | HTTP routes (aiohttp, registered on ComfyUI's server) |
| `workflow_vault/studio.py` | Sharing policy per vault (`policy(vault_root)`), managed config, current user, admins |
| `workflow_vault/sharing.py` | Share/unshare a vault, self-heal the current user's files, permission scan |
| `workflow_vault/utils.py` | Atomic writes, shared permissions, trash |
| `workflow_vault/locking.py` | Cross-process vault lock (`.wv_lock`) |
| `workflow_vault/prefs.py` | Per-artist favorites and view settings |
| `workflow_vault/projects.py` | Project/asset/sequence/shot registry (`projects.json`) |
| `workflow_vault/folders.py` | Library folders (`folders.json`) |
| `workflow_vault/shotgrid.py` | ShotGrid sync |
| `workflow_vault/entries.py`, `versions.py`, `examples.py`, `media.py`, `storage.py`, `config.py` | Vault data model, mostly from upstream |
| `workflow_vault/admin.py`, `vault_admin.py` | Admin CLI for the pipeline team |
| `web/` | Frontend: plain ES modules, no build step. `vault_app.js` is the controller; `vault_projects.js` holds the sidebar trees, places, the location picker and the copy dialog. |
| `tests/` | Integration tests |

## Conventions

- Register routes with `_get()` / `_post()`, never `routes.get/post`
  directly. Both record the artist's ComfyUI login (Comfy-User header) for
  `studio.current_user()`; `_post()` adds the CSRF guard and "vault busy"
  handling. Each read-modify-write runs inside
  `async with _write_lock(vault_root)`.
- Write vault files only through `utils.atomic_write_*` and create folders
  with `utils.makedirs`: they apply the shared permissions. Never call
  `tempfile.mkstemp` or `os.makedirs` directly in vault code; owner-only files
  were the original multi-user bug.
- Delete with `utils.send_to_trash(path, vault_root)`, so studio vaults use
  the shared `.trash`.
- Record authorship with `utils.stamp_created()` / `utils.touch()`.
- Sharing is per vault: use `studio.policy(vault_root)` when you know the
  vault. `studio.load()` / `studio.enabled()` mean the *active* vault, which
  is what file writes without a vault argument follow.
- In a shared vault, per-artist state goes through `prefs.py`, never the
  shared manifest.
- Plug and play is a hard requirement: never add a step that needs IT
  (groups, user changes, root) for the default setup.
- Frontend: build DOM with `el()` from `vault_dom.js`, and reuse the existing
  CSS tokens and classes in `workflow_vault.css`.

## Testing

```bash
pip install aiohttp Pillow
python -m unittest discover -s tests -v
```

The tests run the real API on an aiohttp test server, with a stub ComfyUI
`server` module, and simulate different artists by setting
`WORKFLOW_VAULT_USER`. Add a test for every behavior change.
