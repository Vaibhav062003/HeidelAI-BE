import requests
import os
from dotenv import load_dotenv

load_dotenv()

# Set up your app ID, access token, and image URL
APP_ID = os.getenv("APP_ID")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
image_url = "https://egeniestores.com/wp-content/uploads/2024/04/61xf2zTjBeL._SL1500_.jpg"
file_name = "image.jpg"

# Download the image
response = requests.get(image_url)
if response.status_code == 200:
    with open(file_name, 'wb') as file:
        file.write(response.content)

    # Get file size and MIME type
    file_size = os.path.getsize(file_name)
    mime_type = "image/jpg"

    # Step 1: Start the upload session
    upload_url = f"https://graph.facebook.com/v20.0/{APP_ID}/uploads"
    params = {
        "file_name": file_name,
        "file_length": file_size,
        "file_type": mime_type,
        "access_token": ACCESS_TOKEN
    }
    
    session_response = requests.post(upload_url, params=params)
    upload_session_id = session_response.json()["id"]

    # Step 2: Upload the image
    upload_endpoint = f"https://graph.facebook.com/v20.0/{upload_session_id}"
    headers = {
        "Authorization": f"OAuth {ACCESS_TOKEN}",
        "file_offset": "0"
    }
    
    with open(file_name, 'rb') as file_data:
        upload_response = requests.post(upload_endpoint, headers=headers, data=file_data)

    # Get the uploaded file handle
    uploaded_file_handle = upload_response.json()["h"]
    print(f"Uploaded file handle: {uploaded_file_handle}")

else:
    print("Failed to download the image.")
