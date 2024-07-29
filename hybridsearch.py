from langchain_community.retrievers import PineconeHybridSearchRetriever
from pinecone import Pinecone, ServerlessSpec
from langchain_huggingface import HuggingFaceEmbeddings
from pinecone_text.sparse import BM25Encoder
import csv, time

pc = Pinecone(api_key="84641b3f-7be7-4a15-aa8e-91552da0ca04")

index_name = "egenie-products-hybrid"


# if index_name not in pc.list_indexes().names():
#     pc.create_index(
#     name=index_name,
#     dimension=384,
#     metric="dotproduct",
#     spec=ServerlessSpec(
#         cloud='aws',
#         region='us-east-1'
#     )
# )

index = pc.Index(index_name)

embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
# print(embeddings)

bm25_encoder = BM25Encoder().default()

# products = []
# with open('data.csv', mode='r', encoding='utf-8') as file:
#     csv_reader = csv.DictReader(file)
#     for row in csv_reader:
#         temp = ""
#         for field in row:
#             temp = temp + " " + str(field) + " : " + row[field] + ", "
#         products.append(temp)
            
        
# print(products)
# t = [
#     "1",
#     "2"
# ]
# print(products[0])
# print(t[0])
# print(type(products))
    

# for product in products:
#     bm25_encoder.fit(str(product))
#     bm25_encoder.dump("bm25_values.json")

bm25_encoder = BM25Encoder().load("bm25_values.json")

retriever = PineconeHybridSearchRetriever(
    top_k=10,
    embeddings=embeddings, 
    sparse_encoder=bm25_encoder, 
    index=index
)

# retriever.add_texts(products)
tic = time.perf_counter()
values =  retriever.invoke("gaming laptop")
toc = time.perf_counter()
print(values)
print(f"Response took {toc - tic:0.4f} seconds")