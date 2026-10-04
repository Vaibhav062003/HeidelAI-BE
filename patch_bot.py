import re

with open('services/shakti_bot_service.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Fix _send_outlet_detail
content = content.replace('"cta_label": "Get Directions"', '"button_text": "Get Directions"')
content = content.replace(
    '{"type": "reply", "reply": {"id": "act_menu", "title": "Back to Menu"}}',
    '{"type": "reply", "reply": {"id": "act_menu", "title": "Back to Menu"}}, {"type": "reply", "reply": {"id": "act_end", "title": "End Chat"}}'
)

# 2. Add _send_end_flow_options to CTA functions
cta_addition = '''    await _send_interactive(org_id, wa_id, cid, payload)
    await asyncio.sleep(1)
    back = {
        "type": "button",
        "body_text": "Need anything else?",
        "buttons": [
            {"type": "reply", "reply": {"id": "act_menu", "title": "Main Menu"}},
            {"type": "reply", "reply": {"id": "act_end", "title": "End Chat"}}
        ]
    }
    await _send_interactive(org_id, wa_id, cid, back)'''

content = re.sub(r'(async def _send_test_ride_cta.*?})([\s\n]+)(    await _send_interactive\(org_id, wa_id, cid, payload\))', r'\1\2' + cta_addition, content, flags=re.DOTALL)
content = re.sub(r'(async def _send_service_cta.*?})([\s\n]+)(    await _send_interactive\(org_id, wa_id, cid, payload\))', r'\1\2' + cta_addition, content, flags=re.DOTALL)


# 3. Handle act_end globally
global_end = '''
    if interactive_id == "act_end" or "end chat" in text_content:
        await _update_session(org_id, customer_phone, "S_ENDED", ctx, conversation_id=cid)
        await _send_text(org_id, wa_id, cid, "Thanks for chatting with Shakti Motors! Have a great day.")
        await _audit(org_id, customer_phone, wa_message_id, "ended_chat", current_state, "S_ENDED")
        return True
'''
content = content.replace('    if text_content in AGENT_KEYWORDS:', global_end + '    if text_content in AGENT_KEYWORDS:')

# 4. Handle act_menu in terminal states
# We need to find the specific block for S_ENDED:
#    if current_state in ("S_ENDED", "S8_AGENT"):
#        if text_content in TRIGGER_KEYWORDS:

content = content.replace(
'''    if current_state in ("S_ENDED", "S8_AGENT"):
        if text_content in TRIGGER_KEYWORDS:''',
'''    if current_state in ("S_ENDED", "S8_AGENT"):
        if text_content in TRIGGER_KEYWORDS or interactive_id == "act_menu":'''
)

with open('services/shakti_bot_service.py', 'w', encoding='utf-8') as f:
    f.write(content)
