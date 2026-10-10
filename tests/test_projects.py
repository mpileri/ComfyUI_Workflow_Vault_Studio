"""Tests for projects (project -> sequence -> shot) and the ShotGrid sync.

    python -m unittest discover -s tests -v
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_studio_mode import WORKFLOW, StudioTestCase  # noqa: E402

from workflow_vault import projects, shotgrid  # noqa: E402


class ProjectsTestCase(StudioTestCase):
    admins = ["alice"]

    async def make_structure(self):
        await self.get("/workflow-vault/state")
        body = await self.post("/workflow-vault/projects", {"code": "DEMO", "name": "Demo Show"})
        pid = body["item"]["id"]
        body = await self.post(f"/workflow-vault/projects/{pid}/sequences", {"code": "SQ010"})
        sid = body["item"]["id"]
        body = await self.post(f"/workflow-vault/projects/{pid}/sequences/{sid}/shots",
                               {"codes": "SH0010\nSH0020, SH0030 SH0010"})
        shots = {s["code"]: s["id"] for s in body["created"]}
        self.assertEqual(sorted(shots), ["SH0010", "SH0020", "SH0030"])
        return pid, sid, shots

    async def save_into(self, name, context):
        return await self.post_form("/workflow-vault/entries", {
            "name": name, "workflow": WORKFLOW, "tags": ["comp"], "status": "draft",
            "generation_types": ["image"], "context": context,
        })


class StructureTest(ProjectsTestCase):
    async def test_create_and_list(self):
        pid, sid, shots = await self.make_structure()
        state = await self.get("/workflow-vault/state")
        (project,) = state["projects"]
        self.assertEqual(project["code"], "DEMO")
        self.assertEqual(project["created_by"], "alice")
        self.assertEqual([s["code"] for s in project["sequences"][0]["shots"]], ["SH0010", "SH0020", "SH0030"])
        # Duplicates are refused or skipped, case-insensitively.
        await self.post("/workflow-vault/projects", {"code": "demo"}, status=400)
        body = await self.post(f"/workflow-vault/projects/{pid}/sequences/{sid}/shots", {"codes": "sh0010 SH0040"})
        self.assertEqual(body["skipped"], ["sh0010"])
        self.assertEqual([s["code"] for s in body["created"]], ["SH0040"])

    async def test_only_admins_change_structure(self):
        pid, sid, shots = await self.make_structure()
        self.as_user("bob")
        await self.post("/workflow-vault/projects", {"code": "OTHER"}, status=403)
        await self.post(f"/workflow-vault/projects/{pid}/sequences", {"code": "SQ020"}, status=403)
        await self.post("/workflow-vault/project-items/update", {"project_id": pid, "archived": True}, status=403)
        # ...but artists can save into it.
        await self.save_into("Comp", {"project_id": pid, "sequence_id": sid, "shot_id": shots["SH0010"]})

    async def test_delete_only_when_empty_and_archive(self):
        pid, sid, shots = await self.make_structure()
        await self.save_into("Comp", {"project_id": pid, "sequence_id": sid, "shot_id": shots["SH0010"]})
        await self.post("/workflow-vault/project-items/delete",
                        {"project_id": pid, "sequence_id": sid, "shot_id": shots["SH0010"]}, status=400)
        await self.post("/workflow-vault/project-items/delete", {"project_id": pid}, status=400)
        await self.post("/workflow-vault/project-items/delete",
                        {"project_id": pid, "sequence_id": sid, "shot_id": shots["SH0020"]})
        body = await self.post("/workflow-vault/project-items/update",
                               {"project_id": pid, "sequence_id": sid, "shot_id": shots["SH0010"], "archived": True})
        self.assertTrue(body["item"]["archived"])
        shot_codes = [s["code"] for s in body["projects"][0]["sequences"][0]["shots"]]
        self.assertEqual(shot_codes, ["SH0010", "SH0030"])


class SavingIntoShotsTest(ProjectsTestCase):
    async def test_names_are_unique_per_place(self):
        pid, sid, shots = await self.make_structure()
        sh10 = {"project_id": pid, "sequence_id": sid, "shot_id": shots["SH0010"]}
        sh20 = {"project_id": pid, "sequence_id": sid, "shot_id": shots["SH0020"]}
        a = await self.save_into("Comp Cleanup", sh10)
        b = await self.save_into("Comp Cleanup", sh20)
        await self.save_into("Comp Cleanup", None)  # the library is its own place
        self.assertEqual(a["context"], sh10)
        self.assertEqual(a["slug"], "demo_sq010_sh0010_comp_cleanup")
        self.assertEqual(b["slug"], "demo_sq010_sh0020_comp_cleanup")
        resp = await self.post_form("/workflow-vault/entries", {
            "name": "comp cleanup", "workflow": WORKFLOW, "context": sh10}, status=400)
        self.assertIn("SH0010", resp["error"])

    async def test_bad_location_is_refused(self):
        pid, sid, shots = await self.make_structure()
        await self.post_form("/workflow-vault/entries", {
            "name": "X", "workflow": WORKFLOW, "context": {"project_id": pid, "shot_id": shots["SH0010"]}}, status=400)
        await self.post_form("/workflow-vault/entries", {
            "name": "X", "workflow": WORKFLOW, "context": {"project_id": "project_NOPE"}}, status=400)

    async def test_copy_library_workflow_to_shots(self):
        pid, sid, shots = await self.make_structure()
        master = await self.save_into("Comp Master", None)
        self.as_user("bob")
        targets = [{"project_id": pid, "sequence_id": sid, "shot_id": shots[c]} for c in ("SH0010", "SH0020")]
        targets.append({"project_id": pid, "sequence_id": sid})  # sequence level too
        body = await self.post(f"/workflow-vault/entries/{master['id']}/copy-to", {"targets": targets})
        self.assertEqual(sorted(c["where"] for c in body["copied"]),
                         ["DEMO › SQ010", "DEMO › SQ010 › SH0010", "DEMO › SQ010 › SH0020"])
        self.assertEqual(body["skipped"], [])

        state = await self.get("/workflow-vault/state")
        copies = [e for e in state["entries"] if e["context"]]
        self.assertEqual(len(copies), 3)
        for copy in copies:
            self.assertEqual(copy["name"], "Comp Master")
            self.assertEqual(copy["created_by"], "bob")  # who made the copy
            self.assertEqual(len(copy["versions"]), 1)
            self.assertNotEqual(copy["id"], master["id"])

        # Copies are independent: a new version in one shot doesn't touch another.
        await self.post(f"/workflow-vault/entries/{copies[0]['id']}/versions", {"workflow": WORKFLOW})
        state = await self.get("/workflow-vault/state")
        by_id = {e["id"]: e for e in state["entries"]}
        self.assertEqual(len(by_id[copies[0]["id"]]["versions"]), 2)
        self.assertEqual(len(by_id[copies[1]["id"]]["versions"]), 1)

        # Copying again skips places that already have it.
        body = await self.post(f"/workflow-vault/entries/{master['id']}/copy-to", {"targets": targets[:1]})
        self.assertEqual(body["copied"], [])
        self.assertEqual(len(body["skipped"]), 1)

    async def test_move_between_places(self):
        pid, sid, shots = await self.make_structure()
        entry = await self.save_into("Roto", None)
        sh30 = {"project_id": pid, "sequence_id": sid, "shot_id": shots["SH0030"]}
        state = await self.post_form(f"/workflow-vault/entries/{entry['id']}/metadata", {"context": sh30})
        self.assertEqual(state["context"], sh30)
        self.assertEqual(state["slug"], "demo_sq010_sh0030_roto")
        state = await self.post_form(f"/workflow-vault/entries/{entry['id']}/metadata", {"context": None})
        self.assertIsNone(state["context"])
        self.assertEqual(state["slug"], "roto")
        # A favorite toggle riding along with a move must not swallow the move.
        state = await self.post_form(f"/workflow-vault/entries/{entry['id']}/metadata", {"context": sh30, "favorite": True})
        self.assertEqual(state["context"], sh30)
        self.assertTrue(state["favorite"])


class FakeShotgun:
    """Just enough of shotgun_api3.Shotgun for fetch()."""
    data = {}

    def __init__(self, url, script_name=None, api_key=None):
        assert api_key == "secret"

    def find(self, entity, filters, fields):
        if entity == "Project":
            return self.data["Project"]
        project_id = filters[0][2]["id"]
        return [row for row in self.data[entity] if row.get("_project") == project_id]


SG_DATA = {
    "Project": [{"id": 70, "name": "Demo Show", "tank_name": "DEMO"}],
    "Sequence": [{"id": 1, "code": "SQ010", "description": "Opening", "_project": 70}],
    "Shot": [
        {"id": 11, "code": "SQ010_SH0010", "sg_sequence": {"id": 1}, "sg_status_list": "ip", "_project": 70},
        {"id": 12, "code": "SQ010_SH0020", "sg_sequence": {"id": 1}, "sg_status_list": "omt", "_project": 70},
        {"id": 13, "code": "PICKUP_01", "sg_sequence": None, "sg_status_list": "wtg", "_project": 70},
    ],
}


class ShotGridTest(ProjectsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        FakeShotgun.data = SG_DATA
        sys.modules["shotgun_api3"] = types.SimpleNamespace(Shotgun=FakeShotgun)
        os.environ.update(SHOTGRID_URL="https://example.shotgrid", SHOTGRID_SCRIPT_NAME="vault",
                          SHOTGRID_SCRIPT_KEY="secret")

    async def asyncTearDown(self):
        sys.modules.pop("shotgun_api3", None)
        await super().asyncTearDown()

    def test_merge_is_idempotent_and_links_by_code(self):
        registry = {"projects": [{"id": "project_MANUAL", "code": "demo", "name": "", "sequences": []}]}
        data = shotgrid.fetch()
        counts = shotgrid.merge(registry, data)
        self.assertEqual(counts["project"], {"created": 0, "updated": 1})  # linked, not duplicated
        self.assertEqual(counts["shot"]["created"], 3)
        (project,) = registry["projects"]
        self.assertEqual(project["id"], "project_MANUAL")
        self.assertEqual(project["shotgrid_id"], 70)
        seqs = {s["code"]: s for s in project["sequences"]}
        self.assertEqual(sorted(seqs), [shotgrid.NO_SEQUENCE_CODE, "SQ010"])
        shots = {s["code"]: s for s in seqs["SQ010"]["shots"]}
        self.assertTrue(shots["SQ010_SH0020"]["archived"])  # omitted in ShotGrid
        self.assertFalse(shots["SQ010_SH0010"]["archived"])
        again = shotgrid.merge(registry, data)
        self.assertTrue(all(c == {"created": 0, "updated": 0} for c in again.values()), again)

    async def test_sync_route(self):
        await self.get("/workflow-vault/state")
        state = await self.get("/workflow-vault/state")
        self.assertTrue(state["shotgrid"]["available"], state["shotgrid"])
        body = await self.post("/workflow-vault/shotgrid/sync")
        self.assertIn("3 new", body["summary"])
        self.assertEqual(body["projects"][0]["code"], "DEMO")
        self.as_user("bob")
        await self.post("/workflow-vault/shotgrid/sync", status=403)

    async def test_unconfigured(self):
        os.environ.pop("SHOTGRID_SCRIPT_KEY")
        ok, reason = shotgrid.status()
        self.assertFalse(ok)
        self.assertIn("script key", reason)
        await self.get("/workflow-vault/state")
        await self.post("/workflow-vault/shotgrid/sync", status=400)


class ContextHelpersTest(unittest.TestCase):
    def test_parse_codes(self):
        self.assertEqual(projects.parse_codes("SH010\nSH020, SH030;SH040\tsh010"), ["SH010", "SH020", "SH030", "SH040"])


if __name__ == "__main__":
    unittest.main()
