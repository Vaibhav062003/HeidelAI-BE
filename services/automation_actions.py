# === Third-party Modules ===
from typing import Dict, Any

# === Internal Modules ===
from database import get_mongo_db
from services.ig_service import send_ig_message, send_ig_media_message
from services.wa_service import send_whatsapp_message
from core.logger import get_logger

logger = get_logger(__name__)
db = get_mongo_db()


async def execute_action_node(context, node_id: str, node_type: str, config: Dict):
    """
    Execute an action node based on its type
    
    CRITICAL: This function handles ALL action nodes from the flow.
    Config structure must match flowExport.ts output.
    """
    try:
        trigger_data = context.trigger_data
        
        # CRITICAL FIX: Check config.action_type for action nodes
        action_type = config.get("action_type")
        
        logger.info(f"🔍 Executing node {node_id}: type={node_type}, action_type={action_type}")
        
        # ====================================================================
        # INSTAGRAM-SPECIFIC ACTIONS
        # ====================================================================
        
        if action_type == "reply_to_comment":
            await execute_reply_to_comment(context, node_id, config, trigger_data)
            
        elif action_type == "send_dm":
            await execute_send_dm(context, node_id, config, trigger_data)
        
        # ====================================================================
        # CONTACT MANAGEMENT ACTIONS
        # ====================================================================
        
        elif action_type == "add_tag":
            await execute_add_tag(context, node_id, config, trigger_data)
            
        elif action_type == "remove_tag":
            await execute_remove_tag(context, node_id, config, trigger_data)
            
        elif action_type == "set_field":
            await execute_set_custom_field(context, node_id, config, trigger_data)
        
        # ====================================================================
        # EXTERNAL INTEGRATIONS
        # ====================================================================
        
        elif action_type == "api":
            await execute_http_request(context, node_id, config, trigger_data)
        
        # ====================================================================
        # MESSAGE ACTIONS (type='message' nodes)
        # ====================================================================
        
        elif node_type == "message":
            # This is a "Send Text" or "Send Image" node
            await execute_message_node(context, node_id, config, trigger_data)
        
        # ====================================================================
        # DELAY ACTIONS
        # ====================================================================
        
        elif node_type == "smart_delay":
            await _execute_delay_node(context, node_id, config)
        
        # ====================================================================
        # UNKNOWN ACTION TYPE
        # ====================================================================
        
        else:
            logger.warning(f"⚠️ Unknown action type: node_type={node_type}, action_type={action_type}")
            context.log(
                node_id, 
                node_type, 
                "unknown", 
                f"Unknown action type: {node_type}/{action_type}", 
                success=False
            )
            
    except Exception as e:
        context.log(node_id, node_type, "error", f"Error executing action: {str(e)}", success=False)
        logger.error(f"❌ Error executing action node {node_id}: {str(e)}", exc_info=True)


# ============================================================================
# MESSAGE NODE EXECUTION (Send Text/Image/etc)
# ============================================================================

