# CLAUDE.md

Guidance for Claude Code (and anyone else) working on this repository.

## What this is

The **studio (multi-user) edition** of ComfyUI Workflow Vault, a fork of
[avillabon/ComfyUI_Workflow_Vault](https://github.com/avillabon/ComfyUI_Workflow_Vault)
(GPL-3.0). It adds two things on top of the original:

- **Studio mode:** one shared vault on network storage. Every artist runs
  their own ComfyUI as their own OS user. See [STUDIO.md](STUDIO.md).
- **Projects:** a project → sequence → shot hierarchy, each place holding its
  own copies of workflows, with an optional ShotGrid sync. See the README.

Without a studio config, the extension must behave exactly like the original
single-user vault.

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
| `workflow_vault/studio.py` | Studio config, current user, admins |
| `workflow_vault/utils.py` | Atomic writes, shared permissions, trash |
| `workflow_vault/locking.py` | Cross-process vault lock (`.wv_lock`) |
| `workflow_vault/prefs.py` | Per-artist favorites and view settings |
| `workflow_vault/projects.py` | Project/sequence/shot registry (`projects.json`) |
| `workflow_vault/shotgrid.py` | ShotGrid sync |
| `workflow_vault/entries.py`, `versions.py`, `examples.py`, `media.py`, `storage.py`, `config.py` | Vault data model, mostly from upstream |
| `workflow_vault/admin.py`, `vault_admin.py` | Admin CLI for the pipeline team |
| `web/` | Frontend: plain ES modules, no build step. `vault_app.js` is the controller; `vault_projects.js` holds the Projects UI. |
| `tests/` | Integration tests |

## Conventions

- Every mutating route is registered with `_post()` (CSRF guard and
  "vault busy" handling). Each read-modify-write runs inside
  `async with _write_lock(vault_root)`.
- Write vault files only through `utils.atomic_write_*` and create folders
  with `utils.makedirs`: they apply the shared permissions. Never call
  `tempfile.mkstemp` or `os.makedirs` directly in vault code; owner-only files
  were the original multi-user bug.
- Delete with `utils.send_to_trash(path, vault_root)`, so studio vaults use
  the shared `.trash`.
- Record authorship with `utils.stamp_created()` / `utils.touch()`.
- In studio mode, per-artist state goes through `prefs.py`, never the shared
  manifest.
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
