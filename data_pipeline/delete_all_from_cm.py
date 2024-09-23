import requests
import os
import time
from dotenv import load_dotenv

load_dotenv()

# Meta access token and catalog ID
access_token = os.getenv("ACCESS_TOKEN")
catalog_id = os.getenv("CATALOG_ID")
version = os.getenv("VERSION")

# Function to fetch all products and return their retailer IDs
def fetch_all_products():
    products = []
    url = f"https://graph.facebook.com/{version}/{catalog_id}/products?limit=25"  # Adjust version as needed

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    while url:
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            print(f"Error fetching products: {response.status_code}, {response.json()}")
            break
        
        data = response.json()
        products.extend(data['data'])  # Add the products from this batch

        # Check for next page
        url = data.get('paging', {}).get('next')  # Update URL to next page or None

    return products

# Fetch all products
all_products = fetch_all_products()

# Extract retailer IDs
retailer_ids = [product.get('retailer_id') for product in all_products]

# Meta API endpoint for product deletion
upload_url = f"https://graph.facebook.com/v20.0/{catalog_id}/items_batch"

# Loop through each retailer ID to delete products
for retailer_id in retailer_ids:

    batch_data = {
        "access_token": access_token,
        "item_type": "PRODUCT_ITEM",
        "requests": [
            {
                "method": "DELETE",
                "data" : {
                    "id": retailer_id
                }
            }
        ]
    }

    # Delete products
    upload_response = requests.post(upload_url, json=batch_data)

    # Handle upload response
    print(f"Delete Response for Retailer ID {retailer_id} - Status Code:", upload_response.status_code)
    upload_json = upload_response.json()
    print("Delete Response JSON:", upload_json)

    # Check if the delete was successful and get the handle
    if upload_response.status_code == 200:
        handle = upload_json.get('handles', [None])[0]
        if handle:
            print(f"Deletion request for Retailer ID {retailer_id} submitted successfully.")
            # Check status for deletion
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
                print(f"Check Status Response for Retailer ID {retailer_id} - Status Code: {status_response.status_code}")
                status_json = status_response.json()
                print(f"Check Status Response JSON:", status_json)
                
                if status_response.status_code == 200:
                    status_data = status_json.get('data', [])
                    if status_data:
                        status = status_data[0].get('status')
                        if status == 'finished':
                            print(f"Batch processing for Retailer ID {retailer_id} completed successfully.")
                            break
                        elif status in ['started', 'scheduled', 'dispatched']:
                            print(f"Batch processing for Retailer ID {retailer_id} is still in progress.")
                        elif status == 'canceled':
                            print(f"Batch processing for Retailer ID {retailer_id} was canceled.")
                            break
                        elif status == 'error':
                            print(f"An error occurred during batch processing for Retailer ID {retailer_id}.")
                            break
                    else:
                        print("No status data available.")
                else:
                    print("Error checking status.")
                time.sleep(10)
    else:
        print(f"Failed to submit deletion request for Retailer ID {retailer_id}.")
