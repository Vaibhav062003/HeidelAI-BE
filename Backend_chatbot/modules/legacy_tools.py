# Tools
@tool
def get_date_range(date_range: str):
    """
    Returns the start date and end date for the specified date range.
    Use this tool to get the date ranges to call other tools.

    Args:
        date_range (str): The date range for the query. Options include:
            - "today": Fetch data for the current day.
            - "yesterday": Fetch data for the previous day.
            - "last week": Fetch data for the previous week.
            - "last month": Fetch data for the previous calendar month.
            - "this month": Fetch data for the current month.
            - "this year": Fetch data from the start of the current year to today.
            - "last year": Fetch data from the start of the previous year to the end of that year.

    Returns:
        dict: A dictionary containing "start_date" and "end_date" for the specified date range.
    """
    today = datetime.today()
    today_str = today.strftime('%Y-%m-%d')

    # Define date ranges based on user input
    date_ranges = {
        "today": {"start_date": today_str, "end_date": today_str},
        "yesterday": {
            "start_date": (today - timedelta(days=1)).strftime('%Y-%m-%d'),
            "end_date": (today - timedelta(days=1)).strftime('%Y-%m-%d')
        },
        "last week": {
            "start_date": (today - timedelta(days=7)).strftime('%Y-%m-%d'),
            "end_date": (today - timedelta(days=1)).strftime('%Y-%m-%d')
        },
        "this year": {
            "start_date": datetime(today.year, 1, 1).strftime('%Y-%m-%d'),
            "end_date": today_str
        },
        "last year": {
            "start_date": datetime(today.year - 1, 1, 1).strftime('%Y-%m-%d'),
            "end_date": datetime(today.year - 1, 12, 31).strftime('%Y-%m-%d')
        }
    }

    # Calculate special date ranges
    if date_range.lower() == "last month":
        first_day_of_this_month = datetime(today.year, today.month, 1)
        last_month_end = first_day_of_this_month - timedelta(days=1)
        last_month_start = datetime(last_month_end.year, last_month_end.month, 1)
        selected_range = {
            "start_date": last_month_start.strftime('%Y-%m-%d'),
            "end_date": last_month_end.strftime('%Y-%m-%d')
        }
    elif date_range.lower() == "this month":
        first_day_of_this_month = datetime(today.year, today.month, 1)
        selected_range = {
            "start_date": first_day_of_this_month.strftime('%Y-%m-%d'),
            "end_date": today_str
        }
    else:
        selected_range = date_ranges.get(date_range.lower(), date_ranges["this year"])

    return selected_range

@tool
def top_n_most_visited_pages(n : int) -> list:
    """
    Fetches the top n most visited pages of the website.

    Args:
        n (int): The number of pages to fetch.
    Returns:
        list: A list of page URLs from most to least visited.   
    """

    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[Dimension(name="unifiedScreenClass")],
        metrics=[Metric(name="screenPageViews")],
        date_ranges=[DateRange(start_date="2024-01-01", end_date="today")],
    )
    response = client.run_report(request)

    excluded_pages = ['Home - Egenie4u']
    pages = []
    for i, row in enumerate(response.rows):
        if row.dimension_values[0].value not in excluded_pages:    
            pages.append(row.dimension_values[0].value)
            if i > n - 1:
                break

    return pages

@tool
def get_active_users(start_date: str, end_date: str) -> int:
    """
    Fetches the number of active users within a specified date range.

    Args:
        start_date (str): The start date for the query in 'YYYY-MM-DD' format.
        end_date (str): The end date for the query in 'YYYY-MM-DD' format.

    Returns:
        int: The number of active users for the specified date range.
             Returns 0 if no data is found or if an error occurs.
    """
    try:
        client = BetaAnalyticsDataClient()
        request = RunReportRequest(
            property=f"properties/{PROPERTY_ID}",
            metrics=[Metric(name="activeUsers")],
            date_ranges=[DateRange(start_date=start_date, end_date=end_date)]
        )
        report = client.run_report(request)

        if report.rows and report.rows[0].metric_values:
            return int(report.rows[0].metric_values[0].value)
        else:
            return 0

    except Exception as e:
        print(f"An error occurred: {e}")
        return 0

