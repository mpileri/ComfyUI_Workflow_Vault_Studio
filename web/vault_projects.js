// Places: where a workflow lives, and the two sidebar trees that browse them.
//
//   LIBRARY   the studio-wide library, organized in nested folders
//   PROJECTS  project → assets and sequences side by side → shots
//
// The grid shows one place at a time (controller.scope):
//   null                                   the whole Library
//   {folder_id}                            a Library folder (and its subfolders)
//   {project_id, sequence_id, shot_id}     a project, a sequence or a shot
//   {project_id, asset_id}                 an asset
// A shot also lists what's saved in its sequence and project, and an asset
// what's saved in its project ("inherited"). Every place keeps its own copies
// of workflows (Copy to shots…), so tweaking one never changes another.
//
// This module owns the sidebar trees, the breadcrumb and manage menu of a
// place, the structure actions, the location picker used by the save wizard
// and the Details tab, and the copy dialog.

import { el, clear, showToast, confirmDialog, formDialog, openMenu, closeMenu, onActivate } from "./vault_dom.js";
import { VaultAPI } from "./vault_api.js";

const LEVEL_NOUN = { project: "project", sequence: "sequence", shot: "shot", asset: "asset", folder: "folder" };
const ICONS = {
  library: "pi pi-book",
  folder: "pi pi-folder",
  project: "pi pi-briefcase",
  asset: "pi pi-box",
  sequence: "pi pi-images",
  shot: "pi pi-video",
};

export function isVaultAdmin(state) {
  return !state?.studio?.enabled || state.studio.is_admin !== false;
}

// --- Project contexts ---------------------------------------------------------

const ctxKey = (ctx) => [ctx?.project_id || null, ctx?.sequence_id || null, ctx?.shot_id || null, ctx?.asset_id || null];

export function normalizeScope(ctx) {
  const [project_id, sequence_id, shot_id, asset_id] = ctxKey(ctx);
  if (project_id) {
    return asset_id
      ? { project_id, sequence_id: null, shot_id: null, asset_id }
      : { project_id, sequence_id, shot_id: sequence_id ? shot_id : null };
  }
  return ctx?.folder_id ? { folder_id: ctx.folder_id } : null;
}

export function isFolderScope(scope) {
  return !!(scope && !scope.project_id && scope.folder_id);
}

// Compares project locations (a library entry's context is null).
export function sameContext(a, b) {
  const x = ctxKey(a);
  const y = ctxKey(b);
  return x.every((v, i) => v === y[i]);
}

export function findContext(state, ctx) {
  const [pid, sid, shid, aid] = ctxKey(ctx);
  const project = pid ? (state.projects || []).find((p) => p.id === pid) || null : null;
  const sequence = project && sid ? (project.sequences || []).find((s) => s.id === sid) || null : null;
  const shot = sequence && shid ? (sequence.shots || []).find((s) => s.id === shid) || null : null;
  const asset = project && aid ? (project.assets || []).find((a) => a.id === aid) || null : null;
  return { project, sequence, shot, asset };
}

export function contextResolves(state, ctx) {
  const [pid, sid, shid, aid] = ctxKey(ctx);
  if (!pid) return true;
  const { project, sequence, shot, asset } = findContext(state, ctx);
  return !!project && (!sid || !!sequence) && (!shid || !!shot) && (!aid || !!asset);
}

function levelOf(ctx) {
  const [, sid, shid, aid] = ctxKey(ctx);
  return aid ? "asset" : shid ? "shot" : sid ? "sequence" : "project";
}

export function contextCodes(state, ctx) {
  const { project, sequence, shot, asset } = findContext(state, ctx);
  return [project, asset, sequence, shot].filter(Boolean).map((i) => i.code);
}

export function contextLabel(state, ctx) {
  return contextCodes(state, ctx).join(" › ") || "Library";
}

// Parts of generated project names (mirrors entries.py on the server).
export function cleanDiscipline(value) {
  return String(value || "").trim().toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 30);
}

export function cleanVariant(value) {
  const text = String(value || "").trim().toLowerCase();
  const m = text.match(/^v?(\d{1,4})$/);
  if (m) return `v${String(Number(m[1])).padStart(2, "0")}`;
  return text.replace(/[^a-z0-9]+/g, "").slice(0, 10) || "v01";
}

// --- Library folders --------------------------------------------------------

function folderChildren(state, parentId) {
  return (state.folders || [])
    .filter((f) => (f.parent_id || null) === (parentId || null))
    .sort((a, b) => (a.name || "").localeCompare(b.name || ""));
}

// Root → folder, guarding against cycles in a hand-edited folders.json.
export function folderPath(state, folderId) {
  const byId = new Map((state.folders || []).map((f) => [f.id, f]));
  const path = [];
  const seen = new Set();
  let cur = byId.get(folderId);
  while (cur && !seen.has(cur.id)) {
    seen.add(cur.id);
    path.unshift(cur);
    cur = byId.get(cur.parent_id);
  }
  return path;
}

function folderSubtree(state, folderId) {
  const ids = new Set([folderId]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const f of state.folders || []) {
      if (f.parent_id && ids.has(f.parent_id) && !ids.has(f.id)) {
        ids.add(f.id);
        grew = true;
      }
    }
  }
  return ids;
}

function folderExists(state, folderId) {
  return (state.folders || []).some((f) => f.id === folderId);
}

