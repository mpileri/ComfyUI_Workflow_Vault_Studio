// Projects: the project → sequence → shot hierarchy workflows can be saved
// into, next to the studio-wide Library.
//
// The grid shows one place at a time: controller.scope is null for the
// Library, or {project_id, sequence_id, shot_id} for a project, a sequence or
// a shot. A shot also lists what's saved in its sequence and project
// ("inherited"), and every shot keeps its own copies of workflows (Copy to
// shots…), so tweaking one never changes another.
//
// This module owns the sidebar Projects section, the breadcrumb and children
// strip of a project place, the structure management actions, the location
// picker used by the save wizard and the Details tab, and the copy dialog.

import { el, clear, showToast, confirmDialog, formDialog, openMenu, closeMenu, onActivate } from "./vault_dom.js";
import { VaultAPI } from "./vault_api.js";

const LEVEL_NOUN = { project: "project", sequence: "sequence", shot: "shot" };

export function isVaultAdmin(state) {
  return !state?.studio?.enabled || state.studio.is_admin !== false;
}

// --- Contexts ---------------------------------------------------------------

const ctxKey = (ctx) => [ctx?.project_id || null, ctx?.sequence_id || null, ctx?.shot_id || null];

export function normalizeScope(ctx) {
  const [project_id, sequence_id, shot_id] = ctxKey(ctx);
  return project_id ? { project_id, sequence_id, shot_id: sequence_id ? shot_id : null } : null;
}

export function sameContext(a, b) {
  const x = ctxKey(a);
  const y = ctxKey(b);
  return x[0] === y[0] && x[1] === y[1] && x[2] === y[2];
}

export function findContext(state, ctx) {
  const [pid, sid, shid] = ctxKey(ctx);
  const project = pid ? (state.projects || []).find((p) => p.id === pid) || null : null;
  const sequence = project && sid ? (project.sequences || []).find((s) => s.id === sid) || null : null;
  const shot = sequence && shid ? (sequence.shots || []).find((s) => s.id === shid) || null : null;
  return { project, sequence, shot };
}

export function contextResolves(state, ctx) {
  const [pid, sid, shid] = ctxKey(ctx);
  if (!pid) return true;
  const { project, sequence, shot } = findContext(state, ctx);
  return !!project && (!sid || !!sequence) && (!shid || !!shot);
}

function levelOf(ctx) {
  const [, sid, shid] = ctxKey(ctx);
  return shid ? "shot" : sid ? "sequence" : "project";
}

export function contextLabel(state, ctx) {
  const { project, sequence, shot } = findContext(state, ctx);
  return [project, sequence, shot].filter(Boolean).map((i) => i.code).join(" › ") || "Library";
}

// A workflow whose project location no longer exists falls back to the
// Library, so it can never disappear from view.
export function isLibraryEntry(state, entry) {
  return !entry.context?.project_id || !contextResolves(state, entry.context);
}

export function entriesAt(state, scope) {
  const entries = state.entries || [];
  if (!scope) return entries.filter((e) => isLibraryEntry(state, e));
  return entries.filter((e) => sameContext(e.context, scope));
}

// The places above a scope, nearest first: a shot sees its sequence, then its project.
export function parentScopes(scope) {
  const [pid, sid, shid] = ctxKey(scope);
  const out = [];
  if (shid) out.push({ project_id: pid, sequence_id: sid, shot_id: null });
  if (sid) out.push({ project_id: pid, sequence_id: null, shot_id: null });
  return out;
}

function countWithin(state, pred) {
  return (state.entries || []).filter((e) => e.context?.project_id && pred(e.context)).length;
}

// --- Sidebar ------------------------------------------------------------------

function iconBtn(icon, title, onclick) {
  return el("button", { className: "wv-icon-btn", type: "button", title, "aria-label": title, onclick }, [el("i", { className: icon })]);
}

