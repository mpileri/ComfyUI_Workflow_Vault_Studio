"""Sync projects, sequences and shots from ShotGrid (Flow Production Tracking).

Configuration, in the studio config under "shotgrid" (or environment):

    "shotgrid": {
        "url": "https://yourstudio.shotgrid.autodesk.com",   # or $SHOTGRID_URL
        "script_name": "comfyui_vault",                       # or $SHOTGRID_SCRIPT_NAME
        "script_key_file": "/etc/comfyui/shotgrid_key",       # or $SHOTGRID_SCRIPT_KEY
        "projects": ["DEMO", "OTHER"]                         # optional: only these
    }

The script key is a password: keep it out of studio_config.json (which every
artist can read) and in $SHOTGRID_SCRIPT_KEY or a file only the pipeline
account can read. Needs the `shotgun_api3` package (pip install shotgun-api3).

The sync only adds and updates. Items linked to ShotGrid (by `shotgrid_id`)
follow ShotGrid's code and name, and omitted shots ("omt") are archived;
nothing is ever deleted, since a shot may already hold saved workflows.
Hand-made items with the same code are linked rather than duplicated.
"""

import importlib.util
import os
import sys

from . import projects, studio, utils

NO_SEQUENCE_CODE = "NO_SEQUENCE"
OMITTED_STATUSES = {"omt"}


def settings():
    cfg = (studio.deployment() or {}).get("shotgrid") or {}
    key = os.environ.get("SHOTGRID_SCRIPT_KEY") or ""
    key_file = cfg.get("script_key_file")
    if not key and key_file:
        try:
            with open(os.path.expanduser(key_file), "r", encoding="utf-8") as f:
                key = f.read().strip()
        except OSError:
            key = ""
    names = cfg.get("projects")
    return {
        "url": (os.environ.get("SHOTGRID_URL") or cfg.get("url") or "").strip(),
        "script_name": (os.environ.get("SHOTGRID_SCRIPT_NAME") or cfg.get("script_name") or "").strip(),
        "script_key": key,
        "projects": [str(n) for n in names] if isinstance(names, list) else [],
    }


def _installed():
    if "shotgun_api3" in sys.modules:
        return True
    try:
        return importlib.util.find_spec("shotgun_api3") is not None
    except (ImportError, ValueError):
        return False


def status():
    """(available, reason). Cheap: no network."""
    s = settings()
    if not s["url"] or not s["script_name"]:
        return False, "ShotGrid isn't configured (set shotgrid.url and shotgrid.script_name in the studio config)."
    if not s["script_key"]:
        return False, ("No ShotGrid script key on this machine "
                       "(set $SHOTGRID_SCRIPT_KEY, or shotgrid.script_key_file in the studio config).")
    if not _installed():
        return False, "The shotgun_api3 package isn't installed in this Python (pip install shotgun-api3)."
    return True, None


def fetch():
    """Read projects, sequences and shots from ShotGrid. Network; call off the
    event loop. Returns a list of {project, sequences, shots} dicts."""
    import shotgun_api3

    s = settings()
    sg = shotgun_api3.Shotgun(s["url"], script_name=s["script_name"], api_key=s["script_key"])
    filters = [["is_template", "is", False], ["archived", "is", False]]
    if s["projects"]:
        filters.append({
            "filter_operator": "any",
            "filters": [["name", "in", s["projects"]], ["tank_name", "in", s["projects"]]],
        })
    sg_projects = sg.find("Project", filters, ["id", "name", "tank_name"])
    result = []
    for p in sg_projects:
        link = {"type": "Project", "id": p["id"]}
        result.append({
            "project": p,
            "sequences": sg.find("Sequence", [["project", "is", link]], ["id", "code", "description"]),
            "shots": sg.find("Shot", [["project", "is", link]], ["id", "code", "description", "sg_sequence", "sg_status_list"]),
        })
    return result


def _match(items, sg_id, code):
    """The item linked to this ShotGrid id, else an unlinked one with the same code."""
    for item in items:
        if item.get("shotgrid_id") == sg_id:
            return item
    wanted = (code or "").casefold()
    for item in items:
        if not item.get("shotgrid_id") and (item.get("code") or "").casefold() == wanted:
            return item
    return None


def _apply(items, sg_id, code, name, prefix, stats, extra=None):
    code = " ".join(str(code or "").split())[:projects.MAX_CODE_LEN] or f"{prefix}_{sg_id}"
    name = " ".join(str(name or "").split())[:projects.MAX_NAME_LEN]
    changes = {"code": code, "name": name, "shotgrid_id": sg_id, "source": "shotgrid", **(extra or {})}
    item = _match(items, sg_id, code)
    if item is None:
        item = {"id": utils.generate_id(prefix), "archived": False, **changes}
        utils.stamp_created(item)
        items.append(item)
        stats["created"] += 1
    elif any(item.get(k) != v for k, v in changes.items()):
        item.update(changes)
        stats["updated"] += 1
    return item


def merge(registry, data):
    """Fold fetched ShotGrid data into the registry, in place. Pure (no I/O)
    so it can be tested without ShotGrid. Returns counts per level."""
    counts = {level: {"created": 0, "updated": 0} for level in projects.LEVELS}
    for block in data:
        p = block["project"]
        project = _apply(registry["projects"], p["id"], p.get("tank_name") or p.get("name"), p.get("name"),
                         "project", counts["project"])
        sequences = project.setdefault("sequences", [])
        by_sg_id = {}
        for s in block.get("sequences") or []:
            seq = _apply(sequences, s["id"], s.get("code"), s.get("description"), "seq", counts["sequence"])
            seq.setdefault("shots", [])
            by_sg_id[s["id"]] = seq
        for sh in block.get("shots") or []:
            parent = by_sg_id.get((sh.get("sg_sequence") or {}).get("id"))
            if parent is None:
                parent = next((s for s in sequences if s.get("code") == NO_SEQUENCE_CODE), None)
                if parent is None:
                    parent = {"id": utils.generate_id("seq"), "code": NO_SEQUENCE_CODE,
                              "name": "Shots without a sequence in ShotGrid", "archived": False, "shots": []}
                    utils.stamp_created(parent)
                    sequences.append(parent)
                    counts["sequence"]["created"] += 1
            status_code = sh.get("sg_status_list")
            _apply(parent.setdefault("shots", []), sh["id"], sh.get("code"), sh.get("description"), "shot",
                   counts["shot"], extra={"status": status_code, "archived": status_code in OMITTED_STATUSES})
    return counts


def summary(counts):
    parts = []
    for level, label in (("project", "project"), ("sequence", "sequence"), ("shot", "shot")):
        c = counts[level]
        if c["created"] or c["updated"]:
            parts.append(f"{label}s: {c['created']} new, {c['updated']} updated")
    return "; ".join(parts) or "everything was already up to date"


def sync(vault_root, data=None):
    """Fetch (unless `data` is given) and merge into the vault's registry.
    The caller holds the vault write lock around this when it matters."""
    if data is None:
        data = fetch()
    registry = projects.load(vault_root)
    counts = merge(registry, data)
    projects.save(vault_root, registry)
    return counts
