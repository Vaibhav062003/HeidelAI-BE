from pathlib import Path
import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
import runpy
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import tempfile
import unittest

from scripts import provision_customer_orgs as provision
from scripts.provision_customer_orgs import load_rows, slugify


def _csv(count: int = 18) -> str:
    rows = ["workspace_name,sales_email_1,sales_email_2"]
    rows.extend(f"Customer - Branch {index:02d},sales{index:02d}@example.com," for index in range(1, count + 1))
    return "\n".join(rows)


class WorkspaceGroupTests(unittest.TestCase):
    def test_customer_workspace_csv_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workspaces.csv"
            path.write_text(_csv(), encoding="utf-8")
            rows = load_rows(path, "owner@example.com", "hr@example.com")
            self.assertEqual(len(rows), 18)
            self.assertEqual(rows[0].slug, "customer-branch-01")
            self.assertEqual(slugify("Customer – München"), "customer-munchen")

            path.write_text(_csv(17), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exactly 18"):
                load_rows(path, "owner@example.com", "hr@example.com")

    def test_team_chat_trust_boundary_is_server_owned(self):
        root = Path(__file__).parent
        route = (root / "api/common_routes.py").read_text(encoding="utf-8")
        websocket = (root / "services/websocket_service.py").read_text(encoding="utf-8")
        self.assertIn('recipient_user_id: Optional[str] = Form(None)', route)
        self.assertIn('legacy_recipients = [', route)
        self.assertIn('participants = sorted([user.user_id, recipient_user_id])', route)
        self.assertIn('"sender_id": user.user_id', route)
        self.assertIn('.get("team_scope_id") != org_id', websocket)

    def test_group_rooms_enforce_membership_and_join_time(self):
        root = Path(__file__).parent
        service = (root / "services/team_service.py").read_text(encoding="utf-8")
        route = (root / "api/common_routes.py").read_text(encoding="utf-8")
        common = (root / "services/common_service.py").read_text(encoding="utf-8")
        self.assertIn('EVERYONE_ROOM_ID = "team_room_everyone"', service)
        self.assertIn('ANNOUNCEMENTS_ROOM_ID = "team_room_announcements"', service)
        self.assertIn('doc.get("chat_type") != "announcements"', service)
        self.assertIn('if not 2 <= len(participants) <= 100', service)
        self.assertIn('message_query["timestamp"] = {"$gte":', service)
        self.assertIn('user_id not in room.get("participants", [])', service)
        self.assertIn('doc.get("last_message", "") if is_participant else ""', service)
        self.assertIn('for_post=True', route)
        self.assertIn('for recipient_id in recipient_ids', common)


class MemoryCollection:
    def __init__(self, documents=()):
        self.documents = list(deepcopy(documents))
        self.writes = []

    def find(self, query):
        def matches(doc, condition):
            return all(
                any(matches(doc, choice) for choice in value) if key == "$or" else doc.get(key) == value
                for key, value in condition.items()
            )
        return [deepcopy(doc) for doc in self.documents if matches(doc, query)]

    def find_one(self, query):
        return next(iter(self.find(query)), None)

    def update_one(self, query, update, upsert=False):
        self.writes.append((deepcopy(query), deepcopy(update)))
        doc = next((doc for doc in self.documents if all(doc.get(key) == value for key, value in query.items())), None)
        inserted = doc is None
        if inserted:
            if not upsert:
                raise AssertionError("Update targeted a missing document")
            doc = {**query, **deepcopy(update.get("$setOnInsert", {}))}
            self.documents.append(doc)
        doc.update(deepcopy(update.get("$set", {})))
        return SimpleNamespace(upserted_id=doc.get("_id", doc.get("org_id")) if inserted else None)


class MemoryClerk(provision.Clerk):
    def __init__(self):
        self.orgs = {"org_parent": {"id": "org_parent", "name": "Parent", "slug": "parent", "public_metadata": {}, "max_allowed_memberships": 12}}
        emails = ["admin@example.com", "hr@example.com", *[f"sales{i:02d}@example.com" for i in range(1, 19)]]
        self.users = {f"user_{i}": {"id": f"user_{i}", "email_addresses": [{"email_address": email}]} for i, email in enumerate(emails)}
        self.members = {"org_parent": [self.membership("user_0", "org:admin")]}
        self.invitations = {"org_parent": []}
        self.writes = []
        self.reads = []
        self.fail_slug = None
        self.fail_after_creation = False
        self.fail_path = None

    def membership(self, user_id, role):
        return {"role": role, "public_user_data": {"user_id": user_id, "identifier": self.users[user_id]["email_addresses"][0]["email_address"]}}

    async def close(self):
        pass

    async def request(self, method, path, **kwargs):
        params = kwargs.get("params", {})
        payload = kwargs.get("json", {})
        parts = path.strip("/").split("/")
        if method == "GET":
            self.reads.append((path, deepcopy(params)))
            if path == "/users":
                emails = {value for key, value in params if key == "email_address"}
                return deepcopy([user for user in self.users.values() if any(a["email_address"] in emails for a in user["email_addresses"])])
            if path == "/organizations":
                data = list(self.orgs.values())
            elif len(parts) == 2:
                return deepcopy(self.orgs[parts[1]])
            elif parts[2] == "memberships":
                data = self.members[parts[1]]
            else:
                data = self.invitations[parts[1]]
            offset, limit = params.get("offset", 0), params.get("limit", 100)
            return {"data": deepcopy(data[offset:offset + limit]), "total_count": len(data)}
        if path == self.fail_path:
            raise RuntimeError("Simulated external write failure")
        if path == "/organizations":
            if payload["slug"] == self.fail_slug and not self.fail_after_creation:
                raise RuntimeError("Simulated create failure")
            org_id = f"org_child_{len(self.orgs)}"
            self.orgs[org_id] = {"id": org_id, **deepcopy(payload)}
            self.members[org_id] = [self.membership(payload["created_by"], "org:admin")]
            self.invitations[org_id] = []
            self.writes.append((method, path, deepcopy(payload)))
            if payload["slug"] == self.fail_slug:
                raise RuntimeError("Lost response after Clerk created the child")
            return deepcopy(self.orgs[org_id])
        org_id = parts[1]
        if len(parts) == 2:
            self.orgs[org_id].update(payload)
        elif parts[2] == "metadata":
            self.orgs[org_id]["public_metadata"].update(payload["public_metadata"])
        elif parts[2] == "memberships":
            if method == "DELETE":
                self.members[org_id] = [m for m in self.members[org_id] if m["public_user_data"]["user_id"] != parts[3]]
            elif method == "PATCH":
                next(m for m in self.members[org_id] if m["public_user_data"]["user_id"] == parts[3])["role"] = payload["role"]
            else:
                if len(self.members[org_id]) >= self.orgs[org_id]["max_allowed_memberships"]:
                    raise RuntimeError("Membership cap exceeded")
                self.members[org_id].append(self.membership(payload["user_id"], payload["role"]))
        elif len(parts) == 5:
            self.invitations[org_id] = [i for i in self.invitations[org_id] if i["id"] != parts[3]]
        else:
            self.invitations[org_id].append({"id": f"inv_{len(self.writes)}", **deepcopy(payload)})
        self.writes.append((method, path, deepcopy(payload)))
        return {}


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.csv = Path(directory.name) / "workspaces.csv"
        self.csv.write_text(_csv(), encoding="utf-8")
        self.clerk = MemoryClerk()
        self.db = SimpleNamespace(
            organizations=MemoryCollection([{"org_id": "org_parent", "owner": {}, "plan": "free", "wa_id": "parent_wa"}]),
            feature_entitlements=MemoryCollection([{"_id": "org_parent:SEATS", "account_id": "org_parent", "product": "SEATS", "limits": {"seats": 12}}]),
        )
        settings = ModuleType("config.settings")
        settings.CLERK_TOKEN = "mock-token"
        database = ModuleType("database")
        database.get_mongo_db = lambda: self.db
        for context in (
            patch.dict(sys.modules, {"config.settings": settings, "database": database}),
            patch.object(provision, "Clerk", return_value=self.clerk),
            patch.dict(os.environ, {"ENTITLEMENT_ENFORCEMENT_MODE": "observe"}),
        ):
            context.start()
            self.addCleanup(context.stop)
        self.args = SimpleNamespace(main_org_id="org_parent", admin_email="admin@example.com", hr_email="hr@example.com", csv=str(self.csv), apply=True)

    def run_setup(self, apply=True):
        self.args.apply = apply
        return asyncio.run(provision.run(self.args))

    def provision_children(self):
        report = self.run_setup()
        self.assertNotIn("error", report)
        self.assertEqual(report["actual_group_count"], 18)
        return report

    def test_first_setup_and_unchanged_rerun_preserve_settings(self):
        report = self.provision_children()
        self.assertTrue(report["parent_finalized"])
        self.assertEqual(len(report["completed_org_ids"]), 18)
        child_id = report["completed_org_ids"][0]
        child = next(doc for doc in self.db.organizations.documents if doc["org_id"] == child_id)
        child.update({"wa_id": "connected_wa", "ig_id": "connected_ig", "status": "suspended", "plan": "enterprise", "owner": {"custom": True}, "onboarding_flow_completed": False})
        preserved = deepcopy(child)
        grant = next(doc for doc in self.db.feature_entitlements.documents if doc["account_id"] == child_id)
        grant["limits"]["seats"] = 9
        original_grant = deepcopy(grant)
        self.clerk.writes.clear()
        second = self.provision_children()
        for key in ("wa_id", "ig_id", "status", "plan", "owner", "onboarding_flow_completed", "created_at"):
            self.assertEqual(child[key], preserved[key])
        self.assertEqual(grant, original_grant)
        self.assertEqual(second["workspaces"][0]["entitlements_cloned"], 0)
        self.assertFalse(any(method == "POST" and path == "/organizations" for method, path, _ in self.clerk.writes))
        self.assertEqual(self.clerk.orgs["org_parent"]["max_allowed_memberships"], 12)
        self.assertEqual(self.db.organizations.find_one({"org_id": "org_parent"})["wa_id"], "parent_wa")

    def test_dry_run_previews_removals_without_writes(self):
        report = self.provision_children()
        child_id = report["completed_org_ids"][0]
        self.clerk.members[child_id].append(self.clerk.membership("user_3", "org:member"))
        self.clerk.invitations[child_id].append({"id": "stale", "email_address": "old@example.com", "role": "org:member"})
        self.clerk.writes.clear()
        self.db.organizations.writes.clear()
        self.db.feature_entitlements.writes.clear()
        preview = self.run_setup(apply=False)
        self.assertNotIn("error", preview)
        actions = preview["workspaces"][0]["membership_actions"]
        self.assertIn("removed", [item["action"] for item in actions])
        self.assertIn("invitation_revoked", [item["action"] for item in actions])
        self.assertIn("hides", preview["parent_conversion_effect"])
        self.assertFalse(self.clerk.writes or self.db.organizations.writes or self.db.feature_entitlements.writes)

    def test_reassignment_removes_stale_access_before_adding_at_capacity(self):
        report = self.provision_children()
        child_id = report["completed_org_ids"][0]
        self.clerk.members[child_id].append(self.clerk.membership("user_4", "org:member"))
        self.clerk.members[child_id][1]["role"] = "org:admin"
        self.csv.write_text(_csv().replace("sales01@example.com", "replacement@example.com"), encoding="utf-8")
        self.clerk.users["user_replacement"] = {"id": "user_replacement", "email_addresses": [{"email_address": "replacement@example.com"}]}
        self.clerk.invitations[child_id] = [{"id": "stale", "email_address": "old@example.com", "role": "org:admin"}]
        self.provision_children()
        members = {m["public_user_data"]["user_id"]: m["role"] for m in self.clerk.members[child_id]}
        self.assertEqual(members, {"user_0": "org:admin", "user_1": "org:member", "user_replacement": "org:member"})
        self.assertFalse(self.clerk.invitations[child_id])

    def test_pending_invitation_cleanup_and_retention(self):
        report = self.provision_children()
        child_id = report["completed_org_ids"][0]
        self.clerk.members[child_id] = self.clerk.members[child_id][:2]
        del self.clerk.users["user_2"]
        self.clerk.invitations[child_id] = [
            {"id": "wrong-role", "email_address": "sales01@example.com", "role": "org:admin"},
            {"id": "valid", "email_address": "sales01@example.com", "role": "org:member"},
            {"id": "duplicate", "email_address": "sales01@example.com", "role": "org:member"},
        ]
        self.provision_children()
        self.assertEqual([i["id"] for i in self.clerk.invitations[child_id]], ["valid"])
        self.clerk.invitations[child_id][0]["role"] = "org:admin"
        self.provision_children()
        self.assertEqual(len(self.clerk.invitations[child_id]), 1)
        self.assertEqual(self.clerk.invitations[child_id][0]["role"], "org:member")

    def test_pagination_finds_children_members_and_invitations_after_first_page(self):
        report = self.provision_children()
        child_id = report["completed_org_ids"][0]
        self.clerk.orgs = {**{f"other_{i}": {"id": f"other_{i}", "slug": f"other-{i}", "public_metadata": {}} for i in range(101)}, **self.clerk.orgs}
        self.clerk.members[child_id].extend({"role": "org:member", "public_user_data": {"user_id": f"old_{i}", "identifier": f"old{i}@example.com"}} for i in range(101))
        self.clerk.invitations[child_id] = [{"id": f"old_inv_{i}", "email_address": f"old{i}@example.com", "role": "org:member"} for i in range(101)]
        self.provision_children()
        self.assertEqual(len(self.clerk.members[child_id]), 3)
        self.assertFalse(self.clerk.invitations[child_id])
        self.assertTrue(any(params.get("offset", 0) >= 100 for _, params in self.clerk.reads if isinstance(params, dict)))

    def test_renames_and_conflicting_groups_fail_before_writes(self):
        self.provision_children()
        self.csv.write_text(_csv().replace("Customer - Branch 01", "Renamed Branch"), encoding="utf-8")
        self.clerk.writes.clear()
        report = self.run_setup()
        self.assertIn("renaming", report["error"])
        self.assertFalse(self.clerk.writes)
        self.csv.write_text(_csv(), encoding="utf-8")
        self.clerk.orgs["org_child_1"]["public_metadata"]["workspace_group_id"] = "other_parent"
        report = self.run_setup()
        self.assertIn("Conflicting", report["error"])
        self.assertFalse(self.clerk.writes)

    def check_create_failure(self, after_creation):
        self.clerk.fail_slug = "customer-branch-03"
        self.clerk.fail_after_creation = after_creation
        failed = self.run_setup()
        self.assertIn("error", failed)
        self.assertEqual(len(failed["completed_org_ids"]), 2)
        self.assertFalse(failed["parent_finalized"])
        self.assertEqual(self.clerk.orgs["org_parent"]["public_metadata"], {})
        self.clerk.fail_slug = None
        self.provision_children()
        self.assertEqual(len(self.clerk.orgs), 19)

    def test_create_failure_resumes_without_duplicates(self):
        self.check_create_failure(False)

    def test_lost_create_response_resumes_without_duplicates(self):
        self.check_create_failure(True)

    def test_child_configuration_and_parent_finalization_failures_resume(self):
        self.clerk.fail_path = "/organizations/org_child_1/metadata"
        failed = self.run_setup()
        self.assertFalse(failed["parent_finalized"])
        self.assertEqual(len(self.clerk.orgs), 2)
        self.assertEqual(len(self.db.organizations.documents), 1)
        self.clerk.fail_path = "/organizations/org_parent/metadata"
        failed = self.run_setup()
        self.assertEqual(len(failed["completed_org_ids"]), 18)
        self.assertFalse(failed["parent_finalized"])
        self.assertEqual(self.clerk.orgs["org_parent"]["public_metadata"], {})
        self.clerk.fail_path = None
        self.provision_children()
        self.assertEqual(len(self.clerk.orgs), 19)

    def test_alias_and_invalid_inputs_fail_without_writes(self):
        self.clerk.users["user_0"]["email_addresses"].append({"email_address": "sales01@example.com"})
        del self.clerk.users["user_2"]
        report = self.run_setup()
        self.assertIn("different Clerk users", report["error"])
        self.assertFalse(self.clerk.writes)
        self.csv.write_text(_csv(17), encoding="utf-8")
        report = self.run_setup()
        self.assertIn("exactly 18", report["error"])
        self.assertFalse(self.clerk.writes)

    def test_extra_clerk_only_child_and_foreign_slug_block_all_writes(self):
        self.provision_children()
        self.clerk.orgs["extra"] = {
            "id": "extra", "slug": "extra-branch", "public_metadata": {
                "workspace_kind": "child", "workspace_group_id": "org_parent", "parent_org_id": "org_parent",
            },
        }
        self.clerk.writes.clear()
        report = self.run_setup()
        self.assertIn("extra children", report["error"])
        self.assertFalse(self.clerk.writes)
        del self.clerk.orgs["extra"]
        self.clerk.orgs["foreign"] = {"id": "foreign", "slug": "foreign-branch", "public_metadata": {}}
        self.csv.write_text(_csv().replace("Customer - Branch 01", "Foreign Branch"), encoding="utf-8")
        report = self.run_setup()
        self.assertIn("error", report)
        self.assertFalse(self.clerk.writes)

    def test_two_salespeople_fill_four_seats(self):
        self.csv.write_text(_csv().replace("sales01@example.com,", "sales01@example.com,sales02@example.com"), encoding="utf-8")
        report = self.provision_children()
        child_id = report["completed_org_ids"][0]
        self.assertEqual(len(self.clerk.members[child_id]), 4)
        self.assertEqual(self.clerk.orgs[child_id]["max_allowed_memberships"], 4)

    def test_omitting_hr_provisions_only_admin_and_assigned_salesperson(self):
        self.args.hr_email = ""
        preview = self.run_setup(apply=False)
        self.assertNotIn("error", preview)
        self.assertFalse(self.clerk.writes or self.db.organizations.writes)
        self.assertEqual(preview["main_membership_actions"], [])
        for index, workspace in enumerate(preview["workspaces"], 1):
            self.assertEqual({item["email"] for item in workspace["membership_actions"]},
                             {"admin@example.com", f"sales{index:02d}@example.com"})
        report = self.provision_children()
        for index, child_id in enumerate(report["completed_org_ids"], 1):
            members = {m["public_user_data"]["identifier"]: m["role"] for m in self.clerk.members[child_id]}
            self.assertEqual(members, {"admin@example.com": "org:admin", f"sales{index:02d}@example.com": "org:member"})
            self.assertEqual(self.db.organizations.find_one({"org_id": child_id})["member_emails"],
                             [f"sales{index:02d}@example.com"])
        self.assertEqual(len(self.clerk.members["org_parent"]), 1)
        self.provision_children()
        self.assertFalse(any(value == "" for path, params in self.clerk.reads if path == "/users"
                             for key, value in params if key == "email_address"))

    def test_optional_hr_still_rejects_invalid_or_admin_email(self):
        for hr_email in ("invalid", "admin@example.com"):
            with self.subTest(hr_email=hr_email):
                self.args.hr_email = hr_email
                report = self.run_setup()
                self.assertIn("optional HR", report["error"])
                self.assertFalse(self.clerk.writes or self.db.organizations.writes)

    def test_cli_reports_failure_and_exits_nonzero(self):
        self.csv.write_text(_csv(17), encoding="utf-8")
        output = io.StringIO()
        argv = ["provision_customer_orgs", "--main-org-id", "org_parent", "--admin-email", "admin@example.com",
                "--hr-email", "hr@example.com", "--csv", str(self.csv), "--apply"]
        with patch.object(sys, "argv", argv), redirect_stdout(output), self.assertRaises(SystemExit) as exit_result:
            runpy.run_path(str(Path(provision.__file__)), run_name="__main__")
        self.assertEqual(exit_result.exception.code, 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report["failed_step"], "preflight")
        self.assertEqual(report["completed_org_ids"], [])
        self.assertFalse(self.clerk.writes)


class ClerkResponseTests(unittest.TestCase):
    def test_request_and_user_lookup_accept_array_response(self):
        clerk = object.__new__(provision.Clerk)
        response = Mock(status_code=200)
        response.json.return_value = [{"id": "user_sales", "email_addresses": [{"email_address": "sales@example.com"}]}]
        clerk.client = SimpleNamespace(request=AsyncMock(return_value=response))
        users = asyncio.run(clerk.users_by_email({"sales@example.com"}))
        self.assertEqual(users, {"sales@example.com": "user_sales"})
        self.assertIn(("email_address", "sales@example.com"), clerk.client.request.call_args.kwargs["params"])


if __name__ == "__main__":
    unittest.main()
