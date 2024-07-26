import csv, json
from sentence_transformers import SentenceTransformer
from pinecone import Pinecone, ServerlessSpec

model = SentenceTransformer('sentence-transformers/all-MiniLM-L12-v2')

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


products = []
with open('data.csv', mode='r', encoding='utf-8') as file:
    csv_reader = csv.DictReader(file)
    for row in csv_reader:
        products.append(row)


# Encode and upsert data into Pinecone (Needs to be done only once)
# for product in products:
#     vector = model.encode(str(product))
#     index.upsert([(str(product['product_id']), vector)])


"""
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

"""

query = "hair straightner"

# query_embedding = model.encode(query).tolist()
# print(query_embedding)

# results = index.query(
#     vector=query_embedding,
#     top_k=3,
#     include_values=True
# )

# print(results)


def search_products(query, top_k=3):

    query_vector = model.encode(query).tolist()

    results = index.query(
        vector=query_vector,
        top_k=top_k,
        include_values=True
    )
    
    product_ids = [match['id'] for match in results['matches']]

    matching_products = [product for product in products if product['product_id'] in product_ids]
    
    return matching_products

matching_products = search_products(query)

for product in matching_products:
    print(product['product_id'])

# print(json.dumps(matching_products, indent=4))
# print(matching_products)

    
