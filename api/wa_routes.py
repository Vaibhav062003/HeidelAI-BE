@@
     try:
         org_id = user.org_id
         from services.entitlement_service import consume_quota_async, feature_access, release_quota_async, require_feature
+
+        personalize_name = bool(getattr(req, "personalize_name", False))
+        fallback_name = getattr(req, "fallback_name", None)
+        components = getattr(req, "components", [])
+        delay_seconds = getattr(req, "delay_seconds", 0)
 
         await asyncio.to_thread(require_feature, org_id, "BROADCASTING")
         unique_recipients = {}
         for recipient in req.recipients:
             phone = "".join(character for character in recipient.phone_number if character.isdigit())
             unique_recipients[phone] = recipient.model_copy(update={"phone_number": phone})
         recipients = list(unique_recipients.values())
         
         # Batch lookup names if personalize_name is true
-        if req.personalize_name:
+        if personalize_name:
             phones_missing_names = [r.phone_number for r in recipients if not r.name]
             if phones_missing_names:
                 try:
@@
             run_bulk_send_job,
             wa_id=wa_id,
             job_id=job_id,
             org_id=org_id,
             phone_id=phone_id,
             access_token=access_token,
             template_name=req.template_name,
             language_code=req.language_code,
             recipients=recipients,
-            components=req.components,
-            delay_seconds=req.delay_seconds,
-            personalize_name=req.personalize_name,
-            fallback_name=req.fallback_name,
+            components=components,
+            delay_seconds=delay_seconds,
+            personalize_name=personalize_name,
+            fallback_name=fallback_name,
             # max_concurrent=20, # Optional: can be passed if needed
         )
