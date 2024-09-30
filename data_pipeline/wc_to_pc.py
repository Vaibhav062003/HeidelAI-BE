from woocommerce import API
import pandas as pd
import re, time, requests, csv, os, json
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from sentence_transformers import SentenceTransformer
from pinecone import Pinecone, ServerlessSpec

load_dotenv()

CONSUMER_KEY = os.getenv("CONSUMER_KEY")
CONSUMER_SECRET = os.getenv("CONSUMER_SECRET")

wcapi = API(
    url="https://egeniestores.com",
    consumer_key=CONSUMER_KEY,
    consumer_secret=CONSUMER_SECRET,
    wp_api=True,
    version="wc/v3"
)

def fetch_all_products():
    products = []
    page = 1
    per_page = 21  # Maximum number of products per request
    while True:
        try:
            response = wcapi.get("products", params={"per_page": per_page, "page": page})
            print(response.status_code)
            productss = response.json()
            products.extend(productss)
        
            if len(productss) < per_page:
                break  # If fewer products than per_page are returned, this is the last page

            page += 1  # Increment to the next page
        except requests.exceptions.ReadTimeout:
            print(f"Read timeout error on page {page}. Retrying...")
            continue  # Retry fetching the same page

    return products

products = fetch_all_products()
print(f"Fetched {len(products)} products.")
first_product = json.dumps(products[7], indent=2)
print(first_product)
# attributes = [
#     'id', 'name', 'slug', 'permalink', 'description', 'sku', 'price',
#     'regular_price', 'sale_price', 'on_sale', 'purchasable', 'stock_quantity', 'categories', 'images'
# ]
# # <table border=""3""

# Create a list to hold the extracted data
extracted_data = []
# print(products)

for product in products:
    if(not product['purchasable']):
       continue
    
    html_content = product['description'] 
    
    soup = BeautifulSoup(html_content, 'html.parser')

    for table in soup.find_all('table', attrs={'border': '3'}):
        table.decompose()
    
    cleaned_text = soup.get_text(separator=' ').strip()

    cleaned_text = re.sub(r'[^a-zA-Z0-9\s]', '', cleaned_text)

    # Remove extra whitespace
    cleaned_text = re.sub(r'\s+', ' ', cleaned_text).strip()

    product['description'] = cleaned_text

    # product_data = {attr: product.get(attr, '') for attr in attributes}
    # Extract category names
    # product_data['stock_quantity'] = int(product_data.get('stock_quantity', 0) or 0)

    # if 'categories' in product_data:
    #     product_data['categories'] = ', '.join([category['name'] for category in product['categories']])

    # product_data['images'] = product['images'][0]['src'] if product['images'] else None
    # extracted_data.append(product_data)
    # Extract category names

# # Convert the list of dictionaries to a pandas DataFrame
# df = pd.DataFrame(extracted_data)

# # Save the DataFrame to a CSV file
# df.to_csv('products.csv', index=False)

# print("Data has been saved to csv.")

# model = SentenceTransformer('all-MiniLM-L6-v2')

# def upsert_data_to_pinecone():
#     pc = Pinecone(api_key="84641b3f-7be7-4a15-aa8e-91552da0ca04")

#     index_name = "products-test"

#     if index_name in pc.list_indexes().names():
#         print("Index already exists. Deleting...")
#         pc.delete_index(index_name)
#         print("Creating index...")
#         pc.create_index(
#             name=index_name,
#             dimension=384,
#             metric="cosine",
#             spec=ServerlessSpec(
#                 cloud='aws',
#                 region='us-east-1'
#             )
#         )

#     if index_name not in pc.list_indexes().names():
#         print("Index does not exist. Creating...")
#         pc.create_index(
#             name=index_name,
#             dimension=384,
#             metric="cosine",
#             spec=ServerlessSpec(
#                 cloud='aws',
#                 region='us-east-1'
#             )
#         )
    
#     index = pc.Index(index_name)

#     device = [825, 801, 762, 746, 498 ,489 ,470 ,464 ,407, 295, 221, 201, 25]
#     custom = [386, 335, 175, 168]
#     accessories = [813, 729, 699 ,688 ,640 ,640 ,624 ,609 ,601 ,575 ,564, 209]

#     products = []
#     with open('products.csv', mode='r', encoding='utf-8') as file:
#         csv_reader = csv.DictReader(file)
#         for row in csv_reader:
#             products.append(row)

#     print(f"Creating vector embeddings for {len(products)} products and upserting data...")
#     for product in products:
#         combined_description = f"{product['name']} {product['description']} {product['slug']} {product['price']} {product['categories']}"

#         vector = model.encode(combined_description)  

#         metadata = {
#             'product_id': int(product['id']),
#             'name': product['name'],
#             'slug': product['slug'],
#             'permalink': product['permalink'],
#             'description': product['description'],
#             'price': int(product['price']),
#             'regular_price': product['regular_price'],
#             'sale_price': product['sale_price'],
#             'on_sale': product['on_sale'],
#             'purchasable': product['purchasable'],
#             'stock_quantity': int(product['stock_quantity']),
#             'images': product['images']
#             'images': product['images']
#         }

#         if int(product['id']) in device:
#             metadata['category'] = 'device'
#         elif int(product['id']) in custom:
#             metadata['category'] = 'custom'
#         elif int(product['id']) in accessories:
#             metadata['category'] = 'accessories'

        # index.upsert([(str(product['id']), vector, metadata)])

#     print("Upserted data into pinecone.")
#     return index

# index = upsert_data_to_pinecone()

# print("Job done.")