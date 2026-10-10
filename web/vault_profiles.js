// Vaults: named vault folders (e.g. "Work" and "Personal") the user can switch
// between. Each has its own entries, tags, and settings — switching only
// repoints the app at another folder; nothing is copied, moved, or deleted.
// The UI says "vault"; the code, API routes, and vault_config.json keep the
// original "profile" naming so existing installs need no migration.
// This module owns the sidebar switcher and the Settings → Vaults tab.
//
// In studio mode some vaults come from the studio configuration ("locked"):
// shared by every artist, they can be switched to but not renamed, repointed,
// or removed from here.

import { el, showToast, confirmDialog, promptDialog, toggleField } from "./vault_dom.js";
import { VaultAPI } from "./vault_api.js";

export const MAX_PROFILE_NAME_LEN = 60;

export function activeProfile(state) {
  const profiles = state?.profiles || [];
  return profiles.find((p) => p.id === state?.active_profile) || null;
}

// --- Sidebar switcher ------------------------------------------------------

export function renderProfileSwitcher(controller) {
  const { state } = controller;
  const profiles = state.profiles || [];
  const wrap = el("div", { className: "wv-profile-switcher" });

  wrap.appendChild(
    el("div", { className: "wv-sidebar-heading-row" }, [
      el("span", { className: "wv-sidebar-heading", id: "wv-profile-label" }, ["Vault"]),
      el(
        "button",
        {
          className: "wv-icon-btn",
          title: "Manage vaults",
          "aria-label": "Manage vaults",
          onclick: () => controller.openSettings("profiles"),
        },
        [el("i", { className: "pi pi-cog" })]
      ),
    ])
  );

  const select = el(
    "select",
    {
      className: "wv-input wv-profile-select",
      "aria-labelledby": "wv-profile-label",
      title: "Switch vault",
      onchange: (e) => controller.switchProfile(e.target.value),
    },
    profiles.map((p) => el("option", { value: p.id, selected: p.id === state.active_profile, title: p.vault_root }, [p.name]))
  );
  wrap.appendChild(select);
  return wrap;
}

// --- Settings → Profiles tab ----------------------------------------------

function panel(title, icon, hint) {
  const p = el("div", { className: "wv-vs-panel" });
  p.appendChild(el("div", { className: "wv-vs-panel-title" }, [el("i", { className: icon }), el("span", {}, [title])]));
  if (hint) p.appendChild(el("div", { className: "wv-vs-hint" }, [hint]));
  return p;
}

// --- Team sharing (plug and play) --------------------------------------------
//
// A vault becomes a team vault when someone shares it here (or ticks "Shared
// team vault" when choosing its folder): that writes shared_vault.json into
// the folder, and everyone who picks the same folder joins automatically.
// Vaults defined by a managed studio config are shared by the pipeline team
// and only described here.

function sharingModeText(s) {
  return s.mode === "group"
    ? `Only members of the Unix group “${s.group}” can read and change it.`
    : "Everyone who can open this folder can read and change it — no IT setup needed.";
}

async function applySharing(controller, body, success) {
  try {
    await VaultAPI.setSharing(body);
    await controller.refresh();
    showToast(success, "success");
    return true;
  } catch (e) {
    showToast(e.message, "error");
    return false;
  }
}

