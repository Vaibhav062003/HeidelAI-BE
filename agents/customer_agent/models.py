from pydantic import BaseModel, Field
from typing import List, Optional

class Product(BaseModel):
    """Data model for a single product."""
    name: str = Field(..., description="The name or title of the product.")
    permalink: str = Field(..., description="The direct URL to the product page.")
    price: int = Field(..., description="The price of the product")
    images: str = Field(..., description="URL of the product's image.")
    # rating: Optional[str] = Field(None, description="The product's rating, if available.") 

class ProductResponse(BaseModel):
    """
    Use this model when you have found products to display to the user.
    """
    summary: str = Field(..., description="A brief, one-sentence summary of the findings. For example, 'I found several iphone 16 for you.'")
    products: List[Product] = Field(..., description="A list of the products found.")
