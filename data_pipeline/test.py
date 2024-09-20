import requests
import os
import time
from dotenv import load_dotenv

load_dotenv()

# Meta access token and catalog ID
access_token = os.getenv("ACCESS_TOKEN")
catalog_id = os.getenv("CATALOG_ID")

# Meta API endpoint for product uploads
upload_url = f"https://graph.facebook.com/v20.0/{catalog_id}/items_batch"
# The product data in the required format
batch_data = {
    "access_token": access_token,
    "item_type": "PRODUCT_ITEM",
    "requests": [
        {
            "method": "CREATE",
            "data": {
                "id": "407",
                "title": "SONY PLAYSTATION 5 Disc Version (825 GB)",
                "description": "Brace yourself for a whole new world of possibilities as the PS5 unleashes unparalleled power and innovation. Get ready to elevate your gaming experience to new heights with the Sony PlayStation 5 Console. Unleash the power of play, immerse yourself in stunning visuals, and explore a world of endless possibilities. With the PS5, gaming will never be the same again.",
                "price": "54990 INR",
                "sale_price": "51500 INR",
                "image_link": "https://egeniestores.com/wp-content/uploads/2024/04/original-imaghyykrhvewh4y.jpeg",
                "link": "https://egeniestores.com/product/sony-playstation-5-disc-version-825-gb/",
                "availability": "in stock",

                "brand": "Egenie",
                "origin_country" : "IN",
                "condition": "new",
                "status" : "active"
            }
        },
    ]
}

# Upload products
upload_response = requests.post(upload_url, json=batch_data)

# Handle upload response
print("Upload Response Status Code:", upload_response.status_code)
upload_json = upload_response.json()
print("Upload Response JSON:", upload_json)
print()
print()

# Check if the upload was successful and get the handle
if upload_response.status_code == 200:
    handle = upload_json['handles'][0]
    print(f"Product uploaded successfully")
    print()
    print()

    # Function to check the status
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
        print(f"Check Status Response Status Code: {status_response.status_code}")
        status_json = status_response.json()
        print(f"Check Status Response JSON: {status_json}")
        print()
        print()
        
        if status_response.status_code == 200:
            status_data = status_json.get('data', [])
            if status_data:
                status = status_data[0].get('status')
                if status == 'finished':
                    print("Batch processing completed successfully.")

                    break
                elif status == 'started':
                    print("Batch processing is still in progress.")
                    print()
                    print()
                elif status == 'scheduled':
                    print("Batch processing is scheduled.")
                elif status == 'dispatched':
                    print("Batch processing has been dispatched.")
                elif status == 'canceled':
                    print("Batch processing was canceled.")
                    break
                elif status == 'error':
                    print("An error occurred during batch processing.")
                    break
                else:
                    print("Unknown status:", status)
            else:
                print("No status data available.")
        else:
            print("Error checking status.")

        time.sleep(10)
else:
    print("Failed to upload product.")


"""
        {
            "method": "DELETE",
            "data": {
                "id": "retailer-1"  # Replace with the actual retailer_id you want to delete
            }
        },
        {
            "method": "UPDATE",
            "data": {
                "id": "unique_id_2",
                "availability": "out of stock",
                "description": "Updated product description"
            }
        }
"""
