from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Annotated
import unittest
from unittest.mock import Mock, patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
import requests

from auth.models import CurrentUserModel
from test_workspace_groups import MemoryCollection


class RenameCollection(MemoryCollection):
    def update_one(self, query, update, upsert=False):
        if not self.find_one(query):
            return SimpleNamespace(matched_count=0)
        super().update_one(query, update, upsert)
        return SimpleNamespace(matched_count=1)


class WorkspaceRenameTests(unittest.TestCase):
    def setUp(self):
        self.user = CurrentUserModel(user_id="admin", org_id="parent", role="org:admin")
        self.metadata = {"workspace_kind": "child", "parent_org_id": "parent", "workspace_group_id": "parent"}
        self.original = {"org_id": "child", "org_name": "Sales — Archana", **self.metadata,
                         "provisioning_key": "sales-archana", "wa_id": "connected", "member_emails": ["sales@example.com"]}
        self.collection = RenameCollection([
            {"org_id": "parent", "workspace_kind": "management", "workspace_group_id": "parent"},
            self.original,
            {"org_id": "sibling", "org_name": "Sales — Other", **self.metadata},
        ])
        self.organization = {"id": "child", "name": self.original["org_name"], "slug": "sales-archana", "public_metadata": deepcopy(self.metadata)}

        def module(**values):
            result = ModuleType("stub")
            result.__dict__.update(values)
            return result

        dependencies = {
            "config.settings": module(CLERK_TOKEN="mock-token"),
            "database": module(get_mongo_db=lambda: SimpleNamespace(organizations=self.collection)),
            "core.limiter": module(limiter=SimpleNamespace(limit=lambda _: lambda function: function)),
            "auth.dependencies": module(CurrentUser=Annotated[CurrentUserModel, Depends(lambda: self.user)]),
            "services.push_notification_service": module(process_member_device_revocation=Mock(), queue_member_device_revocation=Mock()),
            "services.entitlement_service": module(release_capacity=Mock(), require_feature=Mock(), reserve_capacity=Mock()),
        }
        spec = importlib.util.spec_from_file_location("workspace_rename_routes", Path(__file__).parent / "api/clerk_routes.py")
        self.routes = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, dependencies):
            spec.loader.exec_module(self.routes)
        app = FastAPI()
        app.include_router(self.routes.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.get = patch.object(self.routes.requests, "get", side_effect=lambda *_a, **_k: self.response())
        self.patch = patch.object(self.routes.requests, "patch", side_effect=self.rename_in_clerk)
        self.get_mock = self.get.start()
        self.patch_mock = self.patch.start()
        self.addCleanup(self.get.stop)
        self.addCleanup(self.patch.stop)

    def response(self):
        return Mock(ok=True, status_code=200, json=lambda: deepcopy(self.organization))

    def rename_in_clerk(self, *_args, **kwargs):
        self.organization["name"] = kwargs["json"]["name"]
        return self.response()

    def rename(self, name="Sales — Renamed", child_id="child"):
        return self.client.patch(f"/api/clerk/organizations/{child_id}/name", json={"name": name})

    def test_normalized_rename_changes_only_names_and_is_idempotent(self):
        response = self.rename("  Sales  —\n Renamed  ")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"org_id": "child", "name": "Sales — Renamed"})
        child = self.collection.find_one({"org_id": "child"})
        for key in self.original:
            self.assertEqual(child[key], "Sales — Renamed" if key == "org_name" else self.original[key])
        self.assertEqual(self.patch_mock.call_args.kwargs["json"], {"name": "Sales — Renamed"})
        self.assertEqual(self.organization["slug"], "sales-archana")
        self.assertEqual(set(self.collection.writes[0][1]["$set"]), {"org_name", "updated_at"})
        self.assertEqual(self.rename().status_code, 200)
        self.assertEqual(self.patch_mock.call_count, 1)
        self.assertEqual(len(self.collection.writes), 1)

    def test_invalid_names_are_rejected_before_external_calls(self):
        for name in (" ", "x" * 101, 123, None, {}):
            with self.subTest(name=name):
                self.assertEqual(self.rename(name).status_code, 422)
        self.assertFalse(self.get_mock.called or self.patch_mock.called or self.collection.writes)

    def test_only_parent_admin_can_rename_own_children(self):
        self.user.role = "org:member"
        self.assertEqual(self.rename().status_code, 403)
        self.user.role = "org:admin"
        self.user.org_id = "child"
        self.assertEqual(self.rename().status_code, 403)
        self.user.org_id = "parent"
        for target in ("parent", "foreign", "missing"):
            self.assertEqual(self.rename(child_id=target).status_code, 404)
        self.assertFalse(self.get_mock.called or self.patch_mock.called or self.collection.writes)

    def test_duplicate_names_ignore_case_and_whitespace_but_allow_self(self):
        self.assertEqual(self.rename(" sales  — OTHER ").status_code, 409)
        self.assertFalse(self.get_mock.called or self.patch_mock.called)
        self.assertEqual(self.rename(self.original["org_name"]).status_code, 200)
        self.assertFalse(self.patch_mock.called or self.collection.writes)

    def test_missing_or_inconsistent_clerk_child_is_rejected(self):
        self.get_mock.side_effect = None
        self.get_mock.return_value = Mock(ok=False, status_code=404)
        self.assertEqual(self.rename().status_code, 404)
        self.get_mock.side_effect = lambda *_a, **_k: self.response()
        self.organization["public_metadata"]["parent_org_id"] = "foreign"
        self.assertEqual(self.rename().status_code, 409)
        self.assertFalse(self.patch_mock.called or self.collection.writes)

    def test_clerk_failure_does_not_change_mongodb(self):
        self.patch_mock.side_effect = requests.Timeout("simulated")
        self.assertEqual(self.rename().status_code, 502)
        self.assertEqual(self.collection.find_one({"org_id": "child"})["org_name"], self.original["org_name"])
        self.assertFalse(self.collection.writes)

    def test_lost_response_after_clerk_write_is_safe_to_retry(self):
        def lost_response(*args, **kwargs):
            self.rename_in_clerk(*args, **kwargs)
            raise requests.Timeout("lost response")
        self.patch_mock.side_effect = lost_response
        self.assertEqual(self.rename().status_code, 502)
        self.assertEqual(self.rename().status_code, 200)
        self.assertEqual(self.patch_mock.call_count, 1)
        self.assertEqual(self.collection.find_one({"org_id": "child"})["org_name"], "Sales — Renamed")

    def test_mongodb_failure_after_clerk_write_is_repaired_by_retry(self):
        with patch.object(self.collection, "update_one", side_effect=RuntimeError("database unavailable")):
            self.assertEqual(self.rename().status_code, 502)
        self.assertEqual(self.organization["name"], "Sales — Renamed")
        self.assertEqual(self.rename().status_code, 200)
        self.assertEqual(self.patch_mock.call_count, 1)
        self.assertEqual(self.collection.find_one({"org_id": "child"})["org_name"], "Sales — Renamed")

    def test_deleted_child_is_not_recreated_during_save(self):
        def delete_child(*args, **kwargs):
            self.collection.documents = [doc for doc in self.collection.documents if doc.get("org_id") != "child"]
            return SimpleNamespace(matched_count=0)
        with patch.object(self.collection, "update_one", side_effect=delete_child) as update:
            self.assertEqual(self.rename().status_code, 404)
        self.assertNotIn("upsert", update.call_args.kwargs)
        self.assertIsNone(self.collection.find_one({"org_id": "child"}))


if __name__ == "__main__":
    unittest.main()
