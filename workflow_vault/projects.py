"""Projects: a Project -> Sequence -> Shot hierarchy, plus each project's
Assets (characters, props, environments...), that workflows can be saved
into, alongside the studio-wide library.

The hierarchy lives in <vault>/projects.json. A workflow (entry) belongs to
the library (no context) or to exactly one place, recorded in its manifest as

    "context": {"project_id": ..., "sequence_id": ... or null, "shot_id": ... or null}

or, for an asset, {"project_id": ..., "sequence_id": null, "shot_id": null,
"asset_id": ...}. Assets sit beside the sequences, directly under a project.

Each shot keeps its own copies of workflows (copy-to-shots duplicates them),
so tweaking one shot never changes another. Entry names only need to be
unique within the same place, so every shot can have its own "Comp Cleanup".

Projects, sequences and shots are created by hand or synced from ShotGrid
(see shotgrid.py); synced items carry a `shotgrid_id`.
"""

import os
import re

from . import utils

MAX_CODE_LEN = 64
MAX_NAME_LEN = 120
LEVELS = ("project", "sequence", "shot", "asset")
MAX_TYPE_LEN = 40


def registry_path(vault_root):
    return os.path.join(vault_root, "projects.json")


def _clean_code(code):
    return " ".join(str(code or "").split())[:MAX_CODE_LEN]


def _clean_name(name):
    return " ".join(str(name or "").split())[:MAX_NAME_LEN]


def validate_code(code):
    if not code:
        return "A code is required (e.g. DEMO, SQ010, SH0010)."
    if re.search(r"[\\/]", code):
        return "Codes can't contain slashes."
    return None


def load(vault_root):
    data = utils.read_json(registry_path(vault_root), default=None)
    projects = data.get("projects") if isinstance(data, dict) else None
    return {"schema_version": "1.0", "projects": [p for p in projects or [] if isinstance(p, dict)]}


def save(vault_root, registry):
    utils.atomic_write_json(registry_path(vault_root), registry)


def _new_item(prefix, code, name, **extra):
    item = {"id": utils.generate_id(prefix), "code": code, "name": name, "archived": False}
    item.update(extra)
    utils.stamp_created(item)
    return item


def _children(item, level):
    """The list holding the next level down (created on demand)."""
    key = {"project": "sequences", "sequence": "shots"}[level]
    if not isinstance(item.get(key), list):
        item[key] = []
    return item[key]


def _code_taken(siblings, code, ignore_id=None):
    wanted = code.casefold()
    return any((s.get("code") or "").casefold() == wanted and s.get("id") != ignore_id for s in siblings)


# ---------------------------------------------------------------------------
# Lookups and contexts
# ---------------------------------------------------------------------------

def find_asset(project, asset_id):
    if not project or not asset_id:
        return None
    return next((a for a in project.get("assets") or [] if a.get("id") == asset_id), None)


def find(registry, project_id=None, sequence_id=None, shot_id=None):
    """(project, sequence, shot) for the given ids; any of them None when not
    asked for or not found. Assets: find_asset()."""
    project = next((p for p in registry["projects"] if p.get("id") == project_id), None) if project_id else None
    sequence = shot = None
    if project and sequence_id:
        sequence = next((s for s in project.get("sequences") or [] if s.get("id") == sequence_id), None)
    if sequence and shot_id:
        shot = next((s for s in sequence.get("shots") or [] if s.get("id") == shot_id), None)
    return project, sequence, shot


def normalize_context(registry, ctx):
    """Validate a context from the UI. Returns (context or None, error).
    None / {} / {"project_id": None} all mean the library."""
    if not ctx:
        return None, None
    if not isinstance(ctx, dict):
        return None, "Invalid project location."
    pid, sid, shid, aid = context_key(ctx)
    if not pid:
        if sid or shid or aid:
            return None, "Invalid project location."
        return None, None
    if shid and not sid:
        return None, "A shot needs its sequence."
    if aid and sid:
        return None, "A workflow is saved in a sequence or an asset, not both."
    project, sequence, shot = find(registry, pid, sid, shid)
    if aid:
        if not find_asset(project, aid):
            return None, "That project or asset no longer exists."
        return {"project_id": pid, "sequence_id": None, "shot_id": None, "asset_id": aid}, None
    if not project or (sid and not sequence) or (shid and not shot):
        return None, "That project, sequence or shot no longer exists."
    return {"project_id": pid, "sequence_id": sid, "shot_id": shid}, None


def context_key(ctx):
    """(project_id, sequence_id, shot_id, asset_id), Nones for what's unset."""
    ctx = ctx or {}
    return (
        ctx.get("project_id") or None,
        ctx.get("sequence_id") or None,
        ctx.get("shot_id") or None,
        ctx.get("asset_id") or None,
    )


def context_codes(registry, ctx):
    """["DEMO", "SQ010", "SH0010"] (or ["DEMO", "hero"] for an asset) for a
    context, shorter for higher levels."""
    if not ctx:
        return []
    pid, sid, shid, aid = context_key(ctx)
    project, sequence, shot = find(registry, pid, sid, shid)
    return [i["code"] for i in (project, sequence, shot, find_asset(project, aid)) if i]


def context_label(registry, ctx):
    return " › ".join(context_codes(registry, ctx)) or "Library"


def ids_in_use(contexts):
    """Every project/sequence/shot/asset id referenced by these entry contexts."""
    used = set()
    for ctx in contexts:
        used.update(i for i in context_key(ctx) if i)
    return used


# ---------------------------------------------------------------------------
# Creating and editing
# ---------------------------------------------------------------------------

