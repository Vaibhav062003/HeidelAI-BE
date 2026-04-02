# === Third-party Modules ===
import asyncio
from typing import Dict, Optional
from datetime import datetime, timezone

# === Internal Modules ===
from database import get_mongo_db

from loguru import logger

db = get_mongo_db()


# === Helper Functions for Collection Access ===
def _automation_triggers_collection():
    """Get the global automation triggers collection"""
    return db["automation_triggers"]


def _conversations_collection(org_id: str):
    """Get the conversations collection for an organization"""
    return db[f"conversations_{org_id}"]


# === Main Trigger Handler ===
async def check_and_trigger_automations(
    org_id: str,
    platform: str,
    event_type: str,
    trigger_data: Dict
):
    """
    Check if any automations should be triggered for this event
    
    Args:
        org_id: Organization ID
        platform: Platform (instagram, whatsapp)
        event_type: Type of event (message_received, story_reply, etc)
        trigger_data: Event data containing customer info, message, etc
    """
    try:
        # Find matching triggers
        triggers = await _find_matching_triggers(org_id, platform, event_type, trigger_data)
        
        if not triggers:
            logger.debug(f"No matching triggers found for {platform} {event_type} in org {org_id}")
            return
        
        logger.info(f"Found {len(triggers)} matching triggers for {platform} {event_type} in org {org_id}")
        
        from services.automation_execution import execute_automation_flow
        
        # Execute each matching flow
        for trigger in triggers:
            flow_id = trigger.get("flow_id")
            
            try:
                # Execute flow asynchronously (in production, use Celery)
                asyncio.create_task(
                    execute_automation_flow(
                        org_id=org_id,
                        flow_id=flow_id,
                        trigger_type=event_type,
                        trigger_data=trigger_data
                    )
                )
                
                # Update last_triggered_at
                await _update_trigger_timestamp(trigger.get("trigger_id"))
                
            except Exception as e:
                logger.error(f"Error executing flow {flow_id}: {str(e)}")
        
    except Exception as e:
        logger.error(f"Error in check_and_trigger_automations: {str(e)}")


async def _find_matching_triggers(
    org_id: str,
    platform: str,
    event_type: str,
    trigger_data: Dict
) -> list:
    """
    Find all triggers that match the given event
    
    Returns:
        List of matching trigger documents
    """
    triggers_collection = _automation_triggers_collection()
    
    # Map event_type to trigger_type
    trigger_type = _event_to_trigger_type(event_type, platform)
    
    # Query for matching triggers
    query = {
        "org_id": org_id,
        "status": "active",
        "trigger_type": trigger_type
    }
    
    # Add platform filter if applicable
    if platform:
        query["platform"] = platform
    
    # Find all potential triggers
    triggers = await asyncio.to_thread(lambda: list(triggers_collection.find(query)))
    
    # Filter triggers based on additional criteria
    matching_triggers = []
    
    for trigger in triggers:
        if _trigger_matches_criteria(trigger, trigger_data):
            matching_triggers.append(trigger)
    
    return matching_triggers


def _event_to_trigger_type(event_type: str, platform: str) -> str:
    """Map event type to trigger type"""
    event_map = {
        "message_received": f"{platform}_message_received" if platform == "whatsapp" else "instagram_dm_received",
        "story_mention": "instagram_story_mention",
        "story_reply": "instagram_story_reply",
        "post_comment": "instagram_comment",
        "tag_added": "contact_tag_added",
        "tag_removed": "contact_tag_removed"
    }
    
    return event_map.get(event_type, event_type)


