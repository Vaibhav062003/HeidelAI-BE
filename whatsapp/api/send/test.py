import json
import os
import requests
from dotenv import load_dotenv

load_dotenv()

ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERSION = os.getenv("VERSION")
sahil = "+919511907785"
vipul = "+918408062973"
rounak = "+917972510629"
anushka = "+918815785354"

def send_product(sender, payload):
    product = {}

    if payload == "MRX33HN/A_201":
        product = {
            "name" : "Apple MacBook Pro (14-Inch, Apple M3 Pro Chip, 11-Core CPU, 14-Core GPU, 18GB Unified Memory, 512GB SSD, macOS, Space Black) ",
            "price" : "₹185,000",
            "regular_price":"₹199,000",
            "discount" : "7%",
            "stock_quantity" : "3"

        }
    elif payload == "82JQ00JCIN_470":
        product = {
            "name" : "Lenovo Legion 5 Pro AMD Ryzen 7 5800H QHD Gaming Laptop(16GB/1TB SSD/RTX 3060 6GB /165Hz/Win11/Office 2021/RGB Backlit/3mnth Xbox Game Pass/Storm Grey/2.45Kg), 82JQ00JCIN",
            "price" : "₹86,700",
            "regular_price":"₹199,000",
            "discount" : "7%",
            "stock_quantity" : "3"

        }
    elif payload == "FA506ICB-HN075W_3780":
        product = {
            "name" : "ASUS TUF Gaming A15 FHD 144Hz, AMD Ryzen 7 4800H, RTX 3050 4GB(16GB/512GB SSD/Win 11//90 Whr./1 Year McAfee/Graphite Black",
            "price" : "₹185,000",
            "regular_price":"₹199,000",
            "discount" : "7%",
            "stock_quantity" : "3"

        }
    elif payload == "wc_post_id_801":
        product = {
            "name" : "Lenovo LOQ 2024 Intel Core i5-13450HX 15.6 (39.6cm) 144Hz 300Nits FHD Gaming Laptop (24GB/512GB SSD/Win 11/NVIDIA RTX 3050 6GB Graphics/100% sRGB/Office 21/3 Month Game Pass/Grey/2.4Kg)",
            "price" : "₹185,000",
            "regular_price":"₹199,000",
            "discount" : "7%",
            "stock_quantity" : "3"

        }
    elif payload == "wc_post_id_407":
        product = {
            "name" : "SONY PLAYSTATION 5 Disc Version (825 GB)",
            "price" : "₹185,000",
            "regular_price":"₹199,000",
            "discount" : "7%",
            "stock_quantity" : "3"

        }
    url = f"https://graph.facebook.com/{VERSION}/{PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization" : "Bearer " + ACCESS_TOKEN,
        "Content-Type" : "application/json",
    }
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": sender,
        "type": "template",
        "template": {
            "name": "single_product_v2",
            "language": {
            "code": "en_US"
            },
            "components": [
            {
                "type": "header",
                "parameters": [
                {
                    "type": "product",
                    "product": {
                    "product_retailer_id": payload,
                    "catalog_id": "759103595212051"
                    }
                }
                ]
            },
            {
                "type": "body",
                "parameters": [
                    {
                    "type":"text",
                    "text": f"*{product['name']}*"
                    }
                ]
            }
            ]
        }
    }

    response = requests.post(url, headers=headers, json=data)
    print(f"Response: {response.status_code}, {response.json()}")
    print(f"Message sent to {sender} with product {product['name'][:10]}")


send_product(rounak,"82JQ00JCIN_470" )
