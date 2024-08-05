import csv
from sentence_transformers import SentenceTransformer
from pinecone import Pinecone, ServerlessSpec

model = SentenceTransformer('sentence-transformers/all-MiniLM-L12-v2')

pc = Pinecone(api_key="84641b3f-7be7-4a15-aa8e-91552da0ca04")

index_name = "egenie-products"
# pc.delete_index(index_name)

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

for product in products:
    vector = model.encode(str(product))  # Use description for embedding
    metadata = {
        'product_id': product['product_id'],
        'description': product['description'],
        'name': product['name'],
        'slug': product['slug'],
        'permalink': product['permalink'],
        'sku': product['sku'],
        'price': product['price'],
        'regular_price': product['regular_price'],
        'sale_price': product['sale_price'],
        'on_sale': product['on_sale'],
        'purchasable': product['purchasable'],
        'stock_quantity': product['stock_quantity'],
        'categories': product['categories']
    }
    index.upsert([(str(product['product_id']), vector, metadata)])
