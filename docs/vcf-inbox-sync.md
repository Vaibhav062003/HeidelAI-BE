# VCF inbox synchronization

VCF imports synchronize saved contact names and search prefixes to existing WhatsApp conversations on every business line in the organization. They preserve conversation IDs, activity timestamps, unread counts, assignments, and messages. Contacts without a chat retain the existing first-message behavior. A partial inbox-sync result preserves the successful contact-import counts and can be retried by re-uploading the file.

Deploy the frontend handling of `reason: "contact_name_updated"` before the backend. Validate on dev before main; apply only the branch-specific fix.

## Repair previously imported contacts

The repair command calls the running API so authorized open inboxes receive the normal conversation update events. It requires an organization admin token, supplied through `HEIDELAI_API_TOKEN`; the API rejects a token for another organization. Select the appropriate dev or production API URL explicitly.

```sh
python -m scripts.repair_vcf_inbox_contacts --api-url https://api.heidelai.com --org-id org_example
```

This defaults to dry-run. Inspect `changes`, including the before-values, and `errors`. It repairs active contacts whose names remain locked by VCF using their current saved names. Conflicting duplicate contacts are reported and skipped.

After reviewing the report, use the same command with `--apply`. Each contact repair uses a transaction and checks that the observed contact has not changed. Repairs advance the contact metadata update time; conversation activity times stay unchanged. Failed repairs are reported without committing their changes; rerun the dry-run after resolving errors. A second successful application should report no changes.

The upload response retains existing fields and adds optional `inbox_sync`, with `status` (`synced` or `partial`) and `failed_contacts`. No schema migration or message-history rewrite is required.