function sidebarRow({ icon, label, sub, count, active, archived, title, onSelect }) {
  return el(
    "div",
    {
      className: `wv-folder-row wv-scope-row${active ? " wv-folder-row-active" : ""}${archived ? " wv-scope-archived" : ""}`,
      role: "button",
      tabindex: "0",
      title: title || (sub ? `${label} — ${sub}` : label),
      onclick: onSelect,
      onkeydown: onActivate(onSelect),
    },
    [
      el("i", { className: `${icon} wv-gentype-icon` }),
      el("span", { className: "wv-folder-label" }, [label, sub ? el("span", { className: "wv-scope-sub" }, [` ${sub}`]) : null]),
      el("span", { className: "wv-folder-count" }, [String(count)]),
    ]
  );
}

export function renderProjectsSidebar(container, controller) {
  const { state } = controller;
  const admin = isVaultAdmin(state);
  const projects = state.projects || [];

  const actions = el("div", { className: "wv-sidebar-heading-actions" });
  if (admin && state.shotgrid?.available) {
    actions.appendChild(iconBtn("pi pi-sync", "Sync projects, sequences and shots from ShotGrid", () => syncShotGridAction(controller)));
  }
  if (admin) actions.appendChild(iconBtn("pi pi-plus", "New project", () => newProjectAction(controller)));
  container.appendChild(
    el("div", { className: "wv-sidebar-heading-row wv-sidebar-section-gap" }, [el("div", { className: "wv-sidebar-heading" }, ["Projects"]), actions])
  );

  const list = el("div", { className: "wv-folder-tree wv-scope-list" });
  list.appendChild(
    sidebarRow({
      icon: "pi pi-book",
      label: "Library",
      count: entriesAt(state, null).length,
      active: !controller.scope,
      title: "Studio-wide workflows, not tied to a project",
      onSelect: () => controller.setScope(null),
    })
  );
  const showArchived = !!controller.filters.showArchived;
  for (const p of projects) {
    if (p.archived && !showArchived) continue;
    list.appendChild(
      sidebarRow({
        icon: "pi pi-video",
        label: p.code,
        sub: p.name,
        count: countWithin(state, (c) => c.project_id === p.id),
        active: controller.scope?.project_id === p.id,
        archived: p.archived,
        onSelect: () => controller.setScope({ project_id: p.id }),
      })
    );
  }
  container.appendChild(list);
  if (!projects.length) {
    const how = state.shotgrid?.available ? "Create one with + or sync from ShotGrid." : "Create one with +.";
    container.appendChild(el("div", { className: "wv-tagfilter-empty" }, [admin ? `No projects yet. ${how}` : "No projects yet."]));
  }
}

// --- Breadcrumb + children strip for a project place ---------------------------

// Appends "Library › DEMO › SQ010 › SH0010" (and the manage menu) to a
// breadcrumb row. The last crumb is the current place.
export function appendScopeCrumbs(row, controller) {
  const { state } = controller;
  const { project, sequence, shot } = findContext(state, controller.scope);
  const crumbs = [
    { label: "Library", target: null },
    { label: project.code, title: project.name, target: { project_id: project.id } },
  ];
  if (sequence) crumbs.push({ label: sequence.code, title: sequence.name, target: { project_id: project.id, sequence_id: sequence.id } });
  if (shot) crumbs.push({ label: shot.code, title: shot.name, target: { project_id: project.id, sequence_id: sequence.id, shot_id: shot.id } });

  crumbs.forEach((c, i) => {
    if (i) row.appendChild(el("span", { className: "wv-breadcrumb-sep" }, ["›"]));
    const last = i === crumbs.length - 1;
    row.appendChild(
      last
        ? el("span", { className: "wv-breadcrumb-item wv-breadcrumb-active", title: c.title || null }, [c.label])
        : el("button", { type: "button", className: "wv-breadcrumb-item wv-breadcrumb-link", title: c.title || null, onclick: () => controller.setScope(c.target) }, [c.label])
    );
  });
  const current = shot || sequence || project;
  if (current.name && current !== project) row.appendChild(el("span", { className: "wv-scope-title-name" }, [current.name]));
  if (current.archived) row.appendChild(el("span", { className: "wv-status-badge wv-status-archived" }, ["Archived"]));

  if (isVaultAdmin(state)) {
    const btn = el(
      "button",
      { type: "button", className: "wv-icon-btn", title: `Manage this ${levelOf(controller.scope)}`, "aria-label": "Manage", "aria-haspopup": "menu" },
      [el("i", { className: "pi pi-ellipsis-h" })]
    );
    btn.onclick = () => {
      if (btn.getAttribute("aria-expanded") === "true") return closeMenu();
      openMenu(btn, manageMenuItems(controller, { project, sequence, shot }), { container: controller.overlay || document.body, align: "left" });
    };
    row.appendChild(btn);
  }
}

