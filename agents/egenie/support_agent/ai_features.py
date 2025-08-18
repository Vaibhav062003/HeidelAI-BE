from .product_suggestions import get_products
from .generate_insights import process_and_update_conversation
from core.managers import manager2

async def analyze_conversation_messages(org_id: str, conversation_id: str) -> None:
    """
    Analyzes conversation messages at specific intervals (5, 15, 25, etc.)
    
    Args:
        conversation_id: The ID of the conversation
        message_count: Current count of messages in the conversation
    """
        
    try:
        # Analyze the conversation
        process_and_update_conversation(org_id, conversation_id)

        await manager2.broadcast({
            "type": "summary_updated",
            "conversation_id": conversation_id,
        })
        
    except Exception as e:
        print(f"Error analyzing conversation messages: {e}")
        

async def update_product_recommendations(org_id: str, conversation_id: str) -> None:
    try:
        print("Getting product recommendations...")

        result = await get_products(org_id, conversation_id)
        print("Product recommendations:", result)
        if result:
            await manager2.broadcast({
                "type": "products_updated",
                "conversation_id": conversation_id
            })
        
    except Exception as e:
        print(f"Error analyzing conversation messages: {e}")