// --- Places -----------------------------------------------------------------

// A workflow whose project location no longer exists falls back to the
// Library, so it can never disappear from view.
export function isLibraryEntry(state, entry) {
  return !entry.context?.project_id || !contextResolves(state, entry.context);
}

export function scopeResolves(state, scope) {
  if (!scope) return true;
  return scope.project_id ? contextResolves(state, scope) : folderExists(state, scope.folder_id);
}

// Is this entry shown when the grid is on `scope`? A folder shows its
// subfolders' workflows too; a project place only its own.
export function entryIsAt(state, entry, scope, subtree = null) {
  if (!scope) return isLibraryEntry(state, entry);
  if (isFolderScope(scope)) {
    return isLibraryEntry(state, entry) && (subtree || folderSubtree(state, scope.folder_id)).has(entry.folder_id);
  }
  return sameContext(entry.context, scope);
}

export function entriesAt(state, scope) {
  const subtree = isFolderScope(scope) ? folderSubtree(state, scope.folder_id) : null;
  return (state.entries || []).filter((e) => entryIsAt(state, e, scope, subtree));
}

// "Library › Comp › Cleanup", "DEMO › SQ010 › SH0010", or "Library".
export function placeLabel(state, scope) {
  if (isFolderScope(scope)) return ["Library", ...folderPath(state, scope.folder_id).map((f) => f.name)].join(" › ");
  return contextLabel(state, scope);
}

// Where a card's workflow lives, when the grid shows it from somewhere else
// (a Library search, a parent folder, a shot's inherited workflows). Null
// when it's right where the grid is.
export function entryPlaceLabel(state, entry, scope) {
  if (!isLibraryEntry(state, entry)) {
    return sameContext(entry.context, scope) ? null : { icon: ICONS.project, text: contextLabel(state, entry.context) };
  }
  if (!entry.folder_id || !folderExists(state, entry.folder_id)) return null;
  if (isFolderScope(scope) && scope.folder_id === entry.folder_id) return null;
  return { icon: ICONS.folder, text: folderPath(state, entry.folder_id).map((f) => f.name).join(" › ") };
}

// The places above a scope, nearest first: a shot sees its sequence, then its
// project; an asset its project. (Folders already include their subfolders.)
export function parentScopes(scope) {
  const [pid, sid, shid, aid] = ctxKey(scope);
  const out = [];
  if (shid) out.push({ project_id: pid, sequence_id: sid, shot_id: null });
  if (sid || aid) out.push({ project_id: pid, sequence_id: null, shot_id: null });
  return out;
}

function countWithin(state, pred) {
  return (state.entries || []).filter((e) => e.context?.project_id && pred(e.context)).length;
}

// --- Sidebar trees ------------------------------------------------------------

function treeOpenSet(controller) {
  controller.ui.treeOpen = controller.ui.treeOpen || new Set();
  return controller.ui.treeOpen;
}

// Open the tree down to a place, so selecting something keeps it in view.
export function revealScope(controller, scope) {
  const open = treeOpenSet(controller);
  if (scope?.project_id) {
    open.add(`p:${scope.project_id}`);
    if (scope.sequence_id) open.add(`s:${scope.sequence_id}`);
  } else if (scope?.folder_id) {
    for (const f of folderPath(controller.state, scope.folder_id)) open.add(`f:${f.id}`);
  }
}

function iconBtn(icon, title, onclick) {
  return el("button", { className: "wv-icon-btn", type: "button", title, "aria-label": title, onclick }, [el("i", { className: icon })]);
}

function treeRow(controller, { key, depth, icon, label, sub, count, active, archived, expandable, forceOpen, title, onSelect }) {
  const open = expandable && (forceOpen || treeOpenSet(controller).has(key));
  const toggle = () => {
    const set = treeOpenSet(controller);
    if (set.has(key)) set.delete(key);
    else set.add(key);
    controller.render();
  };
  const select = () => {
    if (expandable) treeOpenSet(controller).add(key);
    onSelect();
  };
  return el(
    "div",
    {
      className: `wv-folder-row wv-tree-row${active ? " wv-folder-row-active" : ""}${archived ? " wv-scope-archived" : ""}`,
      role: "treeitem",
      "aria-level": String(depth + 1),
      "aria-expanded": expandable ? String(open) : null,
      "aria-selected": String(!!active),
      tabindex: "0",
      title: title || (sub ? `${label} — ${sub}` : label),
      style: { paddingLeft: `${4 + depth * 14}px` },
      onclick: select,
      onkeydown: (e) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === "ArrowRight" && expandable && !open) {
          e.preventDefault();
          toggle();
        } else if (e.key === "ArrowLeft" && expandable && open) {
          e.preventDefault();
          toggle();
        } else {
          onActivate(select)(e);
        }
      },
    },
    [
      expandable
        ? el(
            "button",
            {
              type: "button",
              className: "wv-tree-toggle",
              tabindex: "-1",
              "aria-label": open ? `Collapse ${label}` : `Expand ${label}`,
              onclick: (e) => {
                e.stopPropagation();
                toggle();
              },
            },
            [el("i", { className: `pi ${open ? "pi-chevron-down" : "pi-chevron-right"}` })]
          )
        : el("span", { className: "wv-tree-toggle wv-tree-toggle-spacer" }),
      el("i", { className: `${icon} wv-gentype-icon` }),
      el("span", { className: "wv-folder-label" }, [label, sub ? el("span", { className: "wv-scope-sub" }, [` ${sub}`]) : null]),
      el("span", { className: "wv-folder-count" }, [String(count)]),
    ]
  );
}

