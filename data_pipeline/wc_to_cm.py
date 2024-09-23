import os, json, requests, re, time
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
    
print(f"{len(cleaned_products)} products fetched")
# temp = json.dumps(cleaned_products[2], indent=2)

# filename = "output2.json"

# with open(filename, "w") as file:
#     file.write(temp)

# print(f"Data successfully saved to {filename}")

for i, product in enumerate(products):

    if len(product['images']) == 0  or not product['price']:
        continue

    if (not product['stock_quantity'] or product['stock_quantity'] <= 0):
        availability = "out of stock"
    else:
        availability = "in stock"

    if (product['sale_price'] and product['regular_price']):
        # print(f"Product {i}: ")
        # print("id: ", product['id'])
        # print("title: ", product['name'])
        # print("price: ", f"{product['regular_price']} INR")
        # print("sale_price: ", f"{product['sale_price']} INR")
        # # print("image: ", image)
        # print("images: ", ', '.join(image['src'] for image in product['images']))
        # print("link: ", product['permalink'])
        # print("availability: ", availability)
        # print("\n\n")
        batch_data = {
            "access_token": access_token,
            "item_type": "PRODUCT_ITEM",
            "requests": [
                {
                    "method": "CREATE",
                    "data": {
                        "id": str(product['id']), 
                        "brand": "Egenie",
                        "title": product['name'][:65],
                        "description": product['description'],
                        "price": f"{product['regular_price']} INR",
                        "sale_price": f"{product['sale_price']} INR",
                        "image_link": product['images'][0]['src'],
                        "additional_image_link" : ', '.join(image['src'] for image in product['images']),
                        "link": product['permalink'],
                        "availability": availability,
                        
                        "google_product_category" : "222",
                        "origin_country" : "IN",
                        "condition": "new",
                        "status" : "active"
                    }
                },
            ]
        }
    
    else:
        # print("All prices available")
        # print(f"Product {i}: ")
        # print("id: ", product['id'])
        # print("title: ", product['name'])
        # print("price: ", f"{product['price']} INR")
        # # print("image: ", image)
        # print("images: ", ', '.join(image['src'] for image in product['images']))
        # print("link: ", product['permalink'])
        # print("availability: ", availability)
        # print("\n\n")

        batch_data = {
            "access_token": access_token,
            "item_type": "PRODUCT_ITEM",
            "requests": [
                {
                    "method": "CREATE",
                    "data": {
                        "id": str(product['id']), 
                        "brand": "Egenie",
                        "title": product['name'][:65],
                        "description": product['description'],
                        "price": f"{product['price']} INR",
                        "image_link": product['images'][0]['src'],
                        "additional_image_link" : ', '.join(image['src'] for image in product['images']),
                        "link": product['permalink'],
                        "availability": availability,
                        
                        "google_product_category" : "222",
                        "origin_country" : "IN",
                        "condition": "new",
                        "status" : "active"
                    }
                },
            ]
        }

    upload_response = requests.post(upload_url, json=batch_data)

    # Handle upload response
    print(f"Insert Response for Product ID {product['id']} - Status Code:", upload_response.status_code)
    upload_json = upload_response.json()
    print("insert Response JSON:", upload_json)
    print("\n\n")
    # Check if the insert was successful and get the handle
    if upload_response.status_code == 200:
        handle = upload_json.get('handles', [None])[0]
        if handle:
            print(f"insertion request for Product ID {product['id']} submitted successfully.")
            print("\n\n")
            # Check status for insertion
            def check_status(handle):
                url = f"https://graph.facebook.com/v20.0/{catalog_id}/check_batch_request_status"
                params = {
                    'access_token': access_token,
                    'handle': handle
                }
                response = requests.get(url, params=params)
                return response

            # Check status every 10 seconds until finished
            while True:
                status_response = check_status(handle)
                print(f"Check Status Response for Product ID {product['id']} - Status Code: {status_response.status_code}")
                status_json = status_response.json()
                print(f"Check Status Response JSON:", status_json)
                print("\n\n")
                
                if status_response.status_code == 200:
                    status_data = status_json.get('data', [])
                    if status_data:
                        status = status_data[0].get('status')
                        if status == 'finished':
                            print(f"Batch processing for Product ID {product['id']} completed successfully.")
                            print("\n\n")
                            break
                        elif status in ['started', 'scheduled', 'dispatched']:
                            print(f"Batch processing for Product ID {product['id']} is still in progress.")
                            print("\n\n")
                        elif status == 'canceled':
                            print(f"Batch processing for Product ID {product['id']} was canceled.")
                            print("\n\n")
                            break
                        elif status == 'error':
                            print(f"An error occurred during batch processing for Product ID {product['id']}.")
                            print("\n\n")
                            break
                    else:
                        print("No status data available.")
                else:
                    print("Error checking status.")
                time.sleep(8)
    else:
        print(f"Failed to submit insertion request for Product ID {product['id']}.")
        print("\n\n")

# upload_response = requests.post(upload_url, json=batch_data)
# print("Upload Response Status Code:", upload_response.status_code)
# upload_json = upload_response.json()