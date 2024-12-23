from sentence_transformers import SentenceTransformer
from pinecone import Pinecone

def initialize_pinecone(api_key, index_name):
    # Initialize Pinecone instance
    pc = Pinecone(api_key=api_key)
    
    # Check if the index exists
    if index_name not in pc.list_indexes().names():
        raise ValueError(f"Index '{index_name}' does not exist. Please create it in the Pinecone console.")
    
    # Connect to the index
    index = pc.Index(index_name)
    return index

def vector_search(index, query, model, filters=None, top_k=10):
    # Encode the query into a vector
    query_vector = model.encode(query).tolist()

    # Perform the search in the Pinecone index
    search_results = index.query(
        vector=query_vector,
        filter=filters,
        top_k=top_k,
        include_values=False,
        include_metadata=True
    )

    # Return the search results
    return search_results

# Main script
api_key = "84641b3f-7be7-4a15-aa8e-91552da0ca04"
index_name = "products-test"

# Initialize Pinecone index
index = initialize_pinecone(api_key, index_name)

# Initialize SentenceTransformer model
model = SentenceTransformer('all-MiniLM-L6-v2')

# Input query
query = "Mac mini"

# Define filters (customize as needed)
filters = {
    "category": "electronics",
    "stock_quantity": {"$gt": 0}
}

# Perform vector search
search_results = vector_search(index, query, model, filters, top_k=10)

# # Display the results
# for result in search_results["matches"]:
#     print(f"ID: {result['id']}, Score: {result['score']}, Metadata: {result['metadata']}")

# product_data = [
#     result['metadata']
#     for result in search_results['matches']  
# ]

print(search_results)