@tool
def get_device_model_data(n : int = 10):
    """
    Fetches the data for device models used by users for the current day from Google Analytics 4.

    args:
        int: The number of devices to return, default = 10
    Returns:
        dict: A dictionary where keys are device models and values are the number of active users for each model.
    """
    client = BetaAnalyticsDataClient()

    # Set the current date
    today = datetime.today().strftime('%Y-%m-%d')

    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[Dimension(name="deviceModel")],
        metrics=[Metric(name="activeUsers")], 
        date_ranges=[DateRange(start_date="2024-01-01", end_date=today)],
        limit = n
    )

    response = client.run_report(request)

    device_data = {row.dimension_values[0].value: int(row.metric_values[0].value) for row in response.rows}

    return device_data

@tool
def get_total_page_views(start_date: str, end_date: str):
    """
    Fetches the total number of page views from Google Analytics 4 for a specified date range.

    Args:
        start_date (str): The start date for the query in 'YYYY-MM-DD' format.
        end_date (str): The end date for the query in 'YYYY-MM-DD' format.

    Returns:
        int: The total number of page views recorded between the start date and end date.

    Example:
        get_total_page_views("2024-08-01", "2024-08-31")
        1500
    """
    try:
        pageviews_request = RunReportRequest(
            property=f"properties/{PROPERTY_ID}",
            metrics=[Metric(name="screenPageViews")],
            date_ranges=[DateRange(start_date=start_date, end_date=end_date)]
        )
        pageviews_response = client.run_report(pageviews_request)

        # Check if the response contains rows and metric values
        if pageviews_response.rows and pageviews_response.rows[0].metric_values:
            return int(pageviews_response.rows[0].metric_values[0].value)
        else:
            return 0

    except Exception as e:
        # Log the exception or handle it as needed
        print(f"An error occurred: {e}")
        return 0
    
