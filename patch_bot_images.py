import re

with open('services/shakti_bot_service.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Add _send_image function
image_func = '''
async def _send_image(org_id: str, wa_id: str, conversation_id: str, media_url: str):
    """Send an image to the user."""
    try:
        from services.wa_service import send_wa_media_message
        customer_phone = _extract_phone(conversation_id)
        if not customer_phone: return
        await send_wa_media_message(
            recipient_id=customer_phone,
            media_url=media_url,
            media_type="image",
            wa_id=wa_id,
            content=None,
            org_id=org_id,
            temp_id=None,
            mode="reply",
            context_type=None,
            context=None,
            conversation_id=conversation_id,
        )
    except Exception as e:
        logger.error(f"[SHAKTI] Failed to send image to {conversation_id}: {e}")

'''
content = content.replace('async def _send_welcome(org_id, wa_id, cid):', image_func + 'async def _send_welcome(org_id, wa_id, cid):')


# 2. Add image send to _send_main_menu specifically
main_menu_target = '''async def _send_main_menu(org_id, wa_id, cid, name):
    greeting = f"Nice to meet you, {name}!" if name and name.lower() != "skip" else "Welcome!"
    payload = {'''

main_menu_inject = '''async def _send_main_menu(org_id, wa_id, cid, name):
    greeting = f"Nice to meet you, {name}!" if name and name.lower() != "skip" else "Welcome!"
    
    img_url = "https://raw.githubusercontent.com/egenie4u/chatbot_prod/dev/assets/shakti_welcome.jpg"
    await _send_image(org_id, wa_id, cid, img_url)
    await asyncio.sleep(1)
    
    payload = {'''

content = content.replace(main_menu_target, main_menu_inject)


# 3. Remove prices
# Remove from _send_models
content = content.replace(
    '{"id": f"model_{b[\'id\']}", "title": b["title"], "description": f"From {b[\'price\']} ex-showroom"}',
    '{"id": f"model_{b[\'id\']}", "title": b["title"], "description": "Explore features and book a ride"}'
)

# Remove from _send_model_detail
old_detail_body = 'f"*{bike[\'title\']}*\\nStarts from {bike[\'price\']} (ex-showroom)\\n\\nExact on-road price depends on your city."'
new_detail_body = 'f"*{bike[\'title\']}*\\n\\nGreat choice! What would you like to do next?"'
content = content.replace(old_detail_body, new_detail_body)

with open('services/shakti_bot_service.py', 'w', encoding='utf-8') as f:
    f.write(content)
