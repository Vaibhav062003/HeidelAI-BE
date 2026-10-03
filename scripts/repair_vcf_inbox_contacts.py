"""Repair via the running API so open inboxes receive updates.

HEIDELAI_API_TOKEN=<organization-admin-token> python -m scripts.repair_vcf_inbox_contacts \
    --api-url https://api.heidelai.com --org-id org_example
Add --apply after inspecting the dry-run report.
"""

import argparse
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--org-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    url = urlsplit(args.api_url)
    if url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}):
        parser.error("--api-url must use HTTPS, or HTTP on localhost.")
    if url.username or url.password or url.query or url.fragment or not url.hostname or not args.org_id.strip():
        parser.error("Use an API base URL and a nonempty organization ID.")
    token = os.environ.get("HEIDELAI_API_TOKEN")
    if not token:
        parser.error("Set HEIDELAI_API_TOKEN to an admin token for the selected organization.")
    query = urlencode({"org_id": args.org_id, "apply": str(args.apply).lower()})
    request = Request(f"{args.api_url.rstrip('/')}/api/contacts/vcf-inbox-repair?{query}", data=b"", headers={"Authorization": f"Bearer {token}"}, method="POST")
    try:
        with build_opener(NoRedirect()).open(request, timeout=60) as response:
            report = json.load(response)
    except HTTPError as error:
        parser.exit(1, f"Repair API returned HTTP {error.code}: {error.read(4096).decode(errors='replace')}\n")
    except (URLError, TimeoutError) as error:
        parser.exit(1, f"Repair request did not finish: {error}. Rerun the dry-run to verify database state.\n")
    print(json.dumps(report, indent=2))
    if report.get("errors"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