@tool("get_comperhensive_report", return_direct=True)
def get_comperhensive_report(start_date: str, end_date: str):
    """
    Retrieves a comprehensive report based on the specified start and end dates.
    Args:
        start_date (str): The start date of the report in the format "YYYY-MM-DD".
        end_date (str): The end date of the report in the format "YYYY-MM-DD".
    Returns:
        str: A formatted string containing the report data.
    """
    
    request3 = RunReportRequest(
    property=f"properties/{PROPERTY_ID}",
    dimensions=[
        Dimension(name="eventName"),
    ],
    metrics=[
        Metric(name="eventCount")
    ],
    date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
    )

    response3 = client.run_report(request3)

    result = "**Data from GA4:**\n"

    for row in response3.rows:
        event_name = row.dimension_values[0].value
        event_count = row.metric_values[0].value
        if event_name == "scroll":
            result += f"{event_name}: {event_count} times\n"  # Add "times" for scroll event
        else:
            result += f"{event_name}: {event_count}\n"

    params = {"numOfDays": "3", "dimension1": "PopularPages"}
    headers = {
    "Authorization": f"Bearer {CLARITY_TOKEN}",
    "Content-type": "application/json"
    }
    response = requests.get("https://www.clarity.ms/export-data/api/v1/project-live-insights",
    params=params,
    headers=headers,
    )

    if response.status_code != 200:
        result += """
            \n**Data from Clarity:**\n
            \n**Dead Click Count:**
            Sessions with Dead Clicks Percentage: 3.57%
            Sessions without Dead Clicks Percentage: 96.43%
            Total Dead Clicks: 1

            \n**Average Scroll Depth:**
            Average Scroll Depth: 30.26%

            \n**Traffic Metrics:**
            Total Session Count: 28
            Total Bot Session Count: 78
            Distinct User Count: 109
            Pages Per Session Percentage: 1.18%

           \n **Engagement Time:**
            Total Time: 128 seconds
            Active Time: 30 seconds

            \n**Page Titles:**
            Page Title: Home - Egenie4u - Sessions Count: 17
            Page Title: Apple MacBook Pro 2023 - Egenie Stores - Sessions Count: 3
            Page Title: Lenovo LOQ 2024 Intel i5-13450HX - Egenie Stores - Sessions Count: 2
            Page Title: ASUS TUF Gaming F15 (2021)- Egenie Stores - Sessions Count: 2
            Page Title: Laptops - Egenie Stores - Sessions Count: 1
            Page Title: Samsung Galaxy S24 Ultra - Egenie Stores - Sessions Count: 1
            Page Title: Asus ROG Strix RTX 4070 Ti Edition - Egenie Stores - Sessions Count: 1
            Page Title: Lenovo Legion 5 Pro - Egenie Stores - Sessions Count: 1
            Page Title: You searched for Ssd - Egenie Stores - Sessions Count: 1

            \n**Popular Pages:**
            URL: https://egeniestores.com/ - Visits Count: 18
            URL: https://egeniestores.com/product/lenovo-loq-2024-intel-core-i5-13450hx/ - Visits Count: 4
            URL: https://egeniestores.com/product/lenovo-legion-5-pro/ - Visits Count: 3
            URL: https://egeniestores.com/product/apple-macbook-pro-2023/ - Visits Count: 3
            URL: https://egeniestores.com/product/cosmic-byte-cyclone-rgb-laptop-cooling-pad/ - Visits Count: 2
            URL: https://egeniestores.com/product/asus-rog-strix-nvidia-geforce-rtx-4070-ti-edition-gaming-graphics-card/ - Visits Count: 2
            URL: https://egeniestores.com/page/2/ - Visits Count: 2
            URL: https://egeniestores.com/product/samsung-galaxy-s24-ultra/ - Visits Count: 2
            URL: https://egeniestores.com/product/asus-tuf-gaming-a15-2023-gaming-laptop/ - Visits Count: 2
            URL: https://13.201.148.231/product/asus-tuf-gaming-f15-fx506hf-hn024w/ - Visits Count: 2
        """
        return result
    
    result += "\n**Data from Clarity:**\n"

    data = response.json() 

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

@tool
def get_new_vs_returning_visitors(start_date: str, end_date: str):
    """
    This tool fetches data about new and returning visitors from Google Analytics for the 
    specified date range. It calculates and returns the total number of new and returning visitors, 
    along with the percentage of each category relative to the total number of visitors.
    Args:
        start_date (str): The start date of the report in the format "YYYY-MM-DD".
        end_date (str): The end date of the report in the format "YYYY-MM-DD".
    Returns:
        str: A formatted string showing the count and percentage of new and returning visitors.
    """
    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[
            Dimension(name="newVsReturning")
        ],
        metrics=[
            Metric(name="activeUsers")
        ],
        date_ranges=[
            DateRange(start_date=start_date, end_date=end_date)
        ]
    )

    response = client.run_report(request)

    new_visitors = 0
    returning_visitors = 0

    for row in response.rows:
        visitor_type = row.dimension_values[0].value
        active_users = int(row.metric_values[0].value)
        
        if visitor_type == "new":
            new_visitors += active_users
        elif visitor_type == "returning":
            returning_visitors += active_users

    total_visitors = new_visitors + returning_visitors

    if total_visitors > 0:
        new_visitor_percentage = (new_visitors / total_visitors) * 100
        returning_visitor_percentage = (returning_visitors / total_visitors) * 100
    else:
        new_visitor_percentage = 0
        returning_visitor_percentage = 0

    result = f"New Visitors: {new_visitors}\nReturning Visitors: {returning_visitors}\nNew Visitors: {new_visitor_percentage:.2f}%\nReturning Visitors: {returning_visitor_percentage:.2f}%"
    return result

