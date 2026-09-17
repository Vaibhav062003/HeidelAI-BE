import os


def native_invoicing_enabled(org_id: str = "") -> bool:
    allowed_orgs = {value.strip() for value in os.getenv("NATIVE_INVOICING_ORG_IDS", "").split(",") if value.strip()}
    return bool(org_id and org_id in allowed_orgs)
