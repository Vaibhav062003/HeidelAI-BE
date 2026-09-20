from pathlib import Path
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
