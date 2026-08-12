"""
Database Migration Script for Transfer System

Adds:
- version field to all conversations (for optimistic locking)
- Indexes for transfer system collections
- Creates conversation_mentions collection
- Creates conversation_audit_logs collection

Run: python migrate_transfer_system.py
"""

import asyncio
from database import get_async_mongo_db
from loguru import logger


async def migrate_transfer_system():
    """Run migration for transfer system."""
    
    db = get_async_mongo_db()
    
    logger.info("Starting transfer system migration...")
    
    # 1. Add version field to all conversations
    logger.info("Adding version field to conversations...")
    
    # Get all org conversation collections
    collection_names = await db.list_collection_names()
    conversation_collections = [c for c in collection_names if c.startswith("conversations_")]
    
    for coll_name in conversation_collections:
        collection = db[coll_name]
        
        # Add version = 0 to all conversations without version
        result = await collection.update_many(
            {"version": {"$exists": False}},
            {"$set": {"version": 0}}
        )
        
        logger.info(f"  {coll_name}: Added version to {result.modified_count} conversations")
        
        # Create index on org conversations for assigned_agent
        await collection.create_index([("assigned_agent_id", 1)])
        await collection.create_index([("status", 1)])
        logger.info(f"  {coll_name}: Created indexes")
    
    # 2. Create indexes on users collection
    logger.info("Creating users collection indexes...")
    users_col = db.users
    await users_col.create_index([("org_id", 1), ("status", 1)])
    await users_col.create_index([("username", 1), ("org_id", 1)])
    logger.info("  users: Created org and username indexes")
    
    # 3. Create conversation_mentions collection with indexes
    logger.info("Creating conversation_mentions collection...")
    mentions_col = db.conversation_mentions
    await mentions_col.create_index([("to_agent_id", 1), ("status", 1)])
    await mentions_col.create_index([("org_id", 1)])
    await mentions_col.create_index([("conversation_id", 1)])
    logger.info("  conversation_mentions: Created indexes")
    
    # 4. Create conversation_audit_logs collection with indexes
    logger.info("Creating conversation_audit_logs collection...")
    audit_col = db.conversation_audit_logs
    await audit_col.create_index([("org_id", 1), ("conversation_id", 1)])
    await audit_col.create_index([("performed_by", 1), ("timestamp", -1)])
    logger.info("  conversation_audit_logs: Created indexes")
    
    logger.success("✅ Transfer system migration complete!")
    logger.info(f"Modified {len(conversation_collections)} conversation collections")
    logger.info("Created indexes for users, mentions, and audit logs")


if __name__ == "__main__":
    asyncio.run(migrate_transfer_system())
