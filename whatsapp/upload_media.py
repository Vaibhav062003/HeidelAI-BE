import requests
import os
from dotenv import load_dotenv

# Step 1: Download the image from the URL
def download_image(image_url, save_path):
    response = requests.get(image_url)
    if response.status_code == 200:
        with open(save_path, 'wb') as file:
            file.write(response.content)
        print(f"Image downloaded successfully to {save_path}")
    else:
        print(f"Failed to download image. Status code: {response.status_code}")

# Step 2: Upload the image to the Meta/WhatsApp API
def upload_image_to_whatsapp(phone_number_id, access_token, file_path):
    url = f"https://graph.facebook.com/v20.0/{phone_number_id}/media"
    headers = {
        "Authorization": f"Bearer {access_token}"
    }
    data = {
        'messaging_product': 'whatsapp'
    }
    files = {
        'file': (os.path.basename(file_path), open(file_path, 'rb'), 'image/jpeg')  # Ensure correct MIME type
    }

    response = requests.post(url, headers=headers, data=data, files=files)

    if response.status_code == 200:
        media_id = response.json().get('id')
        print(f"Image uploaded successfully. Media ID: {media_id}")
        return media_id
    else:
        print(f"Failed to upload image. Status code: {response.status_code}, Response: {response.text}")
        return None

load_dotenv()
# Replace these values with your actual data
image_url = "https://egeniestores.com/wp-content/uploads/2024/04/61xf2zTjBeL._SL1500_.jpg"
save_path = "downloaded_image.jpg"  # Local path where the image will be saved
access_token = os.getenv("ACCESS_TOKEN")
phone_number_id = os.getenv("PHONE_NUMBER_ID")  # Your WhatsApp business phone number ID

# Download the image
download_image(image_url, save_path)

# Upload the image to WhatsApp API
media_id = upload_image_to_whatsapp(phone_number_id, access_token, save_path)

if media_id:
    print(f"Media ID of the uploaded image: {media_id}")