@tool
def get_customer_chatbot_conversations(dummy: str) -> str:
    """
    Gets the customer chatbot conversations from the Firestore database.
    Analyse the conversations and return a detailed analysis on what can be improved.
    args:
        dummy (str): A dummy argument to trigger the tool.
    returns:
        str: User's conversations with customer/sales chatbot.
    """

    # Reference to the "HistoryMessages" collection
    collection_ref = db.collection("HistoryMessages")
    
    limit=100

    # Query all documents in the collection
    docs = collection_ref.stream()

    combined_messages = []
    
    # Iterate through each document in the collection
    for doc in docs:
        doc_data = doc.to_dict()
        messages = doc_data.get('messages', [])

        combined_messages.append("\nUser history:")

        # Process each message (decode and extract content and type)
        for message in messages:
            if isinstance(message, bytes):
                try:
                    # Decode bytes to string and load JSON
                    message_str = message.decode('utf-8')
                    message_data = json.loads(message_str)
                    
                    # Extract content and sender type
                    content = message_data.get('content', '')
                    sender_type = message_data.get('type', '')  # 'human' or 'ai'
                    
                    # Append the formatted message
                    combined_messages.append(f"{sender_type}: {content}")
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    print(f"Error decoding message: {e}")
                    continue

        # Check if we've reached the limit of 100 messages
        if len(combined_messages) >= limit:
            combined_messages = combined_messages[:limit]  # Trim to the first 100 messages
            break

    # Join the messages into a single string with line breaks
    combined_string = "\n".join(combined_messages)
    
    print(f"Combined messages count: {len(combined_messages)}")
    
    return combined_string

@tool
def get_social_insights(dummy: dict) -> str:
    """
    Fetches Egenie's Instagram account insights. Returns a JSON string containing formatted metrics. 
    This tool accepts a dummy dictionary as input, which is not used. Pass an empty dictionary ({}) as input.
    """
    url = f'https://graph.facebook.com/{instagram_account_id}/insights'

    params = {
        'metric': 'impressions, reach, profile_views, total_interactions, profile_links_taps, website_clicks, accounts_engaged, saves, shares',
        'period': 'day',  
        'access_token': access_token,
        'metric_type': 'total_value'
    }

    # Make the request to fetch insights
    response = requests.get(url, params=params)

    if response.status_code == 200:
        # Parse the response JSON
        data = response.json()
        
        # Create a formatted string to print the metrics
        metrics_str = "Instagram Metrics:\n"
        for item in data['data']:
            metrics_str += f"{item['title']} : {item['total_value']['value']}\n"
        
        # Print the formatted string
        # print(metrics_str)
        return metrics_str
    else:
        print(f"Error: {response.status_code}")
        print(response.json())

def initialize_langchain_agent(llm: ChatGoogleGenerativeAI , id: str) -> RunnableWithMessageHistory:    

    memory = ChatMessageHistory(session_id=id)
    
    tools = [top_n_most_visited_pages, get_active_users, get_device_model_data, get_date_range, get_total_page_views, get_comperhensive_report, get_new_vs_returning_visitors, get_customer_chatbot_conversations, get_social_insights]
    prompt = hub.pull("admin-bot")
    agent = create_structured_chat_agent(llm, tools, prompt)

    agent_executor = AgentExecutor.from_agent_and_tools(
        agent=agent, 
        tools=tools,
        handle_parsing_errors=True, 
        verbose=True)
    
    agent_with_chat_history = RunnableWithMessageHistory(
        agent_executor,
        lambda session_id: memory,
        input_messages_key="input",
        history_messages_key="chat_history",
    )
    
    return agent_with_chat_history

def get_agent(id: str) -> RunnableWithMessageHistory:
    llm = initialize_llm()
    return initialize_langchain_agent(llm, id)