function manageMenuItems(controller, { project, sequence, shot }) {
  const ctx = controller.scope;
  const level = levelOf(ctx);
  const item = shot || sequence || project;
  const items = [];
  if (level === "project") items.push({ label: "New sequence…", icon: "pi pi-plus", onSelect: () => newSequenceAction(controller, project) });
  if (level === "sequence") items.push({ label: "Add shots…", icon: "pi pi-plus", onSelect: () => addShotsAction(controller, project, sequence) });
  items.push({ label: "Rename…", icon: "pi pi-pencil", onSelect: () => renameItemAction(controller, ctx, item, level) });
  items.push(
    item.archived
      ? { label: "Restore", icon: "pi pi-undo", onSelect: () => archiveItemAction(controller, ctx, item, level, false) }
      : { label: "Archive", icon: "pi pi-inbox", onSelect: () => archiveItemAction(controller, ctx, item, level, true) }
  );
  items.push({ divider: true });
  items.push({ label: "Delete…", icon: "pi pi-trash", danger: true, onSelect: () => deleteItemAction(controller, ctx, item, level) });
  return items;
}

// Sequences of a project, or shots of a sequence, as clickable chips with the
// number of workflows saved in (and below) each.
export function renderScopeChildren(controller) {
  const { state } = controller;
  const scope = controller.scope;
  const { project, sequence, shot } = findContext(state, scope);
  if (shot) return null;
  const admin = isVaultAdmin(state);
  const showArchived = !!controller.filters.showArchived;

  const atProject = !sequence;
  const children = ((atProject ? project.sequences : sequence.shots) || []).filter((c) => showArchived || !c.archived);
  const wrap = el("div", { className: "wv-scope-children" });
  const head = el("div", { className: "wv-scope-children-head" }, [el("span", { className: "wv-sidebar-heading" }, [atProject ? "Sequences" : "Shots"])]);
  wrap.appendChild(head);

  const chips = el("div", { className: "wv-scope-chips" });
  const chipRefs = [];
  for (const child of children) {
    const target = atProject ? { project_id: project.id, sequence_id: child.id } : { project_id: project.id, sequence_id: sequence.id, shot_id: child.id };
    const count = atProject
      ? countWithin(state, (c) => c.sequence_id === child.id)
      : countWithin(state, (c) => c.shot_id === child.id);
    const chip = el(
      "button",
      {
        type: "button",
        className: `wv-scope-chip${child.archived ? " wv-scope-archived" : ""}${count ? "" : " wv-scope-chip-empty"}`,
        title: [child.name, child.status ? `ShotGrid status: ${child.status}` : null].filter(Boolean).join(" · ") || child.code,
        onclick: () => controller.setScope(target),
      },
      [el("span", {}, [child.code]), el("span", { className: "wv-scope-chip-count" }, [String(count)])]
    );
    chipRefs.push({ code: child.code.toLowerCase(), chip });
    chips.appendChild(chip);
  }
  if (admin) {
    chips.appendChild(
      el(
        "button",
        {
          type: "button",
          className: "wv-scope-chip wv-scope-chip-add",
          onclick: () => (atProject ? newSequenceAction(controller, project) : addShotsAction(controller, project, sequence)),
        },
        [el("i", { className: "pi pi-plus" }), atProject ? "Sequence" : "Shots"]
      )
    );
  }

  // Long shot lists get a filter box (no re-render, so typing keeps focus).
  if (children.length > 16) {
    const query = el("input", { className: "wv-input wv-scope-filter", type: "text", placeholder: `Filter ${atProject ? "sequences" : "shots"}…`, value: controller.ui.scopeChildQuery || "" });
    const apply = () => {
      const q = (controller.ui.scopeChildQuery || "").trim().toLowerCase();
      for (const { code, chip } of chipRefs) chip.style.display = !q || code.includes(q) ? "" : "none";
    };
    query.addEventListener("input", () => {
      controller.ui.scopeChildQuery = query.value;
      apply();
    });
    head.appendChild(query);
    apply();
  }
  if (!children.length && !admin) {
    chips.appendChild(el("span", { className: "wv-muted" }, [atProject ? "No sequences yet." : "No shots yet."]));
  }
  wrap.appendChild(chips);
  return wrap;
}

