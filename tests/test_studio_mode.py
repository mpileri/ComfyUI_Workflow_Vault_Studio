"""Integration tests for studio (shared, multi-user) mode.

Runs the real API routes on an aiohttp test server with a stub ComfyUI
`server` module, and plays several artists by switching WORKFLOW_VAULT_USER.
Real multi-user ownership needs several Unix accounts, so these tests check
the mechanism that makes it work: the mode bits and group of everything the
vault writes, plus every multi-user rule.

    pip install aiohttp Pillow
    python -m unittest discover -s tests -v
"""

import asyncio
import grp
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# Stub just enough of ComfyUI for workflow_vault.api to register its routes.
_server = types.ModuleType("server")


class _PromptServer:
    instance = None


_PromptServer.instance = _PromptServer()
_PromptServer.instance.routes = web.RouteTableDef()
_server.PromptServer = _PromptServer
sys.modules.setdefault("server", _server)

from workflow_vault import admin, api, studio, utils  # noqa: E402

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

WORKFLOW = {"last_node_id": 1, "nodes": [{"id": 1, "type": "KSampler"}], "links": []}


def png_bytes(color=(200, 30, 30)):
    if Image is None:
        # A valid 1x1 PNG.
        return bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
            "0000000c4944415408d763f8cfc000000301010018dd8db00000000049454e44ae426082"
        )
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, "PNG")
    return buf.getvalue()


def pick_studio_group(exclude):
    """A group this account belongs to that isn't already the default, so a
    passing test proves the vault actually applied it."""
    for gid in os.getgroups():
        if gid not in exclude:
            return grp.getgrgid(gid).gr_name, gid
    return None, None


