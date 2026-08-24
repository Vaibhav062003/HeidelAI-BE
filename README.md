# HeidelAI Backend

This is the backend for the HeidelAI project.

## Product entitlements

Set `ENTITLEMENT_HMAC_SECRET` to the same secret used by the CRM product adapter, `INTERNAL_HMAC_SECRET` to the Math API signing secret, and `CRM_COMMERCIAL_BASE_URL` to the CRM origin. Enforcement defaults to `observe` so a deployment cannot interrupt legacy organizations before grants are synchronized. Set `ENTITLEMENT_ENFORCEMENT_MODE=enforce` only after the CRM entitlement import reports every AI-enabled organization in sync; startup refuses enforcement while an AI organization is missing an active AI or legacy CHATBOT grant. Trial and monthly quota boundaries use `Asia/Kolkata`.

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