def _trigger_matches_criteria(trigger: Dict, trigger_data: Dict) -> bool:
    """
    Check if trigger matches additional criteria (keyword, post_id, etc)
    
    Returns:
        True if trigger matches, False otherwise
    """
    filters = trigger.get("filters", {})
    
    # Check keyword filter (supports pipe-separated keywords: "price|info|help")
    keyword_filter = filters.get("keyword", "")
    if keyword_filter:
        message_text = trigger_data.get("message_text", "").lower()
        
        # Split by pipe and check if ANY keyword matches
        keywords = [k.strip().lower() for k in keyword_filter.split("|") if k.strip()]
        
        if keywords:
            # Check if ANY keyword is in the message
            keyword_found = any(keyword in message_text for keyword in keywords)
            
            if not keyword_found:
                logger.debug(f"❌ Keyword match failed. Looking for: {keywords}, Got: '{message_text}'")
                return False
            else:
                logger.success(f"✅ Keyword matched! Found one of {keywords} in '{message_text}'")
    
    # Check post_id filter (CRITICAL for Instagram comments)
    post_id = filters.get("post_id")
    if post_id:
        trigger_post_id = trigger_data.get("post_id")
        if trigger_post_id != post_id:
            logger.debug(f"❌ Post ID mismatch. Expected: {post_id}, Got: {trigger_post_id}")
            return False
        else:
            logger.success(f"✅ Post ID matched: {post_id}")
    
    # Check story_id filter
    story_id = filters.get("story_id")
    if story_id:
        if trigger_data.get("story_id") != story_id:
            logger.debug(f"❌ Story ID mismatch")
            return False
    
    # Check tag filter
    tag = filters.get("tag")
    if tag:
        if trigger_data.get("tag") != tag:
            logger.debug(f"❌ Tag mismatch")
            return False
    
    logger.success(f"✅ All trigger criteria matched!")
    return True


async def _update_trigger_timestamp(trigger_id: str):
    """Update last_triggered_at timestamp for a trigger"""
    triggers_collection = _automation_triggers_collection()

    await asyncio.to_thread(
        triggers_collection.update_one,
        {"trigger_id": trigger_id},
        {"$set": {"last_triggered_at": datetime.now(timezone.utc)}},
    )


# === Tag Event Trigger (called from contacts service) ===
async def trigger_tag_automation(org_id: str, conversation_id: str, tag: str, action: str):
    """
    Trigger automations when a tag is added or removed
    
    Args:
        org_id: Organization ID
        conversation_id: Conversation ID
        tag: Tag name that was added/removed
        action: "added" or "removed"
    """
    try:
        # Get conversation details
        conversations_collection = _conversations_collection(org_id)
        conversation = await asyncio.to_thread(lambda: conversations_collection.find_one({"conversation_id": conversation_id}))
        
        if not conversation:
            logger.warning(f"Conversation {conversation_id} not found")
            return
        
        # Determine event type
        event_type = "tag_added" if action == "added" else "tag_removed"
        
        # Build trigger data
        trigger_data = {
            "conversation_id": conversation_id,
            "customer_id": conversation.get("customer_id"),
            "customer_name": conversation.get("customer_name"),
            "platform": conversation.get("platform"),
            "tag": tag,
            "action": action
        }
        
        # Check and trigger automations
        await check_and_trigger_automations(
            org_id=org_id,
            platform=conversation.get("platform"),
            event_type=event_type,
            trigger_data=trigger_data
        )
        
    except Exception as e:
        logger.error(f"Error triggering tag automation: {str(e)}")


def _check_keyword_match(trigger_config: Dict, trigger_data: Dict) -> bool:
    """
    Check if message contains any of the trigger keywords
    
    Supports pipe-separated keywords: "price|info|help"
    """
    keyword_filter = trigger_config.get("keyword", "")
    
    if not keyword_filter:
        return True  # No keyword filter = always match
    
    message_text = trigger_data.get("message_text", "").lower()
    
    # Split by pipe and check if ANY keyword matches
    keywords = [k.strip().lower() for k in keyword_filter.split("|") if k.strip()]
    
    if not keywords:
        return True
    
    # Check if ANY keyword is in the message
    for keyword in keywords:
        if keyword in message_text:
            logger.success(f"✅ Keyword matched: '{keyword}' found in '{message_text}'")
            return True
    
    logger.debug(f"❌ No keyword match. Looking for: {keywords}, Got: '{message_text}'")
    return False