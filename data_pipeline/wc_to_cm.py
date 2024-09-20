import os, json, requests, re
from bs4 import BeautifulSoup
from woocommerce import API
from dotenv import load_dotenv

load_dotenv()

access_token = os.getenv("ACCESS_TOKEN")
catalog_id = os.getenv("CATALOG_ID")
CONSUMER_KEY = os.getenv("CONSUMER_KEY")
CONSUMER_SECRET = os.getenv("CONSUMER_SECRET")

wcapi = API(
    url="https://egeniestores.com",
    consumer_key=CONSUMER_KEY,
    consumer_secret=CONSUMER_SECRET,
    wp_api=True,
    version="wc/v3"
)

upload_url = f"https://graph.facebook.com/v20.0/{catalog_id}/items_batch"

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

cleaned_products = []

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

    # Extract category names
    product['stock_quantity'] = int(product.get('stock_quantity', 0) or 0)

    if 'categories' in product:
        product['categories'] = ', '.join([category['name'] for category in product['categories']])

    # product['images'] = product['images'][0]['src'] if product['images'] else None
    cleaned_products.append(product)
    

# print(cleaned_products)
temp = json.dumps(cleaned_products[2], indent=2)

filename = "output2.json"

with open(filename, "w") as file:
    file.write(temp)

print(f"Data successfully saved to {filename}")

# for product in products:

#     batch_data = {
#         "access_token": access_token,
#         "item_type": "PRODUCT_ITEM",
#         "requests": [
#             {
#                 "method": "CREATE",
#                 "data": {
#                     "id": product['id'],
#                     "brand": "Sony",
#                     "title": "SONY PLAYSTATION 5 Disc Version (825 GB)",
#                     "description": "Brace yourself for a whole new world of possibilities as the PS5 unleashes unparalleled power and innovation. Get ready to elevate your gaming experience to new heights with the Sony PlayStation 5 Console. Unleash the power of play, immerse yourself in stunning visuals, and explore a world of endless possibilities. With the PS5, gaming will never be the same again.",
#                     "price": "54990 INR",
#                     "sale_price": "51500 INR",
#                     "image_link": "https://egeniestores.com/wp-content/uploads/2024/04/original-imaghyykrhvewh4y.jpeg",
#                     "link": "https://egeniestores.com/product/sony-playstation-5-disc-version-825-gb/",
#                     "availability": "in stock",

#                     "origin_country" : "IN",
#                     "condition": "new",
#                     "status" : "active"
#                 }
#             },
#         ]
#     }

# upload_response = requests.post(upload_url, json=batch_data)
# print("Upload Response Status Code:", upload_response.status_code)
# upload_json = upload_response.json()