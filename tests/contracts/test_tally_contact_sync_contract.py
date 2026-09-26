import ast
import pathlib
import re
import unittest


SOURCE = pathlib.Path("services/tally_service.py").read_text()
TREE = ast.parse(SOURCE)
NODES = [node for node in TREE.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {"_normalized_phone", "_contact_match_id"}]
NAMESPACE = {"re": re}
exec(compile(ast.Module(body=NODES, type_ignores=[]), "services/tally_service.py", "exec"), NAMESPACE)


class TallyContactSyncContract(unittest.TestCase):
    def test_matching_is_exact_and_fails_closed_on_conflicts(self):
        choose = NAMESPACE["_contact_match_id"]
        party = {"master_id": "ledger-1", "gstin": "27ABCDE1234F1Z5", "phone": "+91 98765 43210"}
        self.assertEqual(choose(party, {"ledger-1": {"contact-1"}}, {}, {}), ("contact-1", "master_id"))
        self.assertEqual(choose(party, {}, {party["gstin"]: {"contact-2"}}, {"9876543210": {"contact-2"}}), ("contact-2", "gstin"))
        self.assertEqual(choose(party, {}, {party["gstin"]: {"contact-2"}}, {"9876543210": {"contact-2", "contact-3"}}), ("contact-2", "gstin"))
        self.assertEqual(choose(party, {}, {party["gstin"]: {"contact-2"}}, {"9876543210": {"contact-3"}}), (None, "ambiguous"))

    def test_sync_is_non_destructive_and_entitlement_scoped(self):
        routes = pathlib.Path("api/tally_routes.py").read_text()
        scheduler = pathlib.Path("core/scheduler.py").read_text()
        self.assertIn('"tally.active": False', SOURCE)
        self.assertNotIn('delete_many({"tally.master_id"', SOURCE)
        self.assertIn('"partial" if contact_counts["ambiguous"]', SOURCE)
        self.assertIn('require_feature(integration["org_id"], "TALLY")', SOURCE)
        self.assertIn('"/api/tally/access"', routes)
        self.assertIn("check_tally_contact_sync_health", scheduler)
        entitlements = pathlib.Path("services/entitlement_service.py").read_text()
        self.assertIn('product == "TALLY" and not tally_enabled(account_id)', entitlements)
        self.assertIn("if not strict and not enforcement_enabled():", entitlements)
        self.assertIn('return {"state": "locked"', entitlements)

    def test_contacts_hide_tally_data_outside_org_allowlist(self):
        contacts = pathlib.Path("services/contacts_service.py").read_text()
        self.assertIn("include_tally = tally_enabled(org_id)", contacts)
        self.assertIn('query["platform"] = {"$ne": "tally"}', contacts)
        self.assertIn('if include_tally:\n        projection["tally"] = 1', contacts)


if __name__ == "__main__":
    unittest.main()