async def execute_message_node(context, node_id: str, config: Dict, trigger_data: Dict):
    """
    Execute a message node (Send Text, Send Image, etc)
    
    Config structure from frontend:
    {
        "content": {
            "text": "Hello",
            "media_url": null,
            "buttons": []
        }
    }
    """
    platform = trigger_data.get("platform")
    content = config.get("content", {})
    
    text = content.get("text", "")
    media_url = content.get("media_url")
    buttons = content.get("buttons", [])
    
    # Replace variables in text
    text = replace_variables(text, context.variables)
    
    context.log(node_id, "message", "executing", f"Sending message: {text[:50]}...")
    
    try:
        if platform == "instagram":
            # Get recipient from trigger data
            customer_id = trigger_data.get("customer_id") or trigger_data.get("commenter_id")
            instagram_id = trigger_data.get("platform_id")
            
            if not customer_id or not instagram_id:
                context.log(node_id, "message", "error", "Missing customer_id or instagram_id", success=False)
                return
            
            # Send text message
            if media_url:
                result = await send_ig_media_message(
                    recipient=customer_id,
                    media_url=media_url,
                    media_type="image",
                    instagram_id=instagram_id,
                    mode="reply",
                    org_id=context.org_id
                )
            else:
                result = await send_ig_message(
                    id=customer_id,
                    message_text=text,
                    instagram_id=instagram_id,
                    mode="reply",
                    org_id=context.org_id
                )
            
            if result and result.get("message_id"):
                context.log(node_id, "message", "success", "Message sent successfully", success=True)
                context.set_variable("last_message_id", result.get("message_id"))
            else:
                context.log(node_id, "message", "error", "Failed to send message", success=False)
                
        elif platform == "whatsapp":
            conversation_id = trigger_data.get("conversation_id")
            whatsapp_id = trigger_data.get("platform_id")
            
            if not conversation_id or not whatsapp_id:
                context.log(node_id, "message", "error", "Missing conversation_id or whatsapp_id", success=False)
                return
            
            result = await send_whatsapp_message(
                message={
                    "conversation_id": conversation_id,
                    "content": text,
                    "sender_id": whatsapp_id
                },
                org_id=context.org_id,
                mode="reply"
            )
            
            if result and result.get("message_id"):
                context.log(node_id, "message", "success", "WhatsApp message sent", success=True)
            else:
                context.log(node_id, "message", "error", "Failed to send WhatsApp message", success=False)
                
    except Exception as e:
        context.log(node_id, "message", "error", f"Error sending message: {str(e)}", success=False)


# ============================================================================
# INSTAGRAM COMMENT ACTIONS
# ============================================================================

async def execute_reply_to_comment(context, node_id: str, config: Dict, trigger_data: Dict):
    """
    Reply to an Instagram comment
    
    Config structure:
    {
        "action_type": "reply_to_comment",
        "text": "Thank you!"
    }
    """
    from services.ig_service import reply_to_comment
    
    comment_id = trigger_data.get("comment_id")
    ig_account_id = trigger_data.get("platform_id")
    
    if not comment_id or not ig_account_id:
        context.log(node_id, "reply_to_comment", "error", 
                   "Missing comment_id or ig_account_id", success=False)
        return
    
    reply_text = config.get("text", "")
    reply_text = replace_variables(reply_text, context.variables)
    
    context.log(node_id, "reply_to_comment", "executing", 
               f"Replying to comment {comment_id}")
    
    try:
        await reply_to_comment(comment_id, reply_text, ig_account_id)
        context.log(node_id, "reply_to_comment", "success", 
                   "Comment reply sent successfully", success=True)
        context.set_variable("reply_sent", True)
    except Exception as e:
        context.log(node_id, "reply_to_comment", "error", 
                   f"Failed to reply: {str(e)}", success=False)


async def execute_send_dm(context, node_id: str, config: Dict, trigger_data: Dict):
    """
    Send a DM to the user who triggered the automation
    
    Config structure:
    {
        "action_type": "send_dm",
        "text": "Hi! Check this out",
        "link_url": "https://example.com",  # optional
        "button_title": "View Offer"  # optional
    }
    """
    from services.ig_service import send_private_reply
    
    comment_id = trigger_data.get("comment_id")
    ig_account_id = trigger_data.get("platform_id")
    
    if not comment_id or not ig_account_id:
        context.log(node_id, "send_dm", "error", 
                   "Missing comment_id or ig_account_id", success=False)
        return
    
    message_text = config.get("text", "")
    message_text = replace_variables(message_text, context.variables)
    
    link_url = config.get("link_url")
    button_title = config.get("button_title", "View Link")
    
    # Validate link_url if provided
    has_valid_link = (
        link_url and 
        isinstance(link_url, str) and 
        link_url.strip() and 
        (link_url.startswith('http://') or link_url.startswith('https://'))
    )
    
    context.log(node_id, "send_dm", "executing", 
               f"Sending DM via comment {comment_id}")
    
    try:
        result = await send_private_reply(
            comment_id=comment_id,
            ig_account_id=ig_account_id,
            message_text=message_text,
            link_url=link_url if has_valid_link else None,
            button_title=button_title if has_valid_link else None
        )
        
        if result:
            context.log(node_id, "send_dm", "success", 
                       "DM sent successfully", success=True)
            context.set_variable("dm_sent", True)
        else:
            context.log(node_id, "send_dm", "error", 
                       "Failed to send DM", success=False)
    except Exception as e:
        context.log(node_id, "send_dm", "error", 
                   f"Failed to send DM: {str(e)}", success=False)