// --- Structure management ---------------------------------------------------

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
    (r) => `Added ${r.created.length} shot${r.created.length === 1 ? "" : "s"}${r.skipped.length ? ` (${r.skipped.length} already existed)` : ""}.`
  );
}

async function renameItemAction(controller, ctx, item, level) {
  const synced = item.shotgrid_id != null;
  const v = await formDialog({
    title: `Rename ${LEVEL_NOUN[level]}`,
    message: synced ? "This comes from ShotGrid: the next sync will put back ShotGrid's code and name." : "",
    fields: [
      { name: "code", label: "Code", value: item.code },
      { name: "name", label: "Name (optional)", value: item.name || "" },
    ],
    confirmText: "Save",
  });
  if (!v) return;
  await run(controller, () => VaultAPI.updateProjectItem({ ...ctx, code: v.code, name: v.name }), "Saved.");
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
  const ok = await confirmDialog({
    title: `Delete ${item.code}?`,
    message: `Deletes the ${LEVEL_NOUN[level]}${level === "shot" ? "" : " and everything inside it"}. This only works when no workflows are saved in it; otherwise archive it instead.`,
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

// --- Location picker (save wizard, Details tab) ---------------------------------

// Three linked selects: project (or the Library), sequence (or the whole
// project), shot (or the whole sequence). getContext() returns the choice.
export function renderContextPicker(state, value, { onChange } = {}) {
  const wrap = el("div", { className: "wv-context-picker" });
  const projectSel = el("select", { className: "wv-input", "aria-label": "Project" });
  const seqSel = el("select", { className: "wv-input", "aria-label": "Sequence" });
  const shotSel = el("select", { className: "wv-input", "aria-label": "Shot" });
  const start = findContext(state, value);
  const current = {
    project_id: start.project?.id || "",
    sequence_id: start.sequence?.id || "",
    shot_id: start.shot?.id || "",
  };
  // Archived places can't be picked, except the one already chosen.
  const pickable = (item, selectedId) => !item.archived || item.id === selectedId;
  const option = (val, label, selected) => el("option", { value: val, selected: selected || null }, [label]);

  function fill() {
    const { project, sequence } = findContext(state, current);
    projectSel.replaceChildren(
      option("", "Library (no project)", !current.project_id),
      ...(state.projects || [])
        .filter((p) => pickable(p, current.project_id))
        .map((p) => option(p.id, p.name ? `${p.code} — ${p.name}` : p.code, p.id === current.project_id))
    );
    seqSel.style.display = project ? "" : "none";
    seqSel.replaceChildren(
      option("", "Whole project", !current.sequence_id),
      ...((project?.sequences || []).filter((s) => pickable(s, current.sequence_id)).map((s) => option(s.id, s.code, s.id === current.sequence_id)))
    );
    shotSel.style.display = sequence ? "" : "none";
    shotSel.replaceChildren(
      option("", "Whole sequence", !current.shot_id),
      ...((sequence?.shots || []).filter((s) => pickable(s, current.shot_id)).map((s) => option(s.id, s.code, s.id === current.shot_id)))
    );
  }
  projectSel.addEventListener("change", () => {
    Object.assign(current, { project_id: projectSel.value, sequence_id: "", shot_id: "" });
    fill();
    onChange?.();
  });
  seqSel.addEventListener("change", () => {
    Object.assign(current, { sequence_id: seqSel.value, shot_id: "" });
    fill();
    onChange?.();
  });
  shotSel.addEventListener("change", () => {
    current.shot_id = shotSel.value;
    onChange?.();
  });
  fill();
  wrap.append(projectSel, seqSel, shotSel);
  wrap.getContext = () => normalizeScope(current);
  return wrap;
}

// --- Copy to shots ----------------------------------------------------------------

// Pick any number of places in one project (the project itself, sequences,
// shots) and copy the workflow into each as an independent entry.
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
  const filterInput = el("input", { className: "wv-input", type: "text", placeholder: "Filter shots…", "aria-label": "Filter shots" });
  const tree = el("div", { className: "wv-copyto-tree" });
  const countEl = el("span", { className: "wv-muted" });
  const copyBtn = el("button", { className: "wv-btn wv-btn-primary", type: "button" }, ["Copy"]);
  let boxes = []; // [{ctx, input, label}]

  const sync = () => {
    const n = boxes.filter((b) => b.input.checked).length;
    countEl.textContent = n ? `${n} place${n === 1 ? "" : "s"} selected` : "Nothing selected";
    copyBtn.disabled = !n;
    copyBtn.textContent = n ? `Copy to ${n} place${n === 1 ? "" : "s"}` : "Copy";
  };
  const box = (ctx, labelText, extraClass = "") => {
    const input = el("input", { type: "checkbox", className: "wv-checkbox-input" });
    input.addEventListener("change", sync);
    const already = (state.entries || []).some((e) => sameContext(e.context, ctx) && (e.name || "").toLowerCase() === nameInput.value.trim().toLowerCase());
    const label = el("label", { className: `wv-copyto-box ${extraClass}`, title: already ? "Already has a workflow with this name (will be skipped)" : "" }, [input, el("span", {}, [labelText]), already ? el("i", { className: "pi pi-check-circle wv-muted" }) : null]);
    boxes.push({ ctx, input, label, code: labelText.toLowerCase() });
    return label;
  };

  function paintTree() {
    clear(tree);
    boxes = [];
    const project = projects.find((p) => p.id === projectSel.value);
    tree.appendChild(box({ project_id: project.id }, `${project.code} (whole project)`, "wv-copyto-level"));
    for (const seq of (project.sequences || []).filter((s) => !s.archived)) {
      const shots = (seq.shots || []).filter((s) => !s.archived);
      const group = el("div", { className: "wv-copyto-seq" });
      const seqRow = el("div", { className: "wv-copyto-seq-head" }, [box({ project_id: project.id, sequence_id: seq.id }, `${seq.code} (whole sequence)`, "wv-copyto-level")]);
      const shotBoxes = el("div", { className: "wv-copyto-shots" }, shots.map((s) => box({ project_id: project.id, sequence_id: seq.id, shot_id: s.id }, s.code)));
      if (shots.length) {
        const mine = boxes.slice(-shots.length);
        seqRow.appendChild(
          el("button", {
            type: "button",
            className: "wv-btn-link",
            onclick: () => {
              const all = mine.every((b) => b.input.checked);
              for (const b of mine) b.input.checked = !all;
              sync();
            },
          }, ["All shots"])
        );
      }
      group.appendChild(seqRow);
      group.appendChild(shotBoxes);
      tree.appendChild(group);
    }
    applyFilter();
    sync();
  }
  function applyFilter() {
    const q = filterInput.value.trim().toLowerCase();
    for (const b of boxes) if (b.ctx.shot_id) b.label.style.display = !q || b.code.includes(q) ? "" : "none";
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
      showToast(`Copied “${name}” to ${n} place${n === 1 ? "" : "s"}.`, n ? "success" : "warn");
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
