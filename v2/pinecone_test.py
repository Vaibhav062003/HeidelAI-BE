from sentence_transformers import SentenceTransformer
from pinecone import Pinecone, ServerlessSpec

model = SentenceTransformer('all-MiniLM-L6-v2')


def initialize_pinecone():
    pc = Pinecone(api_key="84641b3f-7be7-4a15-aa8e-91552da0ca04")

    index_name = "egenie-products"

    if index_name not in pc.list_indexes().names():
        pc.create_index(
        name=index_name,
        dimension=384,
        metric="cosine",
        spec=ServerlessSpec(
            cloud='aws',
            region='us-east-1'
        )
    )
    
    index = pc.Index(index_name)
    return index

index = initialize_pinecone()
    

query = "laptop"

products = ["489", "699", "688"] 

query_vector = model.encode(query).tolist()

search_results = index.query(
    vector=query_vector,
    filter={"product_id": {"$in": products}},
    top_k=5,
    include_values=False,
    include_metadata=True
)

product_data = [result['metadata'] for result in search_results['matches']]

print("product:", product_data)