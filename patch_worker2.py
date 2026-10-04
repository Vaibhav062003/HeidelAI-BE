import re

with open('services/shakti_booking_worker.py', 'r', encoding='utf-8') as f:
    content = f.read()

loop_target = r'(session_doc = await async_db\[f"shakti_sessions_\{org_id\}"\]\.find_one\(\n\s+\{"phone_number": phone\}\n\s+\)\n\s+if not session_doc:\n\s+logger\.info\(\n\s+f"\[ShaktiBooking\] No bot session for phone \{phone\} - "\n\s+"skipping confirmation\."\n\s+\)\n\s+await _audit_log\(\n\s+org_id, phone, "skipped_no_session", sheet_id,\n\s+row_number, booking_type, customer_name\n\s+\)\n\s+continue)'

replacement = r'''\1
                
                cid = session_doc.get("conversation_id", "")
                business_phone_id = None
                if cid.startswith("whatsapp_"):
                    parts = cid.split("_")
                    if len(parts) >= 3:
                        business_phone_id = parts[1]
                
                from services.wa_service import resolve_outbound_whatsapp_connection
                wa_connection = await asyncio.to_thread(
                    resolve_outbound_whatsapp_connection, org_id, None, business_phone_id
                )
                if not wa_connection:
                    logger.error(f"[ShaktiBooking] Could not resolve connection for phone_id {business_phone_id}. Fallback failed.")
                    continue'''

content = re.sub(loop_target, replacement, content)

with open('services/shakti_booking_worker.py', 'w', encoding='utf-8') as f:
    f.write(content)
