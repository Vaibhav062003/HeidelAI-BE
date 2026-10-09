"""Provision exactly 18 grouped customer workspaces in Clerk and MongoDB.

Dry-run is the default. The command only writes when --apply is supplied.
Omit --hr-email to give each child only admin and salesperson access.
The CSV is the exact child roster; apply removes other child members/invitations.
Parent conversion happens last and hides its existing customer views.
Run one provisioner per parent at a time; rerun the same CSV after a failure.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
EXPECTED_ROWS = 18
REDIRECT_URL = "https://www.heidelai.com/accept_invitation"


@dataclass(frozen=True)
class WorkspaceRow:
    name: str
    slug: str
    sales_emails: tuple[str, ...]


def slugify(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")[:90]


def valid_email(value: str) -> bool:
    return bool(EMAIL.fullmatch(value))


def load_rows(path: Path, admin_email: str, hr_email: str = "") -> list[WorkspaceRow]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"workspace_name", "sales_email_1", "sales_email_2"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"CSV headers must include: {', '.join(sorted(required))}")
        raw_rows = list(reader)
    if len(raw_rows) != EXPECTED_ROWS:
        raise ValueError(f"CSV must contain exactly {EXPECTED_ROWS} workspace rows")

    shared = {admin_email.casefold(), hr_email.casefold()}
    rows = []
    for number, raw in enumerate(raw_rows, start=2):
        name = " ".join((raw.get("workspace_name") or "").split())
        emails = tuple(
            value for value in (
                (raw.get("sales_email_1") or "").strip().lower(),
                (raw.get("sales_email_2") or "").strip().lower(),
            ) if value
        )
        if not name or len(name) > 100:
            raise ValueError(f"Row {number}: workspace_name must be 1-100 characters")
        if len(emails) not in {1, 2} or len(set(emails)) != len(emails):
            raise ValueError(f"Row {number}: provide one or two different salesperson emails")
        if any(not valid_email(email) for email in emails):
            raise ValueError(f"Row {number}: invalid salesperson email")
        if shared.intersection(emails):
            raise ValueError(f"Row {number}: admin/HR cannot also be a workspace salesperson")
        slug = slugify(name)
        if not slug:
            raise ValueError(f"Row {number}: workspace_name cannot produce a Clerk slug")
        rows.append(WorkspaceRow(name=name, slug=slug, sales_emails=emails))

    names = [row.name.casefold() for row in rows]
    slugs = [row.slug for row in rows]
    if len(set(names)) != len(names) or len(set(slugs)) != len(slugs):
        raise ValueError("Workspace names must be unique after slug generation")
    return rows


class Clerk:
    def __init__(self, token: str):
        import httpx

        self.client = httpx.AsyncClient(
            base_url="https://api.clerk.com/v1",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=30,
        )

    async def close(self):
        await self.client.aclose()

    async def request(self, method: str, path: str, **kwargs) -> dict | list:
        for attempt in range(3):
            response = await self.client.request(method, path, **kwargs)
            if response.status_code != 429:
                break
            await asyncio.sleep(min(float(response.headers.get("Retry-After", "1")), 5))
        if response.status_code not in {200, 201}:
            raise RuntimeError(f"Clerk {method} {path} failed ({response.status_code}): {response.text[:500]}")
        return response.json()

    async def organization(self, org_id: str) -> dict:
        return await self.request("GET", f"/organizations/{org_id}")

    async def list_records(self, path: str, **params) -> list[dict]:
        records = []
        while True:
            result = await self.request("GET", path, params={**params, "limit": 100, "offset": len(records)})
            page = result["data"]
            records.extend(page)
            if len(page) < 100:
                return records

    async def organizations(self) -> list[dict]:
        # ponytail: scan Clerk at setup time; index groups if instance size makes this slow.
        return await self.list_records("/organizations")

    async def memberships(self, org_id: str) -> list[dict]:
        return await self.list_records(f"/organizations/{org_id}/memberships")

    async def pending_invitations(self, org_id: str) -> list[dict]:
        return await self.list_records(f"/organizations/{org_id}/invitations", status="pending")

    async def users_by_email(self, emails: set[str]) -> dict[str, str]:
        params = [("email_address", email) for email in sorted(emails)] + [("limit", "100")]
        users = await self.request("GET", "/users", params=params)
        result = {}
        for user in users:
            for address in user.get("email_addresses", []):
                email = str(address.get("email_address") or "").casefold()
                if email in emails:
                    result[email] = str(user["id"])
        return result


def public_user_email(membership: dict) -> str:
    return str((membership.get("public_user_data") or {}).get("identifier") or "").casefold()


def member_actions(desired, known_users, memberships, invitations, *, exact=False) -> list[dict]:
    desired_ids = [known_users[email] for email in desired if email in known_users]
    if len(desired_ids) != len(set(desired_ids)):
        raise ValueError("Admin, HR and salespeople must be different Clerk users within each workspace")
    by_user = {}
    for item in memberships:
        user_id = str((item.get("public_user_data") or {}).get("user_id") or "")
        if not user_id:
            raise ValueError("Cannot reconcile a membership without a Clerk user ID")
        by_user[user_id] = item
    actions = []
    if exact:
        for user_id, item in by_user.items():
            if user_id not in desired_ids:
                actions.append({"email": public_user_email(item), "user_id": user_id, "action": "removed"})
    pending = set()
    for invitation in invitations:
        email = str(invitation.get("email_address") or "").strip().casefold()
        if (exact and email not in desired) or (
            email in desired and (email in known_users or invitation.get("role") != desired[email] or email in pending)
        ):
            actions.append({"email": email, "invitation_id": invitation["id"], "action": "invitation_revoked"})
        else:
            pending.add(email)
    for email, role in desired.items():
        user_id = known_users.get(email)
        current = by_user.get(user_id or "")
        if current:
            if current.get("role") != role:
                actions.append({"email": email, "user_id": user_id, "action": "role_updated", "role": role})
            continue
        if user_id:
            actions.append({"email": email, "user_id": user_id, "action": "added", "role": role})
        elif email not in pending:
            actions.append({"email": email, "action": "invited", "role": role})
    return actions


async def ensure_members(clerk, org_id, admin_user_id, desired, known_users, *, exact=False, dry_run=False):
    actions = member_actions(
        desired, known_users, await clerk.memberships(org_id), await clerk.pending_invitations(org_id), exact=exact
    )
    if dry_run:
        return actions
    for item in actions:
        action = item["action"]
        if action == "removed":
            await clerk.request("DELETE", f"/organizations/{org_id}/memberships/{item['user_id']}")
        elif action == "invitation_revoked":
            await clerk.request("POST", f"/organizations/{org_id}/invitations/{item['invitation_id']}/revoke")
        elif action == "role_updated":
            await clerk.request("PATCH", f"/organizations/{org_id}/memberships/{item['user_id']}", json={"role": item["role"]})
        elif action == "added":
            await clerk.request("POST", f"/organizations/{org_id}/memberships", json={"user_id": item["user_id"], "role": item["role"]})
        else:
            await clerk.request("POST", f"/organizations/{org_id}/invitations", json={
                "email_address": item["email"], "role": item["role"],
                "inviter_user_id": admin_user_id, "redirect_url": REDIRECT_URL,
            })
    remaining = member_actions(
        desired, known_users, await clerk.memberships(org_id), await clerk.pending_invitations(org_id), exact=exact
    )
    if remaining:
        raise RuntimeError(f"Membership verification failed for {org_id}; rerun the same CSV")
    return actions


def plan_workspaces(db, parent_id, rows, organizations):
    by_id = {org["id"]: org for org in organizations}
    by_slug = {org.get("slug"): org for org in organizations}
    existing = {}
    for doc in db.organizations.find({"$or": [
        {"parent_org_id": parent_id}, {"workspace_group_id": parent_id, "workspace_kind": "child"},
    ]}):
        org = by_id.get(doc.get("org_id"))
        if not org or not doc.get("provisioning_key"):
            raise ValueError("Existing child is missing its Clerk organization or provisioning key; repair it first")
        existing[org["id"]] = (org, doc["provisioning_key"])
        for key, value in {"parent_org_id": parent_id, "workspace_group_id": parent_id, "workspace_kind": "child"}.items():
            if doc.get(key) != value:
                raise ValueError(f"Conflicting MongoDB group metadata for {org['id']}")
    for org in organizations:
        metadata = org.get("public_metadata") or {}
        if metadata.get("workspace_group_id") == parent_id or metadata.get("parent_org_id") == parent_id:
            if org["id"] != parent_id:
                existing.setdefault(org["id"], (org, org.get("slug")))
    by_key = {}
    for org, key in existing.values():
        metadata = org.get("public_metadata") or {}
        if any(metadata.get(k) != v for k, v in {
            "parent_org_id": parent_id, "workspace_group_id": parent_id, "workspace_kind": "child",
        }.items()):
            raise ValueError(f"Conflicting Clerk group metadata for {org['id']}")
        if not key or key in by_key:
            raise ValueError("Existing children have missing or duplicate provisioning keys; repair them first")
        by_key[key] = org
    row_keys = {row.slug for row in rows}
    if set(by_key) - row_keys:
        raise ValueError("Existing children do not match the CSV; renaming workspaces or adding extra children is unsupported")
    plan = []
    for row in rows:
        org = by_key.get(row.slug)
        if not org and row.slug in by_slug:
            raise ValueError(f"Slug {row.slug} already belongs to another workspace group")
        plan.append({
            "workspace_name": row.name, "slug": row.slug, "sales_emails": list(row.sales_emails),
            "status": "existing" if org else "create", "org_id": org["id"] if org else None,
        })
    return plan, len(existing)


def clone_entitlements(db, source_org_id: str, target_org_id: str, now: datetime) -> int:
    count = 0
    for source in db.feature_entitlements.find({"account_id": source_org_id}):
        product = str(source.get("product") or "")
        clone = {key: value for key, value in source.items() if key != "_id"}
        clone.update({
            "_id": f"{target_org_id}:{product}",
            "account_id": target_org_id,
            "reason": f"Provisioned from management workspace {source_org_id}",
            "created_at": now,
            "updated_at": now,
        })
        limits = dict(clone.get("limits") or {})
        if product == "SEATS":
            limits["seats"] = 4
        elif product == "CHANNELS":
            limits["whatsapp_accounts"] = 1
        clone["limits"] = limits
        result = db.feature_entitlements.update_one({"_id": clone["_id"]}, {"$setOnInsert": clone}, upsert=True)
        count += int(result.upserted_id is not None)
    return count


async def run(args) -> dict:
    report = {
        "mode": "apply" if args.apply else "dry-run",
        "main_org": {"id": args.main_org_id},
        "workspace_count": EXPECTED_ROWS,
        "completed_org_ids": [],
        "parent_finalized": False,
    }
    clerk = None
    step = "preflight"
    try:
        from config.settings import CLERK_TOKEN
        from database import get_mongo_db

        admin_email = args.admin_email.strip().lower()
        hr_email = args.hr_email.strip().lower()
        if not valid_email(admin_email) or (hr_email and (not valid_email(hr_email) or admin_email == hr_email)):
            raise ValueError("Admin must be a valid email; optional HR must be valid and different from admin")
        rows = load_rows(Path(args.csv), admin_email, hr_email)
        if not CLERK_TOKEN:
            raise RuntimeError("CLERK_TOKEN_KEY is not configured")
        db = get_mongo_db()
        main_doc = db.organizations.find_one({"org_id": args.main_org_id})
        if not main_doc:
            raise ValueError("Main organization is missing from MongoDB")
        grants = list(db.feature_entitlements.find({"account_id": args.main_org_id}))
        if os.getenv("ENTITLEMENT_ENFORCEMENT_MODE", "observe").lower() == "enforce" and not grants:
            raise ValueError("Main organization has no feature entitlements to clone")
        clerk = Clerk(CLERK_TOKEN)
        main_org = await clerk.organization(args.main_org_id)
        main_metadata = main_org.get("public_metadata") or {}
        for metadata in (main_doc, main_metadata):
            if (
                metadata.get("workspace_kind") not in {None, "management"}
                or metadata.get("workspace_group_id") not in {None, args.main_org_id}
                or metadata.get("parent_org_id")
            ):
                raise ValueError("The main organization already belongs to another workspace group")
        report["main_org"]["name"] = main_org.get("name")
        report["parent_finalized"] = all(
            metadata.get("workspace_kind") == "management" for metadata in (main_doc, main_metadata)
        )
        main_memberships = await clerk.memberships(args.main_org_id)
        admin_membership = next((item for item in main_memberships if public_user_email(item) == admin_email), None)
        if not admin_membership or admin_membership.get("role") not in {"admin", "org:admin"}:
            raise ValueError("Admin email must belong to the main organization as an admin")
        admin_user_id = str((admin_membership.get("public_user_data") or {}).get("user_id") or "")
        if not admin_user_id:
            raise ValueError("Admin membership is missing its Clerk user ID")
        shared_members = {admin_email: "org:admin"}
        if hr_email:
            shared_members[hr_email] = "org:member"
        all_emails = set(shared_members)
        for row in rows:
            all_emails.update(row.sales_emails)
        known_users = await clerk.users_by_email(all_emails)
        known_users[admin_email] = admin_user_id
        plan, existing_count = plan_workspaces(db, args.main_org_id, rows, await clerk.organizations())
        report.update({
            "existing_group_count": existing_count,
            "projected_group_count": len(plan),
            "entitlements_to_clone": sorted(item["product"] for item in grants if item.get("product")),
            "parent_conversion_effect": "Management mode hides the parent's existing customer inbox and settings; their data and channels are retained.",
            "main_membership_actions": await ensure_members(
                clerk, args.main_org_id, admin_user_id, shared_members, known_users, dry_run=True,
            ),
            "workspaces": plan,
        })
        for item, row in zip(plan, rows):
            desired = {**shared_members, **{email: "org:member" for email in row.sales_emails}}
            item["membership_actions"] = (
                await ensure_members(clerk, item["org_id"], admin_user_id, desired, known_users, exact=True, dry_run=True)
                if item["org_id"] else member_actions(desired, known_users, [], [], exact=True)
            )
        if not args.apply:
            return report

        now = datetime.now(timezone.utc)
        owner = {**(main_doc.get("owner") or {}), "clerk_user_id": admin_user_id, "email": admin_email}
        # ponytail: single-operator setup; add a per-parent lock if concurrent runs become necessary.
        for item, row in zip(plan, rows):
            step = f"create child {row.slug}"
            child_metadata = {"workspace_kind": "child", "parent_org_id": args.main_org_id, "workspace_group_id": args.main_org_id}
            if not item["org_id"]:
                org = await clerk.request("POST", "/organizations", json={
                    "name": row.name, "slug": row.slug, "created_by": admin_user_id,
                    "max_allowed_memberships": 4, "public_metadata": child_metadata,
                })
                item["org_id"] = org["id"]
            child_id = item["org_id"]
            step = f"configure child {child_id}"
            await clerk.request("PATCH", f"/organizations/{child_id}/metadata", json={"public_metadata": child_metadata})
            db.organizations.update_one({"org_id": child_id}, {
                "$set": {
                    "org_name": row.name, "provisioning_key": row.slug, **child_metadata, "updated_at": now,
                },
                "$setOnInsert": {
                    "org_id": child_id, "owner": owner, "wa_id": None, "ig_id": None, "status": "active",
                    "onboarding_flow_completed": True, "sign_up_flow_completed": True,
                    "plan": main_doc.get("plan", "free"), "created_at": now,
                },
            }, upsert=True)
            step = f"reconcile child membership {child_id}"
            desired = {**shared_members, **{email: "org:member" for email in row.sales_emails}}
            item["membership_actions"] = await ensure_members(
                clerk, child_id, admin_user_id, desired, known_users, exact=True,
            )
            await clerk.request("PATCH", f"/organizations/{child_id}", json={"max_allowed_memberships": 4})
            db.organizations.update_one({"org_id": child_id}, {"$set": {
                "member_emails": [email for email in desired if email != admin_email], "max_allowed_members": 4,
            }})
            step = f"clone missing entitlements {child_id}"
            item["entitlements_cloned"] = clone_entitlements(db, args.main_org_id, child_id, now)
            item["status"] = "ready"
            report["completed_org_ids"].append(child_id)

        step = "verify 18 children"
        verified, actual_count = plan_workspaces(db, args.main_org_id, rows, await clerk.organizations())
        report["actual_group_count"] = actual_count
        if actual_count != EXPECTED_ROWS or any(item["status"] != "existing" for item in verified):
            raise RuntimeError("Group verification failed; rerun the same CSV")
        step = "finalize parent membership"
        report["main_membership_actions"] = await ensure_members(
            clerk, args.main_org_id, admin_user_id, shared_members, known_users,
        )
        group_metadata = {"workspace_kind": "management", "workspace_group_id": args.main_org_id}
        step = "finalize parent MongoDB metadata"
        db.organizations.update_one({"org_id": args.main_org_id}, {"$set": {**group_metadata, "updated_at": now}})
        step = "finalize parent Clerk metadata"
        await clerk.request("PATCH", f"/organizations/{args.main_org_id}/metadata", json={"public_metadata": group_metadata})
        report["parent_finalized"] = True
        return report
    except Exception as error:
        report.update({"error": str(error), "failed_step": step})
        return report
    finally:
        if clerk:
            await clerk.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main-org-id", required=True)
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--hr-email", default="", help="Optional HR email for shared access to every workspace")
    parser.add_argument("--csv", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Validate and preview without writing (default)")
    mode.add_argument("--apply", action="store_true", help="Provision workspaces and remove child members/invitations absent from the CSV")
    parsed = parser.parse_args()
    result = asyncio.run(run(parsed))
    print(json.dumps(result, default=str, indent=2))
    raise SystemExit(1 if "error" in result else 0)