class StudioTestCase(unittest.IsolatedAsyncioTestCase):
    admins = []
    allow_personal = False

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wv_studio_test_")
        self.vault = os.path.join(self.tmp, "shared", "vault")
        os.makedirs(self.vault)
        tmp_gid = os.stat(self.tmp).st_gid
        self.group, self.gid = pick_studio_group({os.getgid(), tmp_gid})
        self.config_path = os.path.join(self.tmp, "studio_config.json")
        config = {
            "vaults": [{"id": "main", "name": "Main", "path": self.vault}],
            "admins": self.admins,
            "allow_personal_vaults": self.allow_personal,
        }
        if self.group:
            config["group"] = self.group
        with open(self.config_path, "w") as f:
            json.dump(config, f)

        self._env = dict(os.environ)
        os.environ["WORKFLOW_VAULT_STUDIO_CONFIG"] = self.config_path
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "home_config")
        os.environ.pop("WORKFLOW_VAULT_ROOT", None)
        self.as_user("alice")

        app = web.Application()
        app.add_routes(_PromptServer.instance.routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def as_user(self, name):
        os.environ["WORKFLOW_VAULT_USER"] = name

    # --- request helpers -------------------------------------------------

    async def get(self, path, status=200):
        resp = await self.client.get(path)
        body = await resp.json()
        self.assertEqual(resp.status, status, body)
        return body

    async def post(self, path, payload=None, status=200):
        resp = await self.client.post(path, json=payload or {})
        body = await resp.json()
        self.assertEqual(resp.status, status, body)
        return body

    async def post_form(self, path, data, files=None, status=200):
        form = FormData()
        # A content type forces multipart even with no files, as a browser's FormData does.
        form.add_field("data", json.dumps(data), content_type="application/json")
        for name, (filename, content) in (files or {}).items():
            form.add_field(name, content, filename=filename)
        resp = await self.client.post(path, data=form)
        body = await resp.json()
        self.assertEqual(resp.status, status, body)
        return body

    async def create_entry(self, name="Upscale Flux", favorite=False):
        await self.get("/workflow-vault/state")  # first open initializes the studio vault
        return await self.post_form(
            "/workflow-vault/entries",
            {
                "name": name,
                "workflow": WORKFLOW,
                "tags": ["flux", "upscale"],
                "status": "stable",
                "generation_types": ["image"],
                "favorite": favorite,
                "notes": [{"title": "Notes", "content": "hello"}],
                "examples": [{"title": "Ex", "input_labels": ["in"], "output_labels": ["out"]}],
            },
            files={
                "thumbnail": ("cover.png", png_bytes()),
                "thumbnail_source": ("source.png", png_bytes((10, 10, 200))),
                "example_0_input_0": ("in.png", png_bytes((0, 255, 0))),
                "example_0_output_0": ("out.png", png_bytes((0, 0, 255))),
            },
        )

    def assert_vault_shared(self):
        """Every file the vault wrote is group read/write and every folder
        group rwx + setgid, all in the studio group."""
        cfg = studio.load()
        checked = 0
        for dirpath, dirnames, filenames in os.walk(self.vault):
            for name, is_dir in [(d, True) for d in dirnames] + [(f, False) for f in filenames]:
                path = os.path.join(dirpath, name)
                st = os.lstat(path)
                mode = stat.S_IMODE(st.st_mode)
                rel = os.path.relpath(path, self.vault)
                want = cfg["dir_mode"] if is_dir else cfg["file_mode"]
                self.assertEqual(oct(mode), oct(want), f"{rel} has mode {oct(mode)}")
                if self.gid is not None:
                    self.assertEqual(st.st_gid, self.gid, f"{rel} has gid {st.st_gid}, expected {self.gid}")
                checked += 1
        self.assertGreater(checked, 10)


class PermissionsTest(StudioTestCase):
    async def test_everything_written_is_group_shared(self):
        entry = await self.create_entry()
        self.as_user("bob")
        await self.post(f"/workflow-vault/entries/{entry['id']}/versions",
                        {"workflow": WORKFLOW, "notes": "bob's tweak"})
        await self.post_form(f"/workflow-vault/entries/{entry['id']}/examples", {},
                             files={"output_0": ("more.png", png_bytes((9, 9, 9)))})
        await self.post_form(f"/workflow-vault/entries/{entry['id']}/metadata",
                             {"name": "Upscale Flux v2", "favorite": True})
        await self.post(f"/workflow-vault/entries/{entry['id']}/duplicate", {"name": "Copy"})
        await self.post("/workflow-vault/settings", {"card_size": "small"})
        self.assert_vault_shared()

    async def test_no_file_is_owner_only_outside_studio_mode(self):
        """The upstream bug: mkstemp made every file 0600."""
        os.environ.pop("WORKFLOW_VAULT_STUDIO_CONFIG")
        target = os.path.join(self.tmp, "plain", "manifest.json")
        utils.atomic_write_json(target, {"a": 1})
        mode = stat.S_IMODE(os.stat(target).st_mode)
        self.assertEqual(mode, 0o666 & ~utils._UMASK)
        self.assertTrue(mode & 0o044, "file must be readable by group/others")

    async def test_health_reports_and_admin_fixes_owner_only_files(self):
        entry = await self.create_entry()
        manifest = os.path.join(self.vault, "entries", entry["slug"], "manifest.json")
        os.chmod(manifest, 0o600)  # what upstream wrote
        report = await self.get("/workflow-vault/health")
        perm = [i for i in report["issues"] if i["type"] == "permissions"]
        self.assertTrue(any(i.get("path", "").endswith("manifest.json") for i in perm), report)

        admin.main(["fix-permissions", "--vault", "main"])
        report = await self.get("/workflow-vault/health")
        self.assertTrue(report["ok"], report)


class AuthorshipAndFavoritesTest(StudioTestCase):
    async def test_authors_are_recorded(self):
        entry = await self.create_entry()
        self.assertEqual(entry["created_by"], "alice")
        self.assertEqual(entry["versions"][0]["created_by"], "alice")

        self.as_user("bob")
        state = await self.post(f"/workflow-vault/entries/{entry['id']}/versions", {"workflow": WORKFLOW})
        self.assertEqual(state["created_by"], "alice")
        self.assertEqual(state["updated_by"], "bob")
        self.assertEqual(sorted(v["created_by"] for v in state["versions"]), ["alice", "bob"])

    async def test_favorites_are_per_artist(self):
        entry = await self.create_entry(favorite=True)
        self.assertTrue(entry["favorite"])
        before = entry["updated_at"]

        self.as_user("bob")
        state = await self.get("/workflow-vault/state")
        self.assertFalse(state["entries"][0]["favorite"])
        await self.post_form(f"/workflow-vault/entries/{entry['id']}/metadata", {"favorite": True})

        self.as_user("alice")
        await self.post_form(f"/workflow-vault/entries/{entry['id']}/metadata", {"favorite": False})
        state = await self.get("/workflow-vault/state")
        self.assertFalse(state["entries"][0]["favorite"])
        # Starring is personal: it never touches the shared entry.
        self.assertEqual(state["entries"][0]["updated_at"], before)

        self.as_user("bob")
        state = await self.get("/workflow-vault/state")
        self.assertTrue(state["entries"][0]["favorite"])

    async def test_view_settings_are_personal(self):
        await self.create_entry()
        await self.post("/workflow-vault/settings", {"card_size": "small", "sort": "name"})
        self.as_user("bob")
        state = await self.get("/workflow-vault/state")
        self.assertEqual(state["settings"]["card_size"], "medium")
        self.assertEqual(state["settings"]["sort"], "updated")
        self.as_user("alice")
        state = await self.get("/workflow-vault/state")
        self.assertEqual(state["settings"]["card_size"], "small")
        with open(os.path.join(self.vault, "vault_settings.json")) as f:
            self.assertEqual(json.load(f).get("card_size", "medium"), "medium")

    async def test_stale_edit_is_refused(self):
        entry = await self.create_entry()
        opened_at = entry["updated_at"]
        self.as_user("bob")
        await asyncio.sleep(1.1)  # updated_at has 1 s resolution
        await self.post_form(f"/workflow-vault/entries/{entry['id']}/metadata",
                             {"description": "bob was here", "base_updated_at": opened_at})
        self.as_user("alice")
        body = await self.post_form(f"/workflow-vault/entries/{entry['id']}/metadata",
                                    {"description": "alice overwrites", "base_updated_at": opened_at}, status=409)
        self.assertIn("bob", body["error"])
        state = await self.get("/workflow-vault/state")
        self.assertEqual(state["entries"][0]["description"], "bob was here")


class AdminsAndTrashTest(StudioTestCase):
    admins = ["alice"]

    async def test_only_admins_change_vault_wide_things(self):
        await self.create_entry()
        self.as_user("bob")
        state = await self.get("/workflow-vault/state")
        self.assertFalse(state["studio"]["is_admin"])
        await self.post("/workflow-vault/tags/rename", {"from": "flux", "to": "flux1"}, status=403)
        await self.post("/workflow-vault/tags/delete", {"tag": "flux"}, status=403)
        await self.post("/workflow-vault/settings", {"accent_color": "#ff0000"}, status=403)
        # The settings form re-posts unchanged vault-wide values alongside
        # personal ones; that must still work for artists.
        await self.post("/workflow-vault/settings", {
            "accent_color": state["settings"]["accent_color"],
            "compress_examples_on_upload": state["settings"]["compress_examples_on_upload"],
            "show_archived": True,
        })
        self.as_user("alice")
        await self.post("/workflow-vault/tags/rename", {"from": "flux", "to": "flux1"})

    async def test_artists_delete_only_their_own_work_and_it_goes_to_vault_trash(self):
        alices = await self.create_entry("Alice flow")
        self.as_user("bob")
        bobs = await self.create_entry("Bob flow")
        await self.post(f"/workflow-vault/entries/{alices['id']}/delete", status=403)
        res = await self.post(f"/workflow-vault/entries/{bobs['id']}/delete")
        self.assertEqual(res["method"], "vault_trash")
        self.assertFalse(os.path.exists(os.path.join(self.vault, "entries", "bob_flow")))

        trash = os.path.join(self.vault, ".trash")
        (item,) = os.listdir(trash)
        with open(os.path.join(trash, item, "trash_info.json")) as f:
            info = json.load(f)
        self.assertEqual(info["original_path"], "entries/bob_flow")
        self.assertEqual(info["deleted_by"], "bob")

        admin.main(["trash", "restore", item, "--vault", "main"])
        state = await self.get("/workflow-vault/state")
        self.assertEqual(sorted(e["name"] for e in state["entries"]), ["Alice flow", "Bob flow"])

        self.as_user("alice")
        await self.post(f"/workflow-vault/entries/{bobs['id']}/delete")  # admins may

    async def test_export_skips_trash(self):
        entry = await self.create_entry()
        await self.post(f"/workflow-vault/entries/{entry['id']}/delete")
        await self.create_entry("Kept")
        resp = await self.client.get("/workflow-vault/export")
        self.assertEqual(resp.status, 200)
        import zipfile
        names = zipfile.ZipFile(io.BytesIO(await resp.read())).namelist()
        self.assertTrue(any("entries/kept/manifest.json" in n for n in names))
        self.assertFalse(any("/.trash/" in n or n.endswith(".wv_lock") for n in names), names)


class LockedVaultTest(StudioTestCase):
    async def test_studio_vault_cannot_be_repointed_renamed_or_removed(self):
        state = await self.get("/workflow-vault/state")
        self.assertTrue(state["initialized"])
        self.assertEqual(state["profiles"][0]["id"], "main")
        self.assertTrue(state["profiles"][0]["locked"])
        other = os.path.join(self.tmp, "elsewhere")
        await self.post("/workflow-vault/settings", {"vault_root": other}, status=403)
        self.assertFalse(os.path.exists(other))
        await self.post("/workflow-vault/profiles/main/rename", {"name": "X"}, status=400)
        await self.post("/workflow-vault/profiles/main/delete", status=400)
        await self.post("/workflow-vault/profiles", {"name": "Mine", "vault_root": other}, status=400)

    async def test_missing_share_is_explained(self):
        shutil.rmtree(self.vault)
        state = await self.get("/workflow-vault/state")
        self.assertFalse(state["initialized"])
        self.assertIn("isn't reachable", state["studio_error"])


class CrossProcessLockTest(StudioTestCase):
    async def test_writes_wait_for_another_process(self):
        entry = await self.create_entry()
        holder = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(f"""
                import fcntl, os, sys, time
                fd = os.open({os.path.join(self.vault, '.wv_lock')!r}, os.O_RDWR | os.O_CREAT)
                fcntl.lockf(fd, fcntl.LOCK_EX)
                print("locked", flush=True)
                time.sleep(float(sys.argv[1]))
            """), "30"],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            old = api.VAULT_LOCK_TIMEOUT
            api.VAULT_LOCK_TIMEOUT = 1
            try:
                body = await self.post(f"/workflow-vault/entries/{entry['id']}/versions",
                                       {"workflow": WORKFLOW}, status=503)
                self.assertIn("busy", body["error"])
            finally:
                api.VAULT_LOCK_TIMEOUT = old
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()
        await self.post(f"/workflow-vault/entries/{entry['id']}/versions", {"workflow": WORKFLOW})