export function renderPlacesSidebar(container, controller) {
  renderLibraryTree(container, controller);
  renderProjectTree(container, controller);
}

function renderLibraryTree(container, controller) {
  const { state } = controller;
  const admin = isVaultAdmin(state);
  const actions = el("div", { className: "wv-sidebar-heading-actions" });
  if (admin) actions.appendChild(iconBtn("pi pi-plus", "New library folder", () => newFolderAction(controller, null)));
  container.appendChild(
    el("div", { className: "wv-sidebar-heading-row wv-sidebar-section-gap" }, [el("div", { className: "wv-sidebar-heading" }, ["Library"]), actions])
  );

  const tree = el("div", { className: "wv-folder-tree wv-tree", role: "tree", "aria-label": "Library" });
  tree.appendChild(
    treeRow(controller, {
      key: "library",
      depth: 0,
      icon: ICONS.library,
      label: "All workflows",
      count: entriesAt(state, null).length,
      active: !controller.scope,
      title: "Every workflow in the studio library",
      onSelect: () => controller.setScope(null),
    })
  );
  const addFolders = (parentId, depth) => {
    for (const f of folderChildren(state, parentId)) {
      const hasKids = folderChildren(state, f.id).length > 0;
      tree.appendChild(
        treeRow(controller, {
          key: `f:${f.id}`,
          depth,
          icon: ICONS.folder,
          label: f.name,
          count: entriesAt(state, { folder_id: f.id }).length,
          active: isFolderScope(controller.scope) && controller.scope.folder_id === f.id,
          expandable: hasKids,
          onSelect: () => controller.setScope({ folder_id: f.id }),
        })
      );
      if (hasKids && treeOpenSet(controller).has(`f:${f.id}`)) addFolders(f.id, depth + 1);
    }
  };
  addFolders(null, 1);
  container.appendChild(tree);
}

function renderProjectTree(container, controller) {
  const { state } = controller;
  const admin = isVaultAdmin(state);
  const projects = state.projects || [];

  const actions = el("div", { className: "wv-sidebar-heading-actions" });
  if (admin && state.shotgrid?.available) {
    actions.appendChild(iconBtn("pi pi-sync", "Sync projects, assets, sequences and shots from ShotGrid", () => syncShotGridAction(controller)));
  }
  if (admin) actions.appendChild(iconBtn("pi pi-plus", "New project", () => newProjectAction(controller)));
  container.appendChild(
    el("div", { className: "wv-sidebar-heading-row wv-sidebar-section-gap" }, [el("div", { className: "wv-sidebar-heading" }, ["Projects"]), actions])
  );

  if (!projects.length) {
    const how = state.shotgrid?.available ? "Create one with + or sync from ShotGrid." : "Create one with +.";
    container.appendChild(el("div", { className: "wv-tagfilter-empty" }, [admin ? `No projects yet. ${how}` : "No projects yet."]));
    return;
  }

  // Big productions get a filter box. It redraws only the tree, so typing
  // keeps focus.
  const leafCount = projects.reduce(
    (n, p) => n + (p.assets || []).length + (p.sequences || []).reduce((m, s) => m + (s.shots || []).length, 0),
    0
  );
  const tree = el("div", { className: "wv-folder-tree wv-tree", role: "tree", "aria-label": "Projects" });
  if (leafCount > 20) {
    const filter = el("input", {
      className: "wv-input wv-tagfilter-search",
      type: "text",
      placeholder: "Filter shots and assets…",
      "aria-label": "Filter projects, assets, sequences and shots",
      value: controller.ui.treeQuery || "",
    });
    filter.addEventListener("input", () => {
      controller.ui.treeQuery = filter.value;
      paintProjectTree(tree, controller);
    });
    container.appendChild(el("div", { className: "wv-tagfilter-search-wrap" }, [filter]));
  }
  paintProjectTree(tree, controller);
  container.appendChild(tree);
}