function renderSharingPanel(controller) {
  const { state } = controller;
  const s = state.studio || {};
  const name = activeProfile(state)?.name || "This vault";
  const p = panel("Team sharing", "pi pi-share-alt", null);
  const line = (text) => p.appendChild(el("p", { className: "wv-radio-desc wv-studio-line" }, [text]));

  if (!s.enabled) {
    line(`“${name}” is private. To work on it with other artists, keep it in a folder on a network drive that everyone can open, then share it.`);
    line("Anyone who then picks the same folder in their vault joins automatically. Nobody needs to set anything up.");
    const btn = el("button", { className: "wv-btn wv-btn-primary", type: "button" }, [el("i", { className: "pi pi-share-alt" }), "Share this vault with my team"]);
    btn.onclick = async () => {
      const ok = await confirmDialog({
        title: `Share “${name}” with your team?`,
        message:
          "Everything in this vault folder becomes readable and changeable by everyone who can open the folder.\n\n" +
          "Saves are recorded with each artist's name, favorites and view settings become personal, and deletes go to a shared trash inside the folder.\n\n" +
          `Other artists join by choosing this same folder in their vault:\n${state.vault_root}`,
        confirmText: "Share vault",
      });
      if (!ok) return;
      btn.disabled = true;
      if (!(await applySharing(controller, { enabled: true }, "Vault shared with your team."))) btn.disabled = false;
    };
    p.appendChild(el("div", { className: "wv-profile-add-actions" }, [btn]));
    return p;
  }

  const role = s.is_admin ? "admin" : "artist";
  if (s.managed) {
    line(`A studio vault, shared and set up by your pipeline team. You're working as ${s.user} (${role}).`);
  } else {
    line(`Shared with your team. You're working as ${s.user} (${role}). ${sharingModeText(s)}`);
    line(`Others join by choosing this same folder in their vault: ${state.vault_root}`);
  }
  line("Saves are recorded with each artist's name. Favorites and view settings are personal. Deletes go to the vault's shared trash.");
  if ((s.admins || []).length) {
    line(`Admins: ${s.admins.join(", ")}. They manage tags, compression, projects and sharing, and can delete anyone's work; everyone else can delete only what they saved.`);
  }
  if (s.managed || !s.is_admin) return p;

  // Admin controls: who's an admin, group restriction, stop sharing.
  const adminsInput = el("input", {
    className: "wv-input",
    type: "text",
    value: (s.admins || []).join(", "),
    placeholder: "Logins, comma-separated — empty: everyone",
    "aria-label": "Vault admins",
  });
  const modeSelect = el("select", { className: "wv-input", "aria-label": "Who can open the vault" }, [
    el("option", { value: "open", selected: s.mode !== "group" || null }, ["Everyone who can open the folder"]),
    el("option", { value: "group", selected: s.mode === "group" || null }, ["Only one Unix group (IT sets it up)"]),
  ]);
  const groupInput = el("input", { className: "wv-input", type: "text", value: s.group || "", placeholder: "Unix group, e.g. comfyusers", "aria-label": "Unix group" });
  const syncGroup = () => (groupInput.style.display = modeSelect.value === "group" ? "" : "none");
  modeSelect.addEventListener("change", syncGroup);
  syncGroup();

  const saveBtn = el("button", { className: "wv-btn", type: "button" }, ["Save sharing settings"]);
  saveBtn.onclick = async () => {
    saveBtn.disabled = true;
    const admins = adminsInput.value.split(/[\s,;]+/).map((a) => a.trim()).filter(Boolean);
    await applySharing(controller, { enabled: true, admins, permissions: modeSelect.value, group: groupInput.value }, "Sharing settings saved.");
    saveBtn.disabled = false;
  };
  const stopBtn = el("button", { className: "wv-btn wv-btn-danger", type: "button" }, ["Stop sharing"]);
  stopBtn.onclick = async () => {
    const ok = await confirmDialog({
      title: "Stop sharing this vault?",
      message: "The vault becomes private again for anyone who opens it. Nothing is deleted, and files stay readable by the people who could already open them.",
      confirmText: "Stop sharing",
      danger: true,
    });
    if (ok) await applySharing(controller, { enabled: false }, "The vault is private again.");
  };

  p.appendChild(el("div", { className: "wv-form-row" }, [el("label", {}, ["Admins"]), adminsInput]));
  p.appendChild(el("div", { className: "wv-form-row" }, [el("label", {}, ["Who can open it"]), modeSelect, groupInput]));
  p.appendChild(el("div", { className: "wv-profile-add-actions wv-sharing-actions" }, [stopBtn, saveBtn]));
  return p;
}

