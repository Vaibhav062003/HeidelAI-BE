from langchain_core.tools import tool
from .models import ProductResponse, Product

@tool
def search_tool(query: str) -> ProductResponse:
    """
    This tool retrieves product information based on the user's query.
    """
    return ProductResponse(
        summary=f"I found several products for '{query}'.",
        products=[
            Product(
                title="Example Product 1",
                link="https://example.com/product1",
                price="$19.99",
                image="https://example.com/image1.jpg",
                rating="4.5"
            ),
            Product(
                title="Example Product 2",
                link="https://example.com/product2",
                price="$29.99",
                image="https://example.com/image2.jpg",
                rating="4.0"
            )
        ]
    )