class PersonalVaultsTest(StudioTestCase):
    allow_personal = True

    async def test_personal_vault_beside_studio_vault(self):
        await self.get("/workflow-vault/state")
        mine = os.path.join(self.tmp, "mine")
        body = await self.post("/workflow-vault/profiles", {"name": "Mine", "vault_root": mine})
        self.assertEqual(body["active_profile"], body["profile"]["id"])
        state = await self.get("/workflow-vault/state")
        self.assertEqual(os.path.realpath(state["vault_root"]), os.path.realpath(mine))
        # Personal choices live in the artist's home config, not the extension folder.
        self.assertTrue(os.path.isfile(studio.user_config_path()))
        await self.post("/workflow-vault/profiles/main/activate")
        state = await self.get("/workflow-vault/state")
        self.assertEqual(os.path.realpath(state["vault_root"]), os.path.realpath(self.vault))


class SingleUserModeTest(StudioTestCase):
    """No studio config: the original single-user behavior is unchanged."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        os.environ.pop("WORKFLOW_VAULT_STUDIO_CONFIG")
        from workflow_vault import config
        self._config_path = config.EXTENSION_CONFIG_PATH
        config.EXTENSION_CONFIG_PATH = os.path.join(self.tmp, "vault_config.json")
        await self.post("/workflow-vault/settings", {"vault_root": self.vault})

    async def asyncTearDown(self):
        from workflow_vault import config
        config.EXTENSION_CONFIG_PATH = self._config_path
        await super().asyncTearDown()

    async def test_original_behavior(self):
        state = await self.get("/workflow-vault/state")
        self.assertFalse(state["studio"]["enabled"])
        self.assertFalse(state["profiles"][0].get("locked", False))
        entry = await self.create_entry(favorite=True)
        self.assertTrue(entry["favorite"])
        with open(os.path.join(self.vault, "entries", entry["slug"], "manifest.json")) as f:
            self.assertTrue(json.load(f)["favorite"])  # stored in the manifest, as upstream
        await self.post("/workflow-vault/tags/rename", {"from": "flux", "to": "flux1"})
        self.assertFalse(os.path.exists(os.path.join(self.vault, ".users")))
        await self.post("/workflow-vault/profiles/default/rename", {"name": "Work"})


if __name__ == "__main__":
    unittest.main()