function paintProjectTree(tree, controller) {
  const { state } = controller;
  const scope = controller.scope || {};
  const showArchived = !!controller.filters.showArchived;
  const q = (controller.ui.treeQuery || "").trim().toLowerCase();
  const hit = (item) => !q || (item.code || "").toLowerCase().includes(q) || (item.name || "").toLowerCase().includes(q);
  // Archived items stay hidden unless "Show archived" is on, except the one being viewed.
  const visible = (item) => showArchived || !item.archived || [scope.project_id, scope.sequence_id, scope.shot_id, scope.asset_id].includes(item.id);

  clear(tree);
  let shown = 0;
  for (const p of state.projects || []) {
    if (!visible(p)) continue;
    const projectHit = hit(p);
    const assets = (p.assets || []).filter((a) => visible(a) && (projectHit || hit(a)));
    const sequences = [];
    for (const s of (p.sequences || []).filter(visible)) {
      const seqHit = projectHit || hit(s);
      const shots = (s.shots || []).filter((sh) => visible(sh) && (seqHit || hit(sh)));
      if (seqHit || shots.length) sequences.push({ s, shots });
    }
    if (q && !projectHit && !assets.length && !sequences.length) continue;
    shown++;

    const hasKids = assets.length + sequences.length > 0;
    tree.appendChild(
      treeRow(controller, {
        key: `p:${p.id}`,
        depth: 0,
        icon: ICONS.project,
        label: p.code,
        sub: p.name,
        count: countWithin(state, (c) => c.project_id === p.id),
        active: scope.project_id === p.id && !scope.sequence_id && !scope.asset_id,
        archived: p.archived,
        expandable: hasKids,
        forceOpen: !!q,
        onSelect: () => controller.setScope({ project_id: p.id }),
      })
    );
    if (!hasKids || !(q || treeOpenSet(controller).has(`p:${p.id}`))) continue;

    // Assets and sequences side by side under the project; shots under sequences.
    for (const a of assets) {
      tree.appendChild(
        treeRow(controller, {
          key: `a:${a.id}`,
          depth: 1,
          icon: ICONS.asset,
          label: a.code,
          sub: a.type,
          count: countWithin(state, (c) => c.asset_id === a.id),
          active: scope.asset_id === a.id,
          archived: a.archived,
          title: [a.code, a.type, a.name].filter(Boolean).join(" · "),
          onSelect: () => controller.setScope({ project_id: p.id, asset_id: a.id }),
        })
      );
    }
    for (const { s, shots } of sequences) {
      tree.appendChild(
        treeRow(controller, {
          key: `s:${s.id}`,
          depth: 1,
          icon: ICONS.sequence,
          label: s.code,
          sub: s.name,
          count: countWithin(state, (c) => c.sequence_id === s.id),
          active: scope.sequence_id === s.id && !scope.shot_id,
          archived: s.archived,
          expandable: shots.length > 0,
          forceOpen: !!q,
          onSelect: () => controller.setScope({ project_id: p.id, sequence_id: s.id }),
        })
      );
      if (!shots.length || !(q || treeOpenSet(controller).has(`s:${s.id}`))) continue;
      for (const sh of shots) {
        tree.appendChild(
          treeRow(controller, {
            key: `sh:${sh.id}`,
            depth: 2,
            icon: ICONS.shot,
            label: sh.code,
            count: countWithin(state, (c) => c.shot_id === sh.id),
            active: scope.shot_id === sh.id,
            archived: sh.archived,
            title: [sh.code, sh.name, sh.status ? `ShotGrid status: ${sh.status}` : null].filter(Boolean).join(" · "),
            onSelect: () => controller.setScope({ project_id: p.id, sequence_id: s.id, shot_id: sh.id }),
          })
        );
      }
    }
  }
  if (q && !shown) tree.appendChild(el("div", { className: "wv-tagfilter-empty" }, [`Nothing matches “${q}”.`]));
}

// --- Breadcrumb + manage menu -------------------------------------------------

// Appends "Library › DEMO › SQ010 › SH0010" or "Library › Comp › Cleanup"
// (and, for admins, the manage menu) to a breadcrumb row.
export function appendScopeCrumbs(row, controller) {
  const { state } = controller;
  const scope = controller.scope;
  const crumbs = [{ label: "Library", target: null }];
  let current;
  let level;
  if (isFolderScope(scope)) {
    for (const f of folderPath(state, scope.folder_id)) crumbs.push({ label: f.name, target: { folder_id: f.id } });
    current = crumbs[crumbs.length - 1];
    level = "folder";
  } else {
    const { project, sequence, shot, asset } = findContext(state, scope);
    crumbs.push({ label: project.code, title: project.name, target: { project_id: project.id } });
    if (asset) crumbs.push({ label: asset.code, title: [asset.type, asset.name].filter(Boolean).join(" · "), target: { project_id: project.id, asset_id: asset.id } });
    if (sequence) crumbs.push({ label: sequence.code, title: sequence.name, target: { project_id: project.id, sequence_id: sequence.id } });
    if (shot) crumbs.push({ label: shot.code, title: shot.name, target: { project_id: project.id, sequence_id: sequence.id, shot_id: shot.id } });
    current = shot || sequence || asset || project;
    level = levelOf(scope);
  }

  crumbs.forEach((c, i) => {
    if (i) row.appendChild(el("span", { className: "wv-breadcrumb-sep" }, ["›"]));
    const last = i === crumbs.length - 1;
    row.appendChild(
      last
        ? el("span", { className: "wv-breadcrumb-item wv-breadcrumb-active", title: c.title || null }, [c.label])
        : el("button", { type: "button", className: "wv-breadcrumb-item wv-breadcrumb-link", title: c.title || null, onclick: () => controller.setScope(c.target) }, [c.label])
    );
  });
  if (level === "asset" && current.type) row.appendChild(el("span", { className: "wv-scope-title-name" }, [current.type]));
  else if (level !== "folder" && level !== "project" && current.name) row.appendChild(el("span", { className: "wv-scope-title-name" }, [current.name]));
  if (current.archived) row.appendChild(el("span", { className: "wv-status-badge wv-status-archived" }, ["Archived"]));

  if (isVaultAdmin(state)) {
    const btn = el(
      "button",
      { type: "button", className: "wv-icon-btn", title: `Manage this ${LEVEL_NOUN[level]}`, "aria-label": "Manage", "aria-haspopup": "menu" },
      [el("i", { className: "pi pi-ellipsis-h" })]
    );
    btn.onclick = () => {
      if (btn.getAttribute("aria-expanded") === "true") return closeMenu();
      const items = level === "folder" ? folderMenuItems(controller, scope.folder_id) : projectMenuItems(controller);
      openMenu(btn, items, { container: controller.overlay || document.body, align: "left" });
    };
    row.appendChild(btn);
  }
}

