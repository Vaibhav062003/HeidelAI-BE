import json, requests
products =  [{'id': 201.0, 'name': 'Apple MacBook Pro (14-Inch, Apple M3 Pro Chip, 11-Core CPU, 14-Core GPU, 18GB Unified Memory, 512GB SSD, macOS, Space Black) MRX33HN/A', 'price': 185000.0, 'permalink': 'https://egeniestores.com/product/apple-macbook-pro-2023/', 'image_link': 'https://egeniestores.com/wp-content/uploads/2024/04/7ebc8d65-d257-42f0-85f4-cdd1993033cd23021130-300x300-1.jpg', 'on_sale': 'True', 'regular_price': '199000'}, {'id': 498.0, 'name': 'HP Victus Gaming Laptop, (Intel Core i5-12450H, RTX 2050, 15.6-inch (39.6 cm), 50W TGP, FHD, IPS, 144Hz, 16GB DDR4, 1TB SSD, Backlit KB,MSO, Blue, 2.37 kg), fa1145TX', 'price': 64000.0, 'permalink': 'https://egeniestores.com/product/hp-victus-gaming-laptop-2/', 'image_link': 'https://egeniestores.com/wp-content/uploads/2024/04/71atfTZpTaL._SL1500_.jpg', 'on_sale': 'True', 'regular_price': '84838'}, {'id': 801.0, 'name': 'Lenovo LOQ 2024 Intel Core i5-13450HX 15.6" (39.6cm) 144Hz 300Nits FHD Gaming Laptop (24GB/512GB SSD/Win 11/NVIDIA RTX 3050 6GB Graphics/100% sRGB/Office 21/3 Month Game Pass/Grey/2.4Kg)', 'price': 77700.0, 'permalink': 'https://egeniestores.com/product/lenovo-loq-2024-intel-core-i5-13450hx/', 'image_link': 'https://egeniestores.com/wp-content/uploads/2024/06/81sI6re6MPL._AC_SL1500_.jpg', 'on_sale': 'True', 'regular_price': '104990'}, {'id': 470.0, 'name': 'Lenovo Legion 5 Pro AMD Ryzen 7 5800H QHD Gaming Laptop(16GB/1TB SSD/RTX 3060 6GB /165Hz/Win11/Office 2021/RGB Backlit/3mnth Xbox Game Pass/Storm Grey/2.45Kg), 82JQ00JCIN', 'price': 86700.0, 'permalink': 'https://egeniestores.com/product/lenovo-legion-5-pro/', 'image_link': 'https://egeniestores.com/wp-content/uploads/2024/04/81MQOV40FbL._SL1500_.jpg', 'on_sale': 'False', 'regular_price': '86700'}, {'id': 489.0, 'name': 'Dell Inspiron 3520 Laptop (Intel Core i5-1235U , 8GB RAM, 512GB SSD, 15.6" (39.62Cms) FHD Display, Win 11, MSO\'21,  Black, 1.65Kg)', 'price': 48500.0, 'permalink': 'https://egeniestores.com/product/dell-inspiron-3520/', 'image_link': 'https://egeniestores.com/wp-content/uploads/2024/04/61gYJrnKu3L._SL1080_.jpg', 'on_sale': 'True', 'regular_price': '56999'}]
elements = []
for product in products:
    product_id = int(product['id'])

    url1 = f"https://graph.facebook.com/v20.0/759103595212051/products"
    params1 = {
        "filter" : json.dumps({
            "retailer_id" : { "eq" :  str(product_id) }
        }),
        "fields" : "id",
        "access_token" : "EAAHPjYclZCTEBOZBBeB3wiqbYp7zdrkn71W6sivXPMNBlG9UrTauLHIfDEJADdJBGWu5rQdE4DV5dSfLBZCuPoEfNwyYXOtKmy2dpvPlavmz1UMmZCMuz1MtrzrEEhsrK6ZB3XbAq6gotRtxxxUANMpDUlZCZCsxKz41EMPIMZBtEel84YrOLcXXZAg6rXYBcKjA0qoWZCLkky855div0ZD"
    }

    response = requests.get(url1, params=params1)
    response = response.json()
    # print(response)
    if len(response['data']) == 0:
        print("Product not found")
        # await send_ig_message(id, "Sorry, we could not find the product details. Please try again.")
        # return

    product_cat_id = response['data'][0]['id']
    print(product_cat_id)
    elements.append({ "id" : product_cat_id })
# elements.append({ "id" : product_cat_id })
print(elements)