# ============================================================================
# CONTACT MANAGEMENT ACTIONS
# ============================================================================

async def execute_add_tag(context, node_id: str, config: Dict, trigger_data: Dict):
    """
    Add a tag to a contact
    
    Config structure:
    {
        "action_type": "add_tag",
        "tag_name": "VIP"
    }
    """
    tag_name = config.get("tag_name", "")
    conversation_id = trigger_data.get("conversation_id")
    
    if not tag_name or not conversation_id:
        context.log(node_id, "add_tag", "error", "Missing tag_name or conversation_id", success=False)
        return
    
    # Replace variables in tag
    tag_name = replace_variables(tag_name, context.variables)
    
    context.log(node_id, "add_tag", "executing", f"Adding tag '{tag_name}'")
    
    try:
        contacts_collection = db[f"contacts_{context.org_id}"]
        
        result = contacts_collection.update_one(
            {"conversation_id": conversation_id},
            {"$addToSet": {"categories": tag_name}}
        )
        
        if result.modified_count > 0:
            context.log(node_id, "add_tag", "success", f"Tag '{tag_name}' added", success=True)
        else:
            context.log(node_id, "add_tag", "info", f"Tag '{tag_name}' already present", success=True)
            
    except Exception as e:
        context.log(node_id, "add_tag", "error", f"Error adding tag: {str(e)}", success=False)


async def execute_remove_tag(context, node_id: str, config: Dict, trigger_data: Dict):
    """Remove a tag from a contact"""
    tag_name = config.get("tag_name", "")
    conversation_id = trigger_data.get("conversation_id")
    
    if not tag_name or not conversation_id:
        context.log(node_id, "remove_tag", "error", "Missing tag_name or conversation_id", success=False)
        return
    
    tag_name = replace_variables(tag_name, context.variables)
    
    context.log(node_id, "remove_tag", "executing", f"Removing tag '{tag_name}'")
    
    try:
        contacts_collection = db[f"contacts_{context.org_id}"]
        
        result = contacts_collection.update_one(
            {"conversation_id": conversation_id},
            {"$pull": {"categories": tag_name}}
        )
        
        if result.modified_count > 0:
            context.log(node_id, "remove_tag", "success", f"Tag '{tag_name}' removed", success=True)
        else:
            context.log(node_id, "remove_tag", "info", f"Tag '{tag_name}' not present", success=True)
            
    except Exception as e:
        context.log(node_id, "remove_tag", "error", f"Error removing tag: {str(e)}", success=False)


async def execute_set_custom_field(context, node_id: str, config: Dict, trigger_data: Dict):
    """
    Set a custom field on a contact
    
    Config structure:
    {
        "action_type": "set_field",
        "field_name": "email",
        "field_value": "{{trigger_data.email}}"
    }
    """
    field_name = config.get("field_name", "")
    field_value = config.get("field_value", "")
    conversation_id = trigger_data.get("conversation_id")
    
    if not field_name or not conversation_id:
        context.log(node_id, "set_field", "error", "Missing field_name or conversation_id", success=False)
        return
    
    # Replace variables
    field_name = replace_variables(field_name, context.variables)
    field_value = replace_variables(field_value, context.variables)
    
    context.log(node_id, "set_field", "executing", f"Setting {field_name} = {field_value}")
    
    try:
        contacts_collection = db[f"contacts_{context.org_id}"]
        
        result = contacts_collection.update_one(
            {"conversation_id": conversation_id},
            {"$set": {f"custom_fields.{field_name}": field_value}}
        )
        
        if result.matched_count > 0:
            context.log(node_id, "set_field", "success", f"Field '{field_name}' set", success=True)
        else:
            context.log(node_id, "set_field", "warning", "Contact not found", success=False)
            
    except Exception as e:
        context.log(node_id, "set_field", "error", f"Error setting field: {str(e)}", success=False)