function projectMenuItems(controller) {
  const ctx = controller.scope;
  const level = levelOf(ctx);
  const { project, sequence, shot, asset } = findContext(controller.state, ctx);
  const item = shot || sequence || asset || project;
  const items = [];
  if (level === "project") {
    items.push({ label: "Add assets…", icon: ICONS.asset, onSelect: () => addAssetsAction(controller, project) });
    items.push({ label: "New sequence…", icon: ICONS.sequence, onSelect: () => newSequenceAction(controller, project) });
  }
  if (level === "sequence") items.push({ label: "Add shots…", icon: ICONS.shot, onSelect: () => addShotsAction(controller, project, sequence) });
  items.push({ label: level === "asset" ? "Rename or change type…" : "Rename…", icon: "pi pi-pencil", onSelect: () => renameItemAction(controller, ctx, item, level) });
  items.push(
    item.archived
      ? { label: "Restore", icon: "pi pi-undo", onSelect: () => archiveItemAction(controller, ctx, item, level, false) }
      : { label: "Archive", icon: "pi pi-inbox", onSelect: () => archiveItemAction(controller, ctx, item, level, true) }
  );
  items.push({ divider: true });
  items.push({ label: "Delete…", icon: "pi pi-trash", danger: true, onSelect: () => deleteItemAction(controller, ctx, item, level) });
  return items;
}

function folderMenuItems(controller, folderId) {
  const folder = (controller.state.folders || []).find((f) => f.id === folderId);
  return [
    { label: "New subfolder…", icon: "pi pi-plus", onSelect: () => newFolderAction(controller, folder) },
    { label: "Rename…", icon: "pi pi-pencil", onSelect: () => renameFolderAction(controller, folder) },
    { divider: true },
    { label: "Delete…", icon: "pi pi-trash", danger: true, onSelect: () => deleteFolderAction(controller, folder) },
  ];
}

// --- Structure actions --------------------------------------------------------

