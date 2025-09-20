query: str = Field(
..., description="The primary product mentioned in the conversation. This should be a concise phrase representing the latest product the user is inquiring about, ensuring it reflects the most relevant item if multiple products have been discussed. If no product is mentioned, use 'None'."
)

categories: List[str] = Field(
...,
description=(
    "A list of product categories the user is interested in. "
    "Allowed values (case-insensitive): "
    "['Custom Built Pc', 'Accessories', 'Gaming Laptop', 'Business Laptop']."
),
example=["Gaming Laptop", "Accessories"]
)

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

@router.get("/product-recommendations/{conversation_id}")
async def get_product_recommendations(conversation_id: str, user: CurrentUser):
    """
    This endpoint retrieves product recommendations for a specific conversation.
    """
    try:
        org_id = user.org_id 
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID missing in user context")

        conversation_col_name = f"conversations_{org_id}"
        conversations_collection = db[conversation_col_name]

        try:
            conversation = conversations_collection.find_one({"id": conversation_id})
        except PyMongoError as e:
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

        if conversation:
            products = conversation.get("product_recommendations")
            if products:
                return {"status": "success", "data": products}
            return {"status": "error", "message": "No product recommendations found"}

        return {"status": "error", "message": "No conversation found"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")