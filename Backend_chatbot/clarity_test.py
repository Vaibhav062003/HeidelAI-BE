import requests, os, json
from dotenv import load_dotenv

load_dotenv()

CLARITY_TOKEN = os.getenv("CLARITY_TOKEN")

params = {"numOfDays": "3", "dimension1": "PageTitle"}
headers = {
   "Authorization": f"Bearer {CLARITY_TOKEN}",
   "Content-type": "application/json"
}
response = requests.get("https://www.clarity.ms/export-data/api/v1/project-live-insights",
   params=params,
   headers=headers,
)

if response.status_code == 200:
    data = response.json()  
    
    product_interaction = {}

    for metric in data:
        metric_name = metric.get("metricName")
        information = metric.get("information", [])

        if metric_name == "PageTitle":
            # PageTitle gives us product interaction info
            for info in information:
                product_name = info.get("name", "Unknown Product")
                sessions_count = int(info.get("sessionsCount", 0))

                # List of keywords to skip
                skip_keywords = ["Home", "Archives", "Laptops", "PreBuilt", "Components", "Mobile", "Peripherals"]

               
                if any(keyword in product_name for keyword in skip_keywords):
                    continue  

                # Store session count for products
                if product_name not in product_interaction:
                    product_interaction[product_name] = 0
                product_interaction[product_name] += sessions_count


    if product_interaction:
        most_popular_product = max(product_interaction, key=product_interaction.get)
        print(f"\nMost popular product: {most_popular_product}")
        print(f"Total sessions: {product_interaction[most_popular_product]}")
    else:
        print("No product interaction data found.")
    
else:
    print(f"Error: {response.status_code}")
    print(response.text)