async function run(controller, action, success) {
  try {
    const res = await action();
    await controller.refresh();
    if (success) showToast(typeof success === "function" ? success(res) : success, "success");
    return res;
  } catch (e) {
    showToast(e.message, "error");
    return null;
  }
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

async function newFolderAction(controller, parent) {
  const v = await formDialog({
    title: parent ? `New folder in ${parent.name}` : "New library folder",
    message: "Library folders organize the studio's reusable workflows, e.g. Comp, Lookdev, Upscaling.",
    fields: [{ name: "name", label: "Name", placeholder: "e.g. Comp" }],
    confirmText: "Create folder",
  });
  if (!v) return;
  const res = await run(controller, () => VaultAPI.createFolder(v.name, parent?.id), (r) => `Folder “${r.folder.name}” created.`);
  if (res) controller.setScope({ folder_id: res.folder.id });
}

async function renameFolderAction(controller, folder) {
  const v = await formDialog({ title: "Rename folder", fields: [{ name: "name", label: "Name", value: folder.name }], confirmText: "Save" });
  if (!v) return;
  await run(controller, () => VaultAPI.updateFolder(folder.id, { name: v.name }), "Saved.");
}

async function deleteFolderAction(controller, folder) {
  const ok = await confirmDialog({
    title: `Delete “${folder.name}”?`,
    message: "Deletes the folder and its subfolders. The workflows inside them aren't deleted: they move to the Library root.",
    confirmText: "Delete folder",
    danger: true,
  });
  if (!ok) return;
  const res = await run(controller, () => VaultAPI.deleteFolder(folder.id), `Folder “${folder.name}” deleted.`);
  if (res) controller.setScope(folder.parent_id ? { folder_id: folder.parent_id } : null);
}

async function newProjectAction(controller) {
  const v = await formDialog({
    title: "New project",
    message: "The code is what artists see everywhere (e.g. the show's short name).",
    fields: [
      { name: "code", label: "Code", placeholder: "e.g. DEMO" },
      { name: "name", label: "Name (optional)", placeholder: "e.g. Demo Show" },
    ],
    confirmText: "Create project",
  });
  if (!v) return;
  const res = await run(controller, () => VaultAPI.createProject({ code: v.code, name: v.name }), (r) => `Project ${r.item.code} created.`);
  if (res) controller.setScope({ project_id: res.item.id });
}

async function newSequenceAction(controller, project) {
  const v = await formDialog({
    title: `New sequence in ${project.code}`,
    fields: [
      { name: "code", label: "Code", placeholder: "e.g. SQ010" },
      { name: "name", label: "Name (optional)", placeholder: "e.g. Opening" },
    ],
    confirmText: "Create sequence",
  });
  if (!v) return;
  const res = await run(controller, () => VaultAPI.createSequence(project.id, { code: v.code, name: v.name }), (r) => `Sequence ${r.item.code} created.`);
  if (res) controller.setScope({ project_id: project.id, sequence_id: res.item.id });
}

async function addShotsAction(controller, project, sequence) {
  const v = await formDialog({
    title: `Add shots to ${project.code} › ${sequence.code}`,
    message: "Paste shot codes: one per line, or separated by commas or spaces. Shots that already exist are skipped.",
    fields: [{ name: "codes", type: "textarea", label: "Shot codes", placeholder: "SH0010\nSH0020\nSH0030" }],
    confirmText: "Add shots",
  });
  if (!v) return;
  await run(
    controller,
    () => VaultAPI.createShots(project.id, sequence.id, v.codes),
    (r) => `Added ${plural(r.created.length, "shot")}${r.skipped.length ? ` (${r.skipped.length} already existed)` : ""}.`
  );
}

async function addAssetsAction(controller, project) {
  const v = await formDialog({
    title: `Add assets to ${project.code}`,
    message: "Paste asset codes: one per line, or separated by commas or spaces. Assets that already exist are skipped.",
    fields: [
      { name: "codes", type: "textarea", label: "Asset codes", placeholder: "hero_character\nsword_prop\nforest_env" },
      { name: "type", label: "Type (optional, for all of them)", placeholder: "e.g. Character, Prop, Environment" },
    ],
    confirmText: "Add assets",
  });
  if (!v) return;
  await run(
    controller,
    () => VaultAPI.createAssets(project.id, v.codes, v.type),
    (r) => `Added ${plural(r.created.length, "asset")}${r.skipped.length ? ` (${r.skipped.length} already existed)` : ""}.`
  );
}

async function renameItemAction(controller, ctx, item, level) {
  const synced = item.shotgrid_id != null;
  const fields = [
    { name: "code", label: "Code", value: item.code },
    { name: "name", label: "Name (optional)", value: item.name || "" },
  ];
  if (level === "asset") fields.push({ name: "type", label: "Type (optional)", value: item.type || "", placeholder: "e.g. Character, Prop, Environment" });
  const v = await formDialog({
    title: `Rename ${LEVEL_NOUN[level]}`,
    message: synced ? "This comes from ShotGrid: the next sync will put back ShotGrid's values." : "",
    fields,
    confirmText: "Save",
  });
  if (!v) return;
  const body = { ...ctx, code: v.code, name: v.name };
  if (level === "asset") body.type = v.type;
  await run(controller, () => VaultAPI.updateProjectItem(body), "Saved.");
}

async function archiveItemAction(controller, ctx, item, level, archived) {
  if (archived) {
    const ok = await confirmDialog({
      title: `Archive ${item.code}?`,
      message: `The ${LEVEL_NOUN[level]} is hidden unless "Show archived" is on. Its workflows are kept and you can restore it any time.`,
      confirmText: "Archive",
    });
    if (!ok) return;
  }
  const res = await run(controller, () => VaultAPI.updateProjectItem({ ...ctx, archived }), archived ? "Archived." : "Restored.");
  if (res && archived && !controller.filters.showArchived) controller.setScope(parentScopes(ctx)[0] || null);
}

async function deleteItemAction(controller, ctx, item, level) {
  const inside = level === "project" ? " and everything inside it" : level === "sequence" ? " and its shots" : "";
  const ok = await confirmDialog({
    title: `Delete ${item.code}?`,
    message: `Deletes the ${LEVEL_NOUN[level]}${inside}. This only works when no workflows are saved in it; otherwise archive it instead.`,
    confirmText: "Delete",
    danger: true,
  });
  if (!ok) return;
  const res = await run(controller, () => VaultAPI.deleteProjectItem(ctx), `${item.code} deleted.`);
  if (res) controller.setScope(parentScopes(ctx)[0] || null);
}

async function syncShotGridAction(controller) {
  showToast("Syncing from ShotGrid…", "info");
  await run(controller, () => VaultAPI.shotgridSync(), (r) => `ShotGrid sync done: ${r.summary}.`);
}

// --- Location picker (save wizard, Details tab) -------------------------------

// Linked selects: Library or a project, then a Library folder or an asset /
// sequence, then a shot. Takes a scope-like value ({folder_id} or a project
// context). getContext() is the project location (null for the Library) and
// getFolder() the Library folder (null at the root or in a project).
// `restrict`: "library" shows only the folder select, "project" only projects.
export function renderContextPicker(state, value, { onChange, restrict = null } = {}) {
  const wrap = el("div", { className: "wv-context-picker" });
  const placeSel = el("select", { className: "wv-input", "aria-label": "Library or project" });
  const midSel = el("select", { className: "wv-input", "aria-label": "Folder, asset or sequence" });
  const shotSel = el("select", { className: "wv-input", "aria-label": "Shot" });

  const start = findContext(state, value);
  const current = { project_id: "", mid: "", shot_id: "" };
  if (start.project) {
    current.project_id = start.project.id;
    current.mid = start.asset ? `a:${start.asset.id}` : start.sequence ? `s:${start.sequence.id}` : "";
    current.shot_id = start.shot?.id || "";
  } else if (value?.folder_id && folderExists(state, value.folder_id)) {
    current.mid = `f:${value.folder_id}`;
  }
  if (restrict === "library") Object.assign(current, { project_id: "", mid: current.mid.startsWith("f:") ? current.mid : "", shot_id: "" });
  if (restrict === "project" && !current.project_id) {
    const first = (state.projects || []).find((p) => !p.archived);
    Object.assign(current, { project_id: first ? first.id : "", mid: "", shot_id: "" });
  }

  // Archived places can't be picked, except the one already chosen.
  const pickable = (item, selected) => !item.archived || item.id === selected;
  const option = (val, label, selected) => el("option", { value: val, selected: selected || null }, [label]);

  function fill() {
    const project = (state.projects || []).find((p) => p.id === current.project_id) || null;
    placeSel.style.display = restrict === "library" ? "none" : "";
    placeSel.replaceChildren(
      ...(restrict === "project" ? [] : [option("", "Library", !project)]),
      ...(state.projects || [])
        .filter((p) => pickable(p, current.project_id))
        .map((p) => option(p.id, p.name ? `${p.code} — ${p.name}` : p.code, p.id === current.project_id))
    );

    if (!project) {
      // Library folders, indented by depth.
      const opts = [option("", "Library root", !current.mid)];
      const walk = (parentId, depth) => {
        for (const f of folderChildren(state, parentId)) {
          opts.push(option(`f:${f.id}`, `${" ".repeat(depth)}${f.name}`, current.mid === `f:${f.id}`));
          walk(f.id, depth + 1);
        }
      };
      walk(null, 0);
      midSel.replaceChildren(...opts);
      midSel.style.display = opts.length > 1 ? "" : "none";
      shotSel.style.display = "none";
      return;
    }

    const groups = [option("", "Whole project", !current.mid)];
    const assets = (project.assets || []).filter((a) => pickable(a, current.mid.slice(2)));
    if (assets.length) {
      groups.push(el("optgroup", { label: "Assets" }, assets.map((a) => option(`a:${a.id}`, a.type ? `${a.code} (${a.type})` : a.code, current.mid === `a:${a.id}`))));
    }
    const sequences = (project.sequences || []).filter((s) => pickable(s, current.mid.slice(2)));
    if (sequences.length) {
      groups.push(el("optgroup", { label: "Sequences" }, sequences.map((s) => option(`s:${s.id}`, s.code, current.mid === `s:${s.id}`))));
    }
    midSel.replaceChildren(...groups);
    midSel.style.display = "";

    const sequence = current.mid.startsWith("s:") ? sequences.find((s) => s.id === current.mid.slice(2)) : null;
    shotSel.style.display = sequence ? "" : "none";
    shotSel.replaceChildren(
      option("", "Whole sequence", !current.shot_id),
      ...((sequence?.shots || []).filter((s) => pickable(s, current.shot_id)).map((s) => option(s.id, s.code, s.id === current.shot_id)))
    );
  }

  placeSel.addEventListener("change", () => {
    Object.assign(current, { project_id: placeSel.value, mid: "", shot_id: "" });
    fill();
    onChange?.();
  });
  midSel.addEventListener("change", () => {
    Object.assign(current, { mid: midSel.value, shot_id: "" });
    fill();
    onChange?.();
  });
  shotSel.addEventListener("change", () => {
    current.shot_id = shotSel.value;
    onChange?.();
  });
  fill();
  wrap.append(placeSel, midSel, shotSel);

  wrap.getContext = () => {
    if (!current.project_id) return null;
    if (current.mid.startsWith("a:")) return normalizeScope({ project_id: current.project_id, asset_id: current.mid.slice(2) });
    const sequence_id = current.mid.startsWith("s:") ? current.mid.slice(2) : null;
    return normalizeScope({ project_id: current.project_id, sequence_id, shot_id: current.shot_id || null });
  };
  wrap.getFolder = () => (!current.project_id && current.mid.startsWith("f:") ? current.mid.slice(2) : null);
  return wrap;
}

// --- Copy to shots ----------------------------------------------------------------

// Pick any number of places in one project (the project itself, assets,
// sequences, shots) and copy the workflow into each as an independent entry.
export function openCopyToDialog(controller, entry) {
  const { state } = controller;
  const projects = (state.projects || []).filter((p) => !p.archived);
  if (!projects.length) {
    showToast(isVaultAdmin(state) ? "Create a project first (Projects + in the sidebar)." : "There are no projects yet.", "info");
    return;
  }
  const startId = [controller.scope?.project_id, entry.context?.project_id].find((id) => projects.some((p) => p.id === id)) || projects[0].id;

  const overlay = el("div", { className: "wv-overlay wv-overlay-dialog" });
  const nameInput = el("input", { className: "wv-input", type: "text", value: entry.name, "aria-label": "Name of the copies" });
  const projectSel = el(
    "select",
    { className: "wv-input", "aria-label": "Project" },
    projects.map((p) => el("option", { value: p.id, selected: p.id === startId || null }, [p.name ? `${p.code} — ${p.name}` : p.code]))
  );
  const filterInput = el("input", { className: "wv-input", type: "text", placeholder: "Filter shots and assets…", "aria-label": "Filter shots and assets" });
  const tree = el("div", { className: "wv-copyto-tree" });
  const countEl = el("span", { className: "wv-muted" });
  const copyBtn = el("button", { className: "wv-btn wv-btn-primary", type: "button" }, ["Copy"]);
  let boxes = []; // [{ctx, input, label, code, leaf}]

  const sync = () => {
    const n = boxes.filter((b) => b.input.checked).length;
    countEl.textContent = n ? `${plural(n, "place")} selected` : "Nothing selected";
    copyBtn.disabled = !n;
    copyBtn.textContent = n ? `Copy to ${plural(n, "place")}` : "Copy";
  };
  const box = (ctx, labelText, { extraClass = "", leaf = false } = {}) => {
    const input = el("input", { type: "checkbox", className: "wv-checkbox-input" });
    input.addEventListener("change", sync);
    const already = (state.entries || []).some((e) => sameContext(e.context, ctx) && (e.name || "").toLowerCase() === nameInput.value.trim().toLowerCase());
    const label = el("label", { className: `wv-copyto-box ${extraClass}`, title: already ? "Already has a workflow with this name (will be skipped)" : "" }, [
      input,
      el("span", {}, [labelText]),
      already ? el("i", { className: "pi pi-check-circle wv-muted" }) : null,
    ]);
    boxes.push({ ctx, input, label, code: labelText.toLowerCase(), leaf });
    return label;
  };
  const group = (title, items) => {
    const g = el("div", { className: "wv-copyto-seq" });
    const head = el("div", { className: "wv-copyto-seq-head" }, [title]);
    const mine = items.map((it) => box(it.ctx, it.label, { leaf: true }));
    if (mine.length) {
      const boxesHere = boxes.slice(-mine.length);
      head.appendChild(
        el(
          "button",
          {
            type: "button",
            className: "wv-btn-link",
            onclick: () => {
              const all = boxesHere.every((b) => b.input.checked);
              for (const b of boxesHere) b.input.checked = !all;
              sync();
            },
          },
          ["Select all"]
        )
      );
    }
    g.appendChild(head);
    g.appendChild(el("div", { className: "wv-copyto-shots" }, mine));
    return g;
  };

  function paintTree() {
    clear(tree);
    boxes = [];
    const project = projects.find((p) => p.id === projectSel.value);
    tree.appendChild(box({ project_id: project.id }, `${project.code} (whole project)`, { extraClass: "wv-copyto-level" }));
    const assets = (project.assets || []).filter((a) => !a.archived);
    if (assets.length) {
      tree.appendChild(
        group(
          el("span", { className: "wv-copyto-level" }, ["Assets"]),
          assets.map((a) => ({ ctx: { project_id: project.id, asset_id: a.id }, label: a.code }))
        )
      );
    }
    for (const seq of (project.sequences || []).filter((s) => !s.archived)) {
      const shots = (seq.shots || []).filter((s) => !s.archived);
      tree.appendChild(
        group(
          box({ project_id: project.id, sequence_id: seq.id }, `${seq.code} (whole sequence)`, { extraClass: "wv-copyto-level" }),
          shots.map((s) => ({ ctx: { project_id: project.id, sequence_id: seq.id, shot_id: s.id }, label: s.code }))
        )
      );
    }
    applyFilter();
    sync();
  }
  function applyFilter() {
    const q = filterInput.value.trim().toLowerCase();
    for (const b of boxes) if (b.leaf) b.label.style.display = !q || b.code.includes(q) ? "" : "none";
  }
  projectSel.addEventListener("change", paintTree);
  filterInput.addEventListener("input", applyFilter);
  nameInput.addEventListener("change", paintTree);

  const cleanup = () => {
    overlay.remove();
    document.removeEventListener("keydown", onKey);
  };
  const onKey = (e) => {
    if (e.key === "Escape") cleanup();
  };
  copyBtn.addEventListener("click", async () => {
    const targets = boxes.filter((b) => b.input.checked).map((b) => b.ctx);
    const name = nameInput.value.trim();
    if (!name) {
      showToast("Name is required.", "error");
      return;
    }
    copyBtn.disabled = true;
    try {
      const res = await VaultAPI.copyEntryTo(entry.id, targets, name);
      cleanup();
      await controller.refresh();
      const n = res.copied.length;
      showToast(`Copied “${name}” to ${plural(n, "place")}.`, n ? "success" : "warn");
      if (res.skipped.length) {
        const sample = res.skipped.slice(0, 3).map((s) => `${s.where}: ${s.reason}`).join("\n");
        showToast(`Skipped ${res.skipped.length}:\n${sample}${res.skipped.length > 3 ? "\n…" : ""}`, "warn", 10000);
      }
    } catch (e) {
      copyBtn.disabled = false;
      showToast(e.message, "error");
    }
  });

  const dialog = el("div", { className: "wv-dialog wv-copyto-dialog", role: "dialog", "aria-modal": "true", "aria-label": "Copy to shots" }, [
    el("div", { className: "wv-dialog-title" }, [`Copy “${entry.name}” to shots`]),
    el("div", { className: "wv-dialog-body" }, [
      el("p", { className: "wv-muted" }, [
        "Each place gets its own copy with the current version, saved under your name. Changing one copy never changes another.",
      ]),
      el("div", { className: "wv-form-row" }, [el("label", {}, ["Name"]), nameInput]),
      el("div", { className: "wv-form-row wv-copyto-pickers" }, [el("label", {}, ["Project"]), projectSel, filterInput]),
      tree,
    ]),
    el("div", { className: "wv-dialog-footer" }, [countEl, el("div", { className: "wv-topbar-spacer" }), el("button", { className: "wv-btn", type: "button", onclick: cleanup }, ["Cancel"]), copyBtn]),
  ]);
  overlay.appendChild(dialog);
  overlay.addEventListener("mousedown", (e) => {
    if (e.target === overlay) cleanup();
  });
  document.addEventListener("keydown", onKey);
  document.body.appendChild(overlay);
  paintTree();
}
