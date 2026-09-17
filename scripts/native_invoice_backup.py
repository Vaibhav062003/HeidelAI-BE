#!/usr/bin/env python3
"""Export, verify, and restore the MongoDB state needed for native invoices.

The backup directory contains BSON streams so ObjectIds, datetimes, Decimal128
values, and nested audit records round-trip without lossy JSON conversion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from bson import BSON, decode_file_iter
from pymongo import MongoClient


FORMAT_VERSION = 3
TENANT_COLLECTIONS = (
    "native_invoices",
    "native_invoice_sequences",
    "invoice_conversion_claims",
    "native_invoice_adjustments",
    "native_invoice_attachments",
    "iris_irp_credentials",
)
INDEX_OPTIONS = (
    "name",
    "unique",
    "sparse",
    "expireAfterSeconds",
    "partialFilterExpression",
    "hidden",
)


class BackupError(RuntimeError):
    pass


def collection_names(org_id: str) -> tuple[str, ...]:
    if not org_id or not all(character.isalnum() or character in "_-" for character in org_id):
        raise BackupError("A valid explicit organization ID is required")
    return (*TENANT_COLLECTIONS, f"quote_settings_{org_id}")


def _tenant_query(name: str, org_id: str) -> dict:
    return {} if name == f"quote_settings_{org_id}" else {"org_id": org_id}


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _document_count(path: Path) -> int:
    with path.open("rb") as source:
        return sum(1 for _ in decode_file_iter(source))


def _write_bson_stream(path: Path, documents) -> int:
    count = 0
    with path.open("xb") as output:
        os.chmod(path, 0o600)
        for document in documents:
            output.write(BSON.encode(document))
            count += 1
    return count


def external_pdf_records(document: dict, document_type: str, document_id: str) -> list[dict]:
    candidates = [("current", document.get("pdf") or {})]
    candidates.extend(
        ("legacy", entry.get("pdf") or {})
        for entry in document.get("legacy_pdfs") or []
        if isinstance(entry, dict)
    )
    candidates.append(("receipt", document.get("receipt") or {}))
    return [{
        "document_type": document_type,
        "document_id": document_id,
        "artifact_state": state,
        "public_id": pdf.get("public_id"),
        "version": pdf.get("version"),
        "sha256": pdf.get("sha256"),
        "byte_size": pdf.get("byte_size"),
        "resource_type": pdf.get("resource_type") or "raw",
        "delivery_type": pdf.get("delivery_type") or "private",
    } for state, pdf in candidates if pdf.get("public_id")]


def export_backup(uri: str, database: str, output: Path, *, org_id: str) -> dict:
    """Create a new consistent-snapshot backup directory; never overwrite one."""
    output = output.resolve()
    if output.exists():
        raise BackupError(f"Backup destination already exists: {output}")
    output.mkdir(parents=True, mode=0o700)
    client = MongoClient(uri, appname="heidelai-native-invoice-backup", serverSelectionTimeoutMS=10_000)
    try:
        hello = client.admin.command("hello")
        if not hello.get("setName") and hello.get("msg") != "isdbgrid":
            raise BackupError("Native invoice backups require a replica set or mongos snapshot source")
        db = client[database]
        collections = collection_names(org_id)
        indexes = {name: list(db[name].list_indexes()) for name in collections}
        manifest = {
            "format_version": FORMAT_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_database": database,
            "org_id": org_id,
            "collections": {},
            "external_pdf_artifacts": [],
            "external_artifact_policy": {
                "provider": "cloudinary",
                "bytes_included": False,
                "provider_backup_and_retention_required": True,
            },
        }
        with client.start_session(snapshot=True) as session:
            for name in collections:
                data_file = output / f"{name}.bson"
                index_file = output / f"{name}.indexes.bson"
                query = _tenant_query(name, org_id)
                documents = db[name].find(query, session=session).sort("_id", 1)
                document_count = _write_bson_stream(data_file, documents)
                index_count = _write_bson_stream(index_file, indexes[name])
                manifest["collections"][name] = {
                    "documents": document_count,
                    "data_sha256": _digest(data_file),
                    "indexes": index_count,
                    "indexes_sha256": _digest(index_file),
                }
            artifacts = {}
            for collection, document_type, id_field in (
                (db.native_invoices, "invoice", "invoice_id"),
                (db.native_invoice_adjustments, "adjustment", "adjustment_id"),
            ):
                for document in collection.find(
                    {"org_id": org_id, "$or": [
                        {"pdf.public_id": {"$type": "string"}},
                        {"legacy_pdfs.pdf.public_id": {"$type": "string"}},
                        {"receipt.public_id": {"$type": "string"}},
                    ]},
                    {id_field: 1, "pdf": 1, "legacy_pdfs": 1, "receipt": 1},
                    session=session,
                ).sort("_id", 1):
                    for artifact in external_pdf_records(document, document_type, str(document.get(id_field) or "")):
                        previous = artifacts.get(artifact["public_id"])
                        comparable = {key: artifact.get(key) for key in ("version", "sha256", "byte_size", "resource_type", "delivery_type")}
                        if previous and comparable != {key: previous.get(key) for key in comparable}:
                            raise BackupError(f"Conflicting metadata for Cloudinary artifact {artifact['public_id']}")
                        artifacts.setdefault(artifact["public_id"], artifact)
            manifest["external_pdf_artifacts"] = sorted(artifacts.values(), key=lambda value: value["public_id"])
        manifest_tmp = output / "manifest.json.tmp"
        manifest_tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        os.chmod(manifest_tmp, 0o600)
        manifest_tmp.replace(output / "manifest.json")
        return manifest
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise
    finally:
        client.close()


def verify_backup(source: Path) -> dict:
    source = source.resolve()
    manifest_path = source / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as exc:
        raise BackupError("Backup manifest is missing or invalid") from exc
    if manifest.get("format_version") != FORMAT_VERSION:
        raise BackupError("Unsupported native invoice backup format")
    org_id = str(manifest.get("org_id") or "")
    expected_collections = set(collection_names(org_id))
    if set(manifest.get("collections") or {}) != expected_collections:
        raise BackupError("Backup does not contain the exact native invoice collection set")
    if manifest.get("external_artifact_policy") != {
        "provider": "cloudinary",
        "bytes_included": False,
        "provider_backup_and_retention_required": True,
    }:
        raise BackupError("Backup must declare its external Cloudinary artifact dependency")
    public_ids = set()
    for artifact in manifest.get("external_pdf_artifacts") or []:
        if (
            artifact.get("document_type") not in {"invoice", "adjustment"}
            or artifact.get("artifact_state") not in {"current", "legacy", "receipt"}
            or not artifact.get("document_id")
            or not artifact.get("public_id")
            or int(artifact.get("version") or 0) <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", str(artifact.get("sha256") or ""))
            or int(artifact.get("byte_size") or 0) <= 0
            or artifact.get("resource_type") != "raw"
            or artifact.get("delivery_type") != "private"
            or artifact["public_id"] in public_ids
        ):
            raise BackupError("Backup contains invalid external PDF reconciliation metadata")
        public_ids.add(artifact["public_id"])
    for name in expected_collections:
        expected = manifest["collections"][name]
        data_file = source / f"{name}.bson"
        index_file = source / f"{name}.indexes.bson"
        if _digest(data_file) != expected.get("data_sha256"):
            raise BackupError(f"Checksum mismatch for {name}")
        if _digest(index_file) != expected.get("indexes_sha256"):
            raise BackupError(f"Index checksum mismatch for {name}")
        if _document_count(data_file) != expected.get("documents"):
            raise BackupError(f"Document count mismatch for {name}")
        if _document_count(index_file) != expected.get("indexes"):
            raise BackupError(f"Index count mismatch for {name}")
    return manifest


def _restore_documents(collection, source: Path) -> int:
    restored = 0
    batch = []
    with source.open("rb") as input_file:
        for document in decode_file_iter(input_file):
            batch.append(document)
            if len(batch) == 1_000:
                collection.insert_many(batch, ordered=True)
                restored += len(batch)
                batch = []
    if batch:
        collection.insert_many(batch, ordered=True)
        restored += len(batch)
    return restored


def _restore_indexes(collection, source: Path) -> None:
    with source.open("rb") as input_file:
        for index in decode_file_iter(input_file):
            if index.get("name") == "_id_":
                continue
            options = {key: index[key] for key in INDEX_OPTIONS if key in index}
            collection.create_index(list(index["key"].items()), **options)


def restore_backup(
    uri: str,
    database: str,
    source: Path,
    *,
    org_id: str,
    drop_existing: bool = False,
    confirm_drop_org_id: str = "",
) -> dict:
    """Restore into empty target collections unless destructive replacement is explicit."""
    manifest = verify_backup(source)
    if org_id != manifest.get("org_id"):
        raise BackupError("Restore organization must exactly match the backup manifest")
    collections = collection_names(org_id)
    client = MongoClient(uri, appname="heidelai-native-invoice-restore", serverSelectionTimeoutMS=10_000)
    try:
        db = client[database]
        occupied = [name for name in collections if db[name].count_documents(_tenant_query(name, org_id), limit=1)]
        if occupied and not drop_existing:
            raise BackupError(f"Target organization already has data in: {', '.join(occupied)}")
        if drop_existing:
            if confirm_drop_org_id != org_id:
                raise BackupError("Destructive restore requires --confirm-drop-org-id matching --org-id")
            for name in occupied:
                if name == f"quote_settings_{org_id}":
                    db.drop_collection(name)
                else:
                    db[name].delete_many({"org_id": org_id})
        for name in collections:
            restored = _restore_documents(db[name], source / f"{name}.bson")
            if restored != manifest["collections"][name]["documents"]:
                raise BackupError(f"Restore count mismatch for {name}")
            _restore_indexes(db[name], source / f"{name}.indexes.bson")
        return verify_restored(uri, database, source, org_id=org_id)
    finally:
        client.close()


def verify_restored(uri: str, database: str, source: Path, *, org_id: str) -> dict:
    """Compare restored document streams and required index names to the backup."""
    manifest = verify_backup(source)
    if org_id != manifest.get("org_id"):
        raise BackupError("Verification organization must exactly match the backup manifest")
    client = MongoClient(uri, appname="heidelai-native-invoice-restore-check", serverSelectionTimeoutMS=10_000)
    try:
        db = client[database]
        for name in collection_names(org_id):
            value = hashlib.sha256()
            count = 0
            for document in db[name].find(_tenant_query(name, org_id)).sort("_id", 1):
                value.update(BSON.encode(document))
                count += 1
            expected = manifest["collections"][name]
            if count != expected["documents"] or value.hexdigest() != expected["data_sha256"]:
                raise BackupError(f"Restored data does not match {name}")
            with (source / f"{name}.indexes.bson").open("rb") as index_file:
                expected_indexes = {row["name"] for row in decode_file_iter(index_file)}
            actual_indexes = {row["name"] for row in db[name].list_indexes()}
            if actual_indexes != expected_indexes:
                raise BackupError(f"Restored indexes do not match {name}")
        return manifest
    finally:
        client.close()


def verify_external_artifacts(source: Path, *, resource_lookup=None) -> dict:
    """Verify every Cloudinary PDF referenced by the immutable backup manifest."""
    manifest = verify_backup(source)
    if resource_lookup is None:
        import cloudinary
        import cloudinary.api

        required = {
            "cloud_name": os.getenv("CLOUDINARY_CLOUD_NAME"),
            "api_key": os.getenv("CLOUDINARY_API_KEY"),
            "api_secret": os.getenv("CLOUDINARY_API_SECRET"),
        }
        if not all(required.values()):
            raise BackupError("Cloudinary credentials are required for external artifact verification")
        cloudinary.config(**required)
        resource_lookup = cloudinary.api.resource
    for expected in manifest.get("external_pdf_artifacts") or []:
        try:
            actual = resource_lookup(
                expected["public_id"],
                resource_type=expected.get("resource_type") or "raw",
                type=expected.get("delivery_type") or "private",
            )
        except Exception as exc:
            raise BackupError(f"Cloudinary PDF is unavailable for {expected['document_type']} {expected['document_id']}") from exc
        if (
            actual.get("public_id") != expected["public_id"]
            or int(actual.get("version") or -1) != int(expected.get("version") or -2)
            or int(actual.get("bytes") or -1) != int(expected.get("byte_size") or -2)
            or (actual.get("resource_type") or "") != expected.get("resource_type")
            or (actual.get("type") or "") != expected.get("delivery_type")
        ):
            raise BackupError(f"Cloudinary PDF metadata mismatch for {expected['document_type']} {expected['document_id']}")
        remote_sha = (((actual.get("context") or {}).get("custom") or {}).get("sha256"))
        if remote_sha != expected.get("sha256"):
            raise BackupError(f"Cloudinary PDF checksum metadata mismatch for {expected['document_type']} {expected['document_id']}")
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("export", "verify", "verify-external", "restore", "verify-restored"))
    parser.add_argument("path", type=Path, help="Backup directory")
    parser.add_argument("--uri", default=os.getenv("MONGODB_URI"))
    parser.add_argument("--database", default=os.getenv("MONGODB_CLUSTER"))
    parser.add_argument("--org-id", required=False, help="Exact tenant organization ID")
    parser.add_argument("--drop-existing", action="store_true")
    parser.add_argument("--confirm-drop-org-id", default="")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "verify":
        manifest = verify_backup(args.path)
    elif args.command == "verify-external":
        manifest = verify_external_artifacts(args.path)
    else:
        if not args.uri or not args.database:
            raise SystemExit("--uri/--database or MONGODB_URI/MONGODB_CLUSTER are required")
        if not args.org_id:
            raise SystemExit("--org-id is required for database operations")
        if args.command == "export":
            manifest = export_backup(args.uri, args.database, args.path, org_id=args.org_id)
        elif args.command == "restore":
            manifest = restore_backup(
                args.uri,
                args.database,
                args.path,
                org_id=args.org_id,
                drop_existing=args.drop_existing,
                confirm_drop_org_id=args.confirm_drop_org_id,
            )
        else:
            manifest = verify_restored(args.uri, args.database, args.path, org_id=args.org_id)
    counts = {name: value["documents"] for name, value in manifest["collections"].items()}
    print(json.dumps({"ok": True, "format_version": FORMAT_VERSION, "documents": counts}, sort_keys=True))


if __name__ == "__main__":
    main()
