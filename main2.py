import csv, json
from sentence_transformers import SentenceTransformer
from pinecone import Pinecone, ServerlessSpec

model = SentenceTransformer('sentence-transformers/all-MiniLM-L12-v2')

"""
from sentence_transformers import SentenceTransformer
import torch
device = 'cuda' if torch.cuda.is_available() else 'cpu'

model = SentenceTransformer('sentence-transformers/all-MiniLM-L12-v2').to(device)
"""

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

# pip install "numpy<2.0"
# D:\ML\Egenie\egenie-chatbot\chatbotvenv\Lib\site-packages\torch\lib\

# index.upsert(
#     vectors=[
#         {"id": "vec1", "values": [1.0, 1.5]},
#         {"id": "vec2", "values": [2.0, 1.0]},
#         {"id": "vec3", "values": [0.1, 3.0]},
#     ],
#     namespace="ns1"
# )


# products = []
# with open('data.csv', mode='r', encoding='utf-8') as file:
#     csv_reader = csv.DictReader(file)
#     for row in csv_reader:
#         products.append(row)

# # Encode and upsert data into Pinecone
# for product in products:
#     vector = model.encode(product['description'])
#     index.upsert([(str(product['id']), vector)])


# for product in products:
#     combined_text = ' '.join(f"{key}: {value}" for key, value in product.items())
#     vector = model.encode(combined_text)
#     index.upsert([(product['product_id'], vector)])


"""
data = [
    {"id": "vec1", "text": "Apple is a popular fruit known for its sweetness and crisp texture."},
    {"id": "vec2", "text": "The tech company Apple is known for its innovative products like the iPhone."},
    {"id": "vec3", "text": "Many people enjoy eating apples as a healthy snack."},
    {"id": "vec4", "text": "Apple Inc. has revolutionized the tech industry with its sleek designs and user-friendly interfaces."},
    {"id": "vec5", "text": "An apple a day keeps the doctor away, as the saying goes."},
]
sentences = [x["text"] for x in data]
embeddings =  model.encode(sentences)  

vectors = []
for d, e in zip(data, embeddings):
    vectors.append({
        "id": d['id'],
        "values": e,
        "metadata": {'text': d['text']}
    })

index.upsert(
    vectors=vectors,
    namespace="ns1"
)
import time 


query = "Tell me about the tech company known as Apple"

query_embedding = model.encode(query).tolist()
print(query_embedding)

results = index.query(
    namespace="ns1",
    vector=query_embedding,
    top_k=3,
    include_values=False,
    include_metadata=True
)

print(results)
"""

# def search_products(query, top_k=10):

#     query_vector = model.encode(query)

#     results = index.query(query_vector, top_k=top_k, include_values=True)
    
#     product_ids = [match['id'] for match in results['matches']]

#     matching_products = [product for product in products if product['product_id'] in product_ids]
    
#     return matching_products

# query = "gaming laptop with high-quality graphics"
# matching_products = search_products(query)

# print(json.dumps(matching_products, indent=4))


    
