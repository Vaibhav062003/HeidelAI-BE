import os


def _org_enabled(variable: str, org_id: str) -> bool:
    allowed_orgs = {value.strip() for value in os.getenv(variable, "").split(",") if value.strip()}
    return bool(org_id and org_id in allowed_orgs)


def native_invoicing_enabled(org_id: str = "") -> bool:
    return _org_enabled("NATIVE_INVOICING_ORG_IDS", org_id)


def tally_enabled(org_id: str = "") -> bool:
    return _org_enabled("TALLY_ORG_IDS", org_id)
