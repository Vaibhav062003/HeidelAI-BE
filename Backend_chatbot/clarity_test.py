import requests, os, json
from dotenv import load_dotenv

load_dotenv()

CLARITY_TOKEN = os.getenv("CLARITY_TOKEN")

params = {"numOfDays": "3", "dimension1": "PopularPages"}
headers = {
   "Authorization": f"Bearer {CLARITY_TOKEN}",
   "Content-type": "application/json"
}
response = requests.get("https://www.clarity.ms/export-data/api/v1/project-live-insights",
   params=params,
   headers=headers,
)

print(response.status_code)
print(response.text)

def format_clarity_metrics(data):
    
    result = ""

    # Iterate through the data and format it
    for metric in data:
        metric_name = metric.get('metricName', 'Unknown Metric')
        information = metric.get('information', [{}])

        if len(information) == 1:
            information = information[0]
            if metric_name == "DeadClickCount":
                result += f"**Dead Click Count:**\n"
                result += f"Sessions with Dead Clicks Percentage: {information.get('sessionsWithMetricPercentage', 'N/A')}%\n"
                result += f"Sessions without Dead Clicks Percentage: {information.get('sessionsWithoutMetricPercentage', 'N/A')}%\n"
                result += f"Total Dead Clicks: {information.get('subTotal', 'N/A')}\n\n"

            elif metric_name == "ScrollDepth":
                result += f"**Average Scroll Depth:**\n"
                result += f"Average Scroll Depth: {information.get('averageScrollDepth', 'N/A')}%\n\n"

            elif metric_name == "Traffic":
                result += f"**Traffic Metrics:**\n"
                result += f"Total Session Count: {information.get('totalSessionCount', 'N/A')}\n"
                result += f"Total Bot Session Count: {information.get('totalBotSessionCount', 'N/A')}\n"
                result += f"Distinct User Count: {information.get('distinctUserCount', 'N/A')}\n"
                result += f"Pages Per Session Percentage: {information.get('pagesPerSessionPercentage', 'N/A'):.2f}%\n\n"

            elif metric_name == "EngagementTime":
                result += f"**Engagement Time:**\n"
                result += f"Total Time: {information.get('totalTime', 'N/A')} seconds\n"
                result += f"Active Time: {information.get('activeTime', 'N/A')} seconds\n\n"

        elif len(information) > 1:

            if metric_name == "PageTitle":
                result += f"**Page Titles:**\n"
                for entry in information:
                    if isinstance(entry, dict):
                        result += f"Page Title: {entry.get('name', 'N/A')} - Sessions Count: {entry.get('sessionsCount', 'N/A')}\n"
                    else:
                        result += f"Unexpected data format in PageTitle.\n"
                result += "\n"

            elif metric_name == "PopularPages":
                result += f"**Popular Pages:**\n"
                for entry in information:
                    if isinstance(entry, dict):
                        result += f"URL: {entry.get('url', 'N/A')} - Visits Count: {entry.get('visitsCount', 'N/A')}\n"
                    else:
                        result += f"Unexpected data format in PopularPages.\n"
                result += "\n"

    return result

data = response.json()  

formatted_string = format_clarity_metrics(data)
print(formatted_string)


# def get_clarity_metrics(parsed_data):
#     """
#     Parses Clarity metrics and returns a summary string.

#     Args:
#         parsed_data (list): A list of dictionaries containing Clarity metrics data.

#     Returns:
#         str: A summary of all metrics combined into a single string for the LLM to reference.
#     """

#     # Initialize variables for storing the results
#     total_scroll_depth = 0
#     scroll_count = 0
#     engagement_time_total = 0
#     engagement_active_time_total = 0
#     popular_pages = []
#     dead_clicks = 0
#     error_clicks = 0

#     # Iterate over parsed data
#     for metric in parsed_data:
#         if metric['metricName'] == 'ScrollDepth':
#             # Average Scroll Depth
#             total_scroll_depth += metric['information'][0].get('averageScrollDepth', 0)
#             scroll_count += 1
#         elif metric['metricName'] == 'EngagementTime':
#             # Engagement Time
#             engagement_time_total += int(metric['information'][0].get('totalTime', 0))
#             engagement_active_time_total += int(metric['information'][0].get('activeTime', 0))
#         elif metric['metricName'] == 'PopularPages':
#             # Popular Pages
#             for page in metric['information']:
#                 popular_pages.append(f"Page: {page['url']}, Visits: {page['visitsCount']}")
#         elif metric['metricName'] == 'DeadClickCount':
#             # Dead Clicks
#             dead_clicks += int(metric['information'][0].get('subTotal', 0))
#         elif metric['metricName'] == 'ErrorClickCount':
#             # Error Clicks
#             error_clicks += int(metric['information'][0].get('subTotal', 0))

#     # Calculate average scroll depth
#     avg_scroll_depth = total_scroll_depth / scroll_count if scroll_count > 0 else 0

#     # Format and combine results
#     result = (
#         f"Clarity Metrics Summary:\n\n"
#         f"Average Scroll Depth: {avg_scroll_depth:.2f} pixels\n"
#         f"Total Engagement Time: {engagement_time_total} seconds\n"
#         f"Total Active Engagement Time: {engagement_active_time_total} seconds\n\n"
#         f"Popular Pages:\n" + "\n".join(popular_pages) + "\n\n"
#         f"Dead Clicks: {dead_clicks}\n"
#         f"Error Clicks: {error_clicks}\n"
#     )

#     return result

# # Example usage with parsed data
# parsed_data = [{'metricName': 'DeadClickCount', 'information': [{'sessionsCount': '80', 'sessionsWithMetricPercentage': 2.5, 'sessionsWithoutMetricPercentage': 97.5, 'pagesViews': '2', 'subTotal': '2'}]},
#                {'metricName': 'ScrollDepth', 'information': [{'averageScrollDepth': 35.16}]},
#                {'metricName': 'EngagementTime', 'information': [{'totalTime': '192', 'activeTime': '19'}]},
#                {'metricName': 'PopularPages', 'information': [{'url': 'https://egeniestores.com/', 'visitsCount': '53'}]},
#                {'metricName': 'ErrorClickCount', 'information': [{'sessionsCount': '80', 'sessionsWithMetricPercentage': 0, 'sessionsWithoutMetricPercentage': 100, 'pagesViews': '0', 'subTotal': '0'}]}
#               ]

# # Call the function
# print(get_clarity_metrics(parsed_data))