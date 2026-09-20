"""Provision exactly 18 grouped customer workspaces in Clerk and MongoDB.

Dry-run is the default. The command only writes when --apply is supplied.
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


def load_rows(path: Path, admin_email: str, hr_email: str) -> list[WorkspaceRow]:
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

    async def request(self, method: str, path: str, **kwargs) -> dict:
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

    async def organization_by_slug(self, slug: str) -> dict | None:
        result = await self.request("GET", "/organizations", params={"query": slug, "limit": 100})
        return next((item for item in result.get("data", []) if item.get("slug") == slug), None)

    async def memberships(self, org_id: str) -> list[dict]:
        return (await self.request("GET", f"/organizations/{org_id}/memberships", params={"limit": 100})).get("data", [])

    async def pending_invitations(self, org_id: str) -> list[dict]:
        return (await self.request(
            "GET", f"/organizations/{org_id}/invitations", params={"status": "pending", "limit": 100}
        )).get("data", [])

    async def users_by_email(self, emails: set[str]) -> dict[str, str]:
        params = [("email_address[]", email) for email in sorted(emails)] + [("limit", "100")]
        users = (await self.request("GET", "/users", params=params)).get("data", [])
        result = {}
        for user in users:
            for address in user.get("email_addresses", []):
                email = str(address.get("email_address") or "").casefold()
                if email in emails:
                    result[email] = str(user["id"])
        return result


def public_user_email(membership: dict) -> str:
    return str((membership.get("public_user_data") or {}).get("identifier") or "").casefold()


async def ensure_members(
    clerk: Clerk,
    org_id: str,
    admin_user_id: str,
    desired: dict[str, str],
    known_users: dict[str, str],
) -> list[dict]:
    memberships = await clerk.memberships(org_id)
    by_user = {
        str((item.get("public_user_data") or {}).get("user_id") or ""): item
        for item in memberships
    }
    pending = {str(item.get("email_address") or "").casefold() for item in await clerk.pending_invitations(org_id)}
    actions = []
    for email, role in desired.items():
        user_id = known_users.get(email)
        current = by_user.get(user_id or "")
        if current:
            if current.get("role") != role:
                await clerk.request("PATCH", f"/organizations/{org_id}/memberships/{user_id}", json={"role": role})
                actions.append({"email": email, "action": "role_updated", "role": role})
            continue
        if user_id:
            await clerk.request(
                "POST", f"/organizations/{org_id}/memberships", json={"user_id": user_id, "role": role}
            )
            actions.append({"email": email, "action": "added", "role": role})
        elif email not in pending:
            await clerk.request("POST", f"/organizations/{org_id}/invitations", json={
                "email_address": email,
                "role": role,
                "inviter_user_id": admin_user_id,
                "redirect_url": REDIRECT_URL,
            })
            actions.append({"email": email, "action": "invited", "role": role})
    return actions


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
        db.feature_entitlements.replace_one({"_id": clone["_id"]}, clone, upsert=True)
        count += 1
    return count


async def run(args) -> dict:
    from config.settings import CLERK_TOKEN
    from database import get_mongo_db

    admin_email = args.admin_email.strip().lower()
    hr_email = args.hr_email.strip().lower()
    if not valid_email(admin_email) or not valid_email(hr_email) or admin_email == hr_email:
        raise ValueError("Admin and HR must be different valid email addresses")
    rows = load_rows(Path(args.csv), admin_email, hr_email)
    if not CLERK_TOKEN:
        raise RuntimeError("CLERK_TOKEN_KEY is not configured")

    db = get_mongo_db()
    main_doc = db.organizations.find_one({"org_id": args.main_org_id})
    if not main_doc:
        raise ValueError("Main organization is missing from MongoDB")
    grants = list(db.feature_entitlements.find({"account_id": args.main_org_id}, {"product": 1, "_id": 0}))
    if os.getenv("ENTITLEMENT_ENFORCEMENT_MODE", "observe").lower() == "enforce" and not grants:
        raise ValueError("Main organization has no feature entitlements to clone")

    clerk = Clerk(CLERK_TOKEN)
    try:
        main_org = await clerk.organization(args.main_org_id)
        main_memberships = await clerk.memberships(args.main_org_id)
        admin_membership = next(
            (item for item in main_memberships if public_user_email(item) == admin_email), None
        )
        if not admin_membership or admin_membership.get("role") not in {"admin", "org:admin"}:
            raise ValueError("Admin email must belong to the main organization as an admin")
        admin_user_id = str((admin_membership.get("public_user_data") or {}).get("user_id") or "")

        all_emails = {admin_email, hr_email}
        for row in rows:
            all_emails.update(row.sales_emails)
        known_users = await clerk.users_by_email(all_emails)
        known_users[admin_email] = admin_user_id

        plan = []
        for row in rows:
            existing = db.organizations.find_one({
                "parent_org_id": args.main_org_id, "provisioning_key": row.slug
            }, {"org_id": 1, "_id": 0})
            clerk_org = await clerk.organization(str(existing["org_id"])) if existing else await clerk.organization_by_slug(row.slug)
            metadata = (clerk_org or {}).get("public_metadata") or {}
            if clerk_org and (
                metadata.get("workspace_group_id") != args.main_org_id
                or metadata.get("workspace_kind") != "child"
            ):
                raise ValueError(f"Slug {row.slug} already belongs to another workspace group")
            plan.append({
                "workspace_name": row.name,
                "slug": row.slug,
                "sales_emails": list(row.sales_emails),
                "status": "existing" if clerk_org else "create",
                "org_id": (clerk_org or {}).get("id"),
            })

        report = {
            "mode": "apply" if args.apply else "dry-run",
            "main_org": {"id": args.main_org_id, "name": main_org.get("name")},
            "workspace_count": len(plan),
            "entitlements_to_clone": sorted(item.get("product") for item in grants if item.get("product")),
            "workspaces": plan,
        }
        if not args.apply:
            return report

        now = datetime.now(timezone.utc)
        group_metadata = {"workspace_kind": "management", "workspace_group_id": args.main_org_id}
        await clerk.request("PATCH", f"/organizations/{args.main_org_id}", json={"max_allowed_memberships": 4})
        await clerk.request("PATCH", f"/organizations/{args.main_org_id}/metadata", json={"public_metadata": group_metadata})
        db.organizations.update_one({"org_id": args.main_org_id}, {"$set": {**group_metadata, "updated_at": now}})
        report["main_membership_actions"] = await ensure_members(
            clerk, args.main_org_id, admin_user_id,
            {admin_email: "org:admin", hr_email: "org:member"}, known_users,
        )

        owner = dict(main_doc.get("owner") or {})
        owner.update({"clerk_user_id": admin_user_id, "email": admin_email})
        for item, row in zip(report["workspaces"], rows):
            clerk_org = await clerk.organization(str(item["org_id"])) if item["org_id"] else None
            if not clerk_org:
                clerk_org = await clerk.request("POST", "/organizations", json={
                    "name": row.name,
                    "slug": row.slug,
                    "created_by": admin_user_id,
                    "max_allowed_memberships": 4,
                    "public_metadata": {
                        "workspace_kind": "child",
                        "parent_org_id": args.main_org_id,
                        "workspace_group_id": args.main_org_id,
                    },
                })
            child_id = str(clerk_org["id"])
            child_metadata = {
                "workspace_kind": "child",
                "parent_org_id": args.main_org_id,
                "workspace_group_id": args.main_org_id,
            }
            await clerk.request("PATCH", f"/organizations/{child_id}", json={"max_allowed_memberships": 4})
            await clerk.request("PATCH", f"/organizations/{child_id}/metadata", json={"public_metadata": child_metadata})
            db.organizations.update_one(
                {"org_id": child_id},
                {"$set": {
                    "org_id": child_id,
                    "org_name": row.name,
                    "owner": owner,
                    "wa_id": None,
                    "ig_id": None,
                    "status": "active",
                    "onboarding_flow_completed": True,
                    "sign_up_flow_completed": True,
                    "plan": main_doc.get("plan", "free"),
                    "max_allowed_members": 4,
                    "member_emails": [hr_email, *row.sales_emails],
                    "provisioning_key": row.slug,
                    **child_metadata,
                    "updated_at": now,
                }, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
            desired = {
                admin_email: "org:admin",
                hr_email: "org:member",
                **{email: "org:member" for email in row.sales_emails},
            }
            item.update({
                "org_id": child_id,
                "status": "ready",
                "membership_actions": await ensure_members(
                    clerk, child_id, admin_user_id, desired, known_users
                ),
                "entitlements_cloned": clone_entitlements(db, args.main_org_id, child_id, now),
            })
        return report
    finally:
        await clerk.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main-org-id", required=True)
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--hr-email", required=True)
    parser.add_argument("--csv", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Validate and preview without writing (default)")
    mode.add_argument("--apply", action="store_true", help="Create/update Clerk and MongoDB records")
    parsed = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(parsed)), default=str, indent=2))
    except (ValueError, RuntimeError) as error:
        print(json.dumps({"mode": "apply" if parsed.apply else "dry-run", "error": str(error)}, indent=2))
        raise SystemExit(1)