export function renderProfilesSection(controller, { activePanels = [] } = {}) {
  const { state } = controller;
  const profiles = state.profiles || [];
  const managedSetup = !!state.studio?.deployment; // a pipeline-managed studio config exists
  const section = el("div", { className: "wv-profiles" });

  section.appendChild(renderSharingPanel(controller));

  // --- Existing profiles ---
  const listPanel = panel(
    "Your vaults",
    "pi pi-users",
    managedSetup
      ? "Vaults marked Studio are set up by your pipeline team and shared with everyone. Switching only changes which one you're looking at."
      : "Each vault is its own folder with separate entries, tags, and settings — for example one for work and one for personal projects. These changes apply immediately."
  );
  const list = el("div", { className: "wv-profile-list" });
  for (const p of profiles) {
    const isActive = p.id === state.active_profile;
    const row = el("div", { className: `wv-profile-row${isActive ? " wv-profile-row-active" : ""}` });
    row.appendChild(
      el("div", { className: "wv-profile-info" }, [
        el("div", { className: "wv-profile-name" }, [
          p.name,
          ...(p.locked ? [el("span", { className: "wv-profile-badge wv-profile-badge-studio", title: "Shared studio vault, set in the studio configuration" }, ["Studio"])] : []),
          ...(isActive ? [el("span", { className: "wv-profile-badge" }, ["Active"])] : []),
        ]),
        el("div", { className: "wv-profile-path wv-mono", title: p.vault_root }, [p.vault_root]),
      ])
    );
    const actions = el("div", { className: "wv-profile-actions" });
    if (!isActive) {
      actions.appendChild(el("button", { className: "wv-btn", onclick: () => controller.switchProfile(p.id) }, ["Switch to"]));
    }
    row.appendChild(actions);
    list.appendChild(row);
    if (p.locked) continue;
    actions.appendChild(
      el(
        "button",
        {
          className: "wv-icon-btn",
          title: "Rename",
          "aria-label": `Rename vault ${p.name}`,
          onclick: () => renameProfileAction(controller, p),
        },
        [el("i", { className: "pi pi-pencil" })]
      )
    );
    actions.appendChild(
      el(
        "button",
        {
          className: "wv-icon-btn wv-icon-btn-danger",
          title: isActive ? "Switch to another vault to remove this one" : "Remove vault",
          "aria-label": `Remove vault ${p.name}`,
          disabled: isActive,
          onclick: () => removeProfileAction(controller, p),
        },
        [el("i", { className: "pi pi-trash" })]
      )
    );
  }
  listPanel.appendChild(list);
  section.appendChild(listPanel);

  // Per-profile settings (folder, accent color) sit right under the list they
  // describe; the settings view builds them because they share its Save flow.
  for (const p of activePanels) section.appendChild(p);

  if (managedSetup && !state.studio.allow_personal_vaults) return section;

  // --- Add a profile ---
  const addPanel = panel(
    "Add a vault",
    "pi pi-plus",
    "Pick an empty folder to start a fresh vault, or an existing vault folder to reuse it. The new vault becomes active right away."
  );
  const nameInput = el("input", {
    className: "wv-input",
    type: "text",
    placeholder: "Vault name, e.g. Work",
    maxlength: String(MAX_PROFILE_NAME_LEN),
    "aria-label": "Vault name",
  });
  const pathInput = el("input", {
    className: "wv-input wv-mono",
    type: "text",
    placeholder: "Vault folder path, e.g. C:\\Users\\you\\Documents\\Work Vault",
    "aria-label": "Vault folder path",
  });
  const status = el("div", { className: "wv-init-status" });
  const browseBtn = el(
    "button",
    {
      className: "wv-btn",
      title: "Browse for a folder",
      onclick: async () => {
        browseBtn.disabled = true;
        status.textContent = "";
        try {
          const res = await VaultAPI.browseFolder();
          if (res.path) pathInput.value = res.path;
          // res.path === null → the user cancelled the dialog; leave the field.
        } catch {
          // Same fallback as the vault-location picker: the native dialog is
          // absent on some installs (e.g. ComfyUI's portable Windows build).
          status.textContent = "Folder picker isn't available on this ComfyUI install — type or paste the full folder path.";
        } finally {
          browseBtn.disabled = false;
        }
      },
    },
    [el("i", { className: "pi pi-folder-open" }), " Browse…"]
  );
  const addBtn = el("button", { className: "wv-btn wv-btn-primary", onclick: () => submit(false) }, [el("i", { className: "pi pi-plus" }), "Create vault"]);
  // A folder that's already a team vault is joined as one either way.
  const sharedSwitch = toggleField("Shared team vault (a network folder other artists will use too)", false, () => {});

  async function submit(confirm) {
    const name = nameInput.value.trim();
    const vaultRoot = pathInput.value.trim();
    if (!name) {
      status.textContent = "Give the vault a name.";
      nameInput.focus();
      return;
    }
    if (!vaultRoot) {
      status.textContent = "Choose a folder for the vault.";
      pathInput.focus();
      return;
    }
    if (!(await controller.checkDirty())) return;
    addBtn.disabled = true;
    status.textContent = "";
    try {
      await VaultAPI.createProfile({ name, vault_root: vaultRoot, confirm, shared: sharedSwitch.input.checked });
      controller.resetForNewVault();
      await controller.refresh();
      showToast(`Vault “${name}” created and activated.`, "success");
    } catch (e) {
      if (e.status === 409 && e.data?.needs_confirmation) {
        addBtn.disabled = false;
        const ok = await confirmDialog({ title: "Use this folder?", message: e.data.message, confirmText: "Continue" });
        if (ok) await submit(true);
        return;
      }
      status.textContent = e.message;
    } finally {
      addBtn.disabled = false;
    }
  }
  nameInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") pathInput.focus();
  });
  pathInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") submit(false);
  });

  addPanel.appendChild(el("div", { className: "wv-profile-add-name" }, [nameInput]));
  addPanel.appendChild(el("div", { className: "wv-vs-location-row" }, [pathInput, browseBtn]));
  addPanel.appendChild(el("div", { className: "wv-form-row" }, [sharedSwitch]));
  addPanel.appendChild(el("div", { className: "wv-profile-add-actions" }, [addBtn]));
  addPanel.appendChild(status);
  section.appendChild(addPanel);

  return section;
}

async function renameProfileAction(controller, profile) {
  const next = await promptDialog({
    title: "Rename vault",
    defaultValue: profile.name,
    placeholder: "Vault name",
    confirmText: "Rename",
  });
  if (next == null) return;
  const name = next.trim();
  if (!name || name === profile.name) return;
  try {
    await VaultAPI.renameProfile(profile.id, name);
    await controller.refresh();
    showToast("Vault renamed.", "success");
  } catch (e) {
    showToast(e.message, "error");
  }
}

async function removeProfileAction(controller, profile) {
  const ok = await confirmDialog({
    title: `Remove “${profile.name}”?`,
    message:
      "This only removes the vault from the list. Its folder and everything in it stay on disk, " +
      "and you can add the folder back later as a new vault.\n\n" +
      profile.vault_root,
    confirmText: "Remove vault",
    danger: true,
  });
  if (!ok) return;
  try {
    await VaultAPI.deleteProfile(profile.id);
    await controller.refresh();
    showToast(`Vault “${profile.name}” removed.`, "success");
  } catch (e) {
    showToast(e.message, "error");
  }
}
