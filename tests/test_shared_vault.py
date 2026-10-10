"""Tests for plug-and-play team sharing: no studio config and no Unix group,
just a vault folder shared from the UI (shared_vault.json).

    python -m unittest discover -s tests -v
"""

import asyncio
import json
import os
import stat
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_studio_mode import WORKFLOW, StudioTestCase, _PromptServer  # noqa: E402

from workflow_vault import config, sharing, studio  # noqa: E402


def mode_of(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


class SharedVaultTestCase(StudioTestCase):
    """No managed studio config: every artist picks the folder themselves."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        os.environ.pop("WORKFLOW_VAULT_STUDIO_CONFIG")
        self._config_path = config.EXTENSION_CONFIG_PATH
        config.EXTENSION_CONFIG_PATH = os.path.join(self.tmp, "vault_config.json")
        sharing._HEALED.clear()

    async def asyncTearDown(self):
        config.EXTENSION_CONFIG_PATH = self._config_path
        await super().asyncTearDown()

    async def share(self, **body):
        return await self.post("/workflow-vault/settings", {"vault_root": self.vault, "shared": True, **body})

    async def save(self, name):
        return await self.post_form("/workflow-vault/entries", {
            "name": name, "workflow": WORKFLOW, "tags": ["t"], "status": "draft", "generation_types": ["image"],
        })

    def assert_open(self):
        """Everything is readable and writable by everyone: no group needed."""
        checked = 0
        for dirpath, dirnames, filenames in os.walk(self.vault):
            for name in dirnames:
                self.assertEqual(oct(mode_of(os.path.join(dirpath, name))), oct(studio.OPEN_DIR_MODE), name)
            for name in filenames:
                self.assertEqual(oct(mode_of(os.path.join(dirpath, name))), oct(studio.OPEN_FILE_MODE), name)
                checked += 1
        self.assertGreater(checked, 3)


class PlugAndPlayTest(SharedVaultTestCase):
    async def test_share_from_ui_and_join(self):
        state = await self.share()
        self.assertTrue(os.path.isfile(os.path.join(self.vault, studio.MARKER_FILENAME)))
        state = await self.get("/workflow-vault/state")
        self.assertTrue(state["studio"]["enabled"])
        self.assertEqual(state["studio"]["mode"], "open")
        self.assertFalse(state["studio"]["managed"])
        entry = await self.save("Flow")

        # Bob just picks the same folder: no checkbox, no config. He joins.
        self.as_user("bob")
        state = await self.post("/workflow-vault/settings", {"vault_root": self.vault})
        state = await self.get("/workflow-vault/state")
        self.assertTrue(state["studio"]["enabled"])
        await self.post(f"/workflow-vault/entries/{entry['id']}/versions", {"workflow": WORKFLOW})
        await self.post_form(f"/workflow-vault/entries/{entry['id']}/metadata", {"favorite": True})
        state = await self.get("/workflow-vault/state")
        self.assertTrue(state["entries"][0]["favorite"])
        self.assertEqual(state["entries"][0]["updated_by"], "bob")
        self.as_user("alice")
        state = await self.get("/workflow-vault/state")
        self.assertFalse(state["entries"][0]["favorite"])  # favorites are per artist
        self.assert_open()

        # Deletes go to the vault's shared trash.
        res = await self.post(f"/workflow-vault/entries/{entry['id']}/delete")
        self.assertEqual(res["method"], "vault_trash")

    async def test_private_vault_is_unchanged(self):
        await self.post("/workflow-vault/settings", {"vault_root": self.vault})
        state = await self.get("/workflow-vault/state")
        self.assertFalse(state["studio"]["enabled"])
        entry = await self.save("Flow")
        manifest = os.path.join(self.vault, "entries", entry["slug"], "manifest.json")
        from workflow_vault import utils
        self.assertEqual(mode_of(manifest), 0o666 & ~utils._UMASK)

    async def test_stop_sharing(self):
        await self.share()
        await self.post("/workflow-vault/sharing", {"enabled": False})
        self.assertFalse(os.path.exists(os.path.join(self.vault, studio.MARKER_FILENAME)))
        state = await self.get("/workflow-vault/state")
        self.assertFalse(state["studio"]["enabled"])


class SelfHealingTest(SharedVaultTestCase):
    async def test_sharing_an_existing_vault_opens_up_old_files(self):
        """A vault made by the single-user extension: owner-only files."""
        await self.post("/workflow-vault/settings", {"vault_root": self.vault})
        entry = await self.save("Old Flow")
        edir = os.path.join(self.vault, "entries", entry["slug"])
        for dirpath, dirnames, filenames in os.walk(edir):
            for d in dirnames:
                os.chmod(os.path.join(dirpath, d), 0o755)
            for f in filenames:
                os.chmod(os.path.join(dirpath, f), 0o600)
        await self.post("/workflow-vault/sharing", {"enabled": True})
        self.assert_open()

    async def test_opening_a_shared_vault_heals_my_files_in_background(self):
        await self.share()
        entry = await self.save("Flow")
        manifest = os.path.join(self.vault, "entries", entry["slug"], "manifest.json")
        os.chmod(manifest, 0o600)
        sharing._HEALED.clear()  # as if ComfyUI had just started
        await self.get("/workflow-vault/state")
        for _ in range(50):
            if mode_of(manifest) == studio.OPEN_FILE_MODE:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(oct(mode_of(manifest)), oct(studio.OPEN_FILE_MODE))

    async def test_health_reports_unshared_files_by_owner_and_fix_my_files(self):
        await self.share()
        entry = await self.save("Flow")
        os.chmod(os.path.join(self.vault, "entries", entry["slug"], "manifest.json"), 0o600)
        report = await self.get("/workflow-vault/health")
        perm = [i for i in report["issues"] if i["type"] == "permissions"]
        self.assertEqual(len(perm), 1, report)
        self.assertIn("manifest.json", perm[0]["path"])
        self.assertTrue(perm[0]["owner"])
        body = await self.post("/workflow-vault/sharing/heal")
        self.assertEqual(body["fixed"], 1)
        self.assertTrue(body["health"]["ok"], body["health"])


class SharingAdminsTest(SharedVaultTestCase):
    async def test_admins_and_no_lockout(self):
        await self.share()
        await self.post("/workflow-vault/sharing", {"enabled": True, "admins": ["bob"]}, status=400)
        await self.post("/workflow-vault/sharing", {"enabled": True, "admins": ["alice"]})
        await self.save("Flow")
        self.as_user("bob")
        state = await self.get("/workflow-vault/state")
        self.assertFalse(state["studio"]["is_admin"])
        await self.post("/workflow-vault/sharing", {"enabled": False}, status=403)
        await self.post("/workflow-vault/tags/rename", {"from": "t", "to": "u"}, status=403)

    async def test_group_mode_needs_a_real_group(self):
        await self.share()
        await self.post("/workflow-vault/sharing",
                        {"enabled": True, "permissions": "group", "group": "no_such_group_wv_test"}, status=400)
        if self.group:
            await self.post("/workflow-vault/sharing", {"enabled": True, "permissions": "group", "group": self.group})
            await self.save("Flow")
            for dirpath, _dirs, files in os.walk(os.path.join(self.vault, "entries")):
                for f in files:
                    st = os.lstat(os.path.join(dirpath, f))
                    self.assertEqual(oct(stat.S_IMODE(st.st_mode)), oct(studio.GROUP_FILE_MODE))
                    self.assertEqual(st.st_gid, self.gid)


class ComfyLoginTest(SharedVaultTestCase):
    """ComfyUI's own user accounts (--multi-user): the browser sends the user
    id in a Comfy-User header and that login is who the artist is."""

    UID = "j-doe_123e4567-e89b-12d3-a456-426614174000"

    async def asyncTearDown(self):
        if hasattr(_PromptServer.instance, "user_manager"):
            del _PromptServer.instance.user_manager
        await super().asyncTearDown()

    async def save_as(self, header, name):
        from aiohttp import FormData
        form = FormData()
        form.add_field("data", json.dumps({"name": name, "workflow": WORKFLOW}), content_type="application/json")
        resp = await self.client.post("/workflow-vault/entries", data=form, headers={"Comfy-User": header})
        body = await resp.json()
        self.assertEqual(resp.status, 200, body)
        return body

    async def test_comfyui_login_names_the_artist(self):
        await self.share()
        _PromptServer.instance.user_manager = types.SimpleNamespace(users={self.UID: "j.doe"})
        entry = await self.save_as(self.UID, "Comp")
        self.assertEqual(entry["created_by"], "j.doe")
        resp = await self.client.get("/workflow-vault/state", headers={"Comfy-User": self.UID})
        state = await resp.json()
        self.assertEqual(state["current_user"], "j.doe")
        self.assertEqual(state["studio"]["user"], "j.doe")
        # Requests without the header are still whoever runs ComfyUI.
        state = await self.get("/workflow-vault/state")
        self.assertEqual(state["current_user"], "alice")

    async def test_falls_back_to_the_id_without_its_uuid(self):
        await self.share()
        entry = await self.save_as("k-roe_123e4567-e89b-12d3-a456-426614174000", "Comp")
        self.assertEqual(entry["created_by"], "k-roe")

    async def test_single_user_comfyui_uses_the_computer_login(self):
        await self.share()
        entry = await self.save_as("default", "Comp")
        self.assertEqual(entry["created_by"], "alice")


if __name__ == "__main__":
    unittest.main()
