# HeidelAI Backend

This is the backend for the HeidelAI project.

## Product entitlements

Set `ENTITLEMENT_HMAC_SECRET` to the same secret used by the CRM product adapter, `INTERNAL_HMAC_SECRET` to the Math API signing secret, and `CRM_COMMERCIAL_BASE_URL` to the CRM origin. `BILLING_PROXY_ENABLED` defaults to `false`, which keeps `/api/billing/*` available but returns a stable 503 without forwarding credentials to CRM. `ENTITLEMENT_ENFORCEMENT_MODE` defaults to `observe`; in that mode every product is active with unlimited capacity while entitlement writes, usage counters, history, and CRM usage pulls continue normally. Set it to `enforce` only after the CRM entitlement import reports every AI-enabled organization in sync; startup refuses enforcement while an AI organization is missing an active AI or legacy CHATBOT grant. Trial and monthly quota boundaries use `Asia/Kolkata`.

Native invoicing is disabled by default. Enable it for selected organizations with comma-separated, exact organization IDs:

```env
NATIVE_INVOICING_ORG_IDS=org_id_one,org_id_two
```

When `NATIVE_INVOICING_ORG_IDS` is empty, native invoicing is disabled for every organization.
Final invoice numbering uses MongoDB transactions, so enabled deployments must use a replica set or mongos; startup fails closed otherwise. PDF artifacts use Cloudinary and are stored as private, content-addressed raw files, so `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, and `CLOUDINARY_API_SECRET` are mandatory for enabled production organizations. Rate limiting uses process-local memory by default; `RATE_LIMIT_STORAGE_URI=rediss://...` remains optional when shared counters are wanted across replicas. An admin must save the legal seller profile and e-invoice applicability before the first invoice can be issued. `/health` is liveness-only; deployment readiness probes must use `/ready`, which checks MongoDB.

Organizations assessed as requiring GST e-invoicing also need IRIS IRP6 API-integrator credentials. The API base URL must be the exact sandbox or production URL issued by IRIS; arbitrary hosts are rejected.

```env
IRIS_IRP_ENV=sandbox
IRIS_IRP_API_BASE_URL=<sandbox or production HTTPS API host issued by IRIS>
IRIS_IRP_CLIENT_ID=
IRIS_IRP_CLIENT_SECRET=
IRIS_IRP_PUBLIC_KEY_PEM="-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----"
IRP_CREDENTIAL_ENCRYPTION_KEY=
```

`IRP_CREDENTIAL_ENCRYPTION_KEY` is a dedicated Fernet key. Taxpayer IRP usernames and passwords are encrypted before MongoDB persistence and are never returned by the API. Native issuance remains fail-closed for organizations subject to e-invoicing until the live IRP submission path and related compliance artifacts have been verified against the IRIS sandbox; the service never fabricates an IRN.

### Native invoice backup and restore

Back up each enabled organization from a MongoDB replica set or mongos snapshot source:

```bash
python scripts/native_invoice_backup.py export /secure/backups/org-id --org-id org-id
python scripts/native_invoice_backup.py verify /secure/backups/org-id
python scripts/native_invoice_backup.py verify-external /secure/backups/org-id
```

The BSON backup contains invoice data, indexes, attachment bytes, adjustment records, numbering state, and encrypted IRP credentials. Generated PDFs remain private Cloudinary artifacts; their immutable IDs, versions, sizes, and SHA-256 metadata are recorded, but their bytes are not copied. Configure Cloudinary backup/retention separately and do not accept a backup unless `verify-external` passes against the same retained Cloudinary account.

Restore first into an empty database and verify it before switching traffic:

```bash
python scripts/native_invoice_backup.py restore /secure/backups/org-id --uri "$MONGODB_URI" --database restored_db --org-id org-id
python scripts/native_invoice_backup.py verify-restored /secure/backups/org-id --uri "$MONGODB_URI" --database restored_db --org-id org-id
python scripts/native_invoice_backup.py verify-external /secure/backups/org-id
```

Replacing existing tenant data is deliberately blocked unless both `--drop-existing` and `--confirm-drop-org-id org-id` are supplied. A Mongo restore cannot recreate missing Cloudinary PDF bytes.

## Run a script or command

```bash
poetry run python script.py
```

Or run anything in the env:

```bash
poetry run uvicorn app.main:app --reload
```

---

## Poetry Usage

### 🔄 Install dependencies

```bash
poetry install
```

This installs all packages listed in `pyproject.toml` into a virtual environment managed by Poetry.

---

### ➕ Add a package

```bash
poetry add <package-name>
```

Example:

```bash
poetry add requests
```

This will:
- Install the package
- Add it to your `pyproject.toml`
- Update the `poetry.lock` file

---

### 🐚 Activate the Poetry shell

```bash
poetry shell
```

This spawns a shell inside Poetry's virtual environment.
---

### Lock dependencies manually

```bash
poetry lock
```

This updates the `poetry.lock` file without installing packages.

---

### Remove a package

```bash
poetry remove <package-name>
```