# ============================================================================
# EXTERNAL INTEGRATIONS
# ============================================================================

async def execute_http_request(context, node_id: str, config: Dict, trigger_data: Dict):
    """
    Make an HTTP request (webhook)
    
    Config structure:
    {
        "action_type": "api",
        "api_url": "https://example.com/webhook",
        "api_method": "POST",
        "api_body": "{\"event\": \"comment\"}"
    }
    """
    import httpx
    import json as json_lib
    
    url = config.get("api_url", "")
    method = config.get("api_method", "POST").upper()
    body_str = config.get("api_body", "{}")
    
    if not url:
        context.log(node_id, "api", "error", "Missing api_url", success=False)
        return
    
    # Replace variables in URL and body
    url = replace_variables(url, context.variables)
    body_str = replace_variables(body_str, context.variables)
    
    # Parse body
    try:
        body = json_lib.loads(body_str) if body_str else {}
    except:
        body = {}
    
    context.log(node_id, "api", "executing", f"{method} {url}")
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if method == "GET":
                response = await client.get(url)
            elif method == "POST":
                response = await client.post(url, json=body)
            elif method == "PUT":
                response = await client.put(url, json=body)
            elif method == "DELETE":
                response = await client.delete(url)
            else:
                context.log(node_id, "api", "error", f"Unsupported method: {method}", success=False)
                return
            
            # Store response
            context.set_variable("http_status_code", response.status_code)
            
            try:
                context.set_variable("http_response", response.json())
            except:
                context.set_variable("http_response", response.text)
            
            if 200 <= response.status_code < 300:
                context.log(node_id, "api", "success", f"HTTP {response.status_code}", success=True)
            else:
                context.log(node_id, "api", "warning", f"HTTP {response.status_code}", success=True)
                
    except Exception as e:
        context.log(node_id, "api", "error", f"HTTP request failed: {str(e)}", success=False)


# ============================================================================
# DELAY ACTION
# ============================================================================

async def _execute_delay_node(context, node_id: str, config: Dict):
    """
    Execute a delay node
    
    Config structure:
    {
        "amount": 5,
        "unit": "minutes"
    }
    """
    import asyncio
    
    amount = config.get("amount", 1)
    unit = config.get("unit", "seconds")
    
    # Convert to seconds
    if unit == "minutes":
        delay_seconds = amount * 60
    elif unit == "hours":
        delay_seconds = amount * 3600
    elif unit == "days":
        delay_seconds = amount * 86400
    else:
        delay_seconds = amount
    
    # Cap delay at 5 minutes for safety
    delay_seconds = min(delay_seconds, 300)
    
    context.log(node_id, "delay", "executing", f"Delaying {delay_seconds}s", success=True)
    
    await asyncio.sleep(delay_seconds)
    
    context.log(node_id, "delay", "success", "Delay completed", success=True)


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def replace_variables(text: str, variables: Dict[str, Any]) -> str:
    """
    Replace {{variable}} placeholders in text with actual values
    
    Example:
        text = "Hello {{customer_name}}!"
        variables = {"customer_name": "John"}
        result = "Hello John!"
    """
    import re
    
    if not isinstance(text, str):
        return str(text)
    
    # Find all {{variable}} patterns
    pattern = r'\{\{([^}]+)\}\}'
    
    def replacer(match):
        var_name = match.group(1).strip()
        
        # Support nested access like {{trigger_data.customer_name}}
        parts = var_name.split('.')
        value = variables
        
        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return match.group(0)  # Return original if can't access
        
        return str(value) if value is not None else match.group(0)
    
    return re.sub(pattern, replacer, text)