def create_project(vault_root, code, name=""):
    code, name = _clean_code(code), _clean_name(name)
    err = validate_code(code)
    if err:
        return None, err
    registry = load(vault_root)
    if _code_taken(registry["projects"], code):
        return None, f"A project with code “{code}” already exists."
    project = _new_item("project", code, name, sequences=[], assets=[])
    registry["projects"].append(project)
    save(vault_root, registry)
    return project, None


def create_sequence(vault_root, project_id, code, name=""):
    code, name = _clean_code(code), _clean_name(name)
    err = validate_code(code)
    if err:
        return None, err
    registry = load(vault_root)
    project, _, _ = find(registry, project_id)
    if not project:
        return None, "Project not found."
    sequences = _children(project, "project")
    if _code_taken(sequences, code):
        return None, f"{project['code']} already has a sequence “{code}”."
    sequence = _new_item("seq", code, name, shots=[])
    sequences.append(sequence)
    save(vault_root, registry)
    return sequence, None


def parse_codes(text):
    """Shot codes from pasted text: one per line, or separated by commas,
    semicolons, tabs or spaces. Keeps order, drops duplicates."""
    if isinstance(text, list):
        parts = [str(t) for t in text]
    else:
        parts = re.split(r"[\s,;]+", str(text or ""))
    seen, codes = set(), []
    for part in parts:
        code = _clean_code(part)
        if code and code.casefold() not in seen:
            seen.add(code.casefold())
            codes.append(code)
    return codes


def create_shots(vault_root, project_id, sequence_id, codes):
    """Returns ({"created": [shots], "skipped": [codes already there]}, error)."""
    codes = parse_codes(codes)
    if not codes:
        return None, "Enter at least one shot code."
    for code in codes:
        err = validate_code(code)
        if err:
            return None, f"{code}: {err}"
    registry = load(vault_root)
    _, sequence, _ = find(registry, project_id, sequence_id)
    if not sequence:
        return None, "Sequence not found."
    shots = _children(sequence, "sequence")
    created, skipped = [], []
    for code in codes:
        if _code_taken(shots, code):
            skipped.append(code)
            continue
        shot = _new_item("shot", code, "")
        shots.append(shot)
        created.append(shot)
    if created:
        save(vault_root, registry)
    return {"created": created, "skipped": skipped}, None


def _clean_type(asset_type):
    return " ".join(str(asset_type or "").split())[:MAX_TYPE_LEN]


def create_assets(vault_root, project_id, codes, asset_type=""):
    """Add assets to a project, from pasted codes. `asset_type` (Character,
    Prop, Environment...) is optional and applies to all of them.
    Returns ({"created": [assets], "skipped": [codes already there]}, error)."""
    codes = parse_codes(codes)
    if not codes:
        return None, "Enter at least one asset code."
    for code in codes:
        err = validate_code(code)
        if err:
            return None, f"{code}: {err}"
    registry = load(vault_root)
    project, _, _ = find(registry, project_id)
    if not project:
        return None, "Project not found."
    if not isinstance(project.get("assets"), list):
        project["assets"] = []
    assets = project["assets"]
    created, skipped = [], []
    for code in codes:
        if _code_taken(assets, code):
            skipped.append(code)
            continue
        asset = _new_item("asset", code, "", type=_clean_type(asset_type))
        assets.append(asset)
        created.append(asset)
    if created:
        save(vault_root, registry)
    return {"created": created, "skipped": skipped}, None


def _locate(registry, ctx):
    """(item, siblings, level) for the deepest id in ctx."""
    pid, sid, shid, aid = context_key(ctx)
    project, sequence, shot = find(registry, pid, sid, shid)
    if aid:
        asset = find_asset(project, aid)
        return (asset, project.get("assets"), "asset") if asset else (None, None, None)
    if shid:
        return (shot, sequence.get("shots"), "shot") if shot else (None, None, None)
    if sid:
        return (sequence, project.get("sequences"), "sequence") if sequence else (None, None, None)
    return (project, registry["projects"], "project") if project else (None, None, None)


def update_item(vault_root, ctx, code=None, name=None, archived=None, asset_type=None):
    """Rename / re-code / (un)archive the project, sequence, shot or asset that
    ctx points at (its deepest level); assets also take a type.
    Returns (item, error)."""
    registry = load(vault_root)
    item, siblings, level = _locate(registry, ctx)
    if not item:
        return None, "Not found."
    if code is not None:
        code = _clean_code(code)
        err = validate_code(code)
        if err:
            return None, err
        if _code_taken(siblings, code, ignore_id=item["id"]):
            return None, f"Another {level} already uses the code “{code}”."
        item["code"] = code
    if name is not None:
        item["name"] = _clean_name(name)
    if archived is not None:
        item["archived"] = bool(archived)
    if asset_type is not None and level == "asset":
        item["type"] = _clean_type(asset_type)
    utils.touch(item)
    save(vault_root, registry)
    return item, None


def delete_item(vault_root, ctx, used_ids):
    """Delete a project, sequence, shot or asset, only if no workflow is saved
    in it or below it (archive it instead). Returns an error message or None."""
    registry = load(vault_root)
    item, siblings, level = _locate(registry, ctx)
    if not item:
        return "Not found."
    ids = {item["id"]}
    for seq in item.get("sequences") or []:
        ids.add(seq.get("id"))
        ids.update(s.get("id") for s in seq.get("shots") or [])
    ids.update(s.get("id") for s in item.get("shots") or [])
    ids.update(a.get("id") for a in item.get("assets") or [])
    if ids & used_ids:
        return f"This {level} still has workflows saved in it. Move or delete them first, or archive the {level} instead."
    siblings.remove(item)
    save(vault_root, registry)
    return None
