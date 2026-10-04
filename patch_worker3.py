with open('services/shakti_booking_worker.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
for i, line in enumerate(lines):
    if "await _audit_booking(" in line and "skipped_no_session" in lines[i+1]:
        # we're in the audit block of session skip
        pass
    new_lines.append(line)
    if "continue" in line and "skipped_count += 1" in lines[i-1] and "skipped_no_session" in lines[i-3]:
        # This is exactly after `continue` of session skip
        injection = """
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
                    skipped_count += 1
                    continue
"""
        new_lines.append(injection)

with open('services/shakti_booking_worker.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
