import requests
import json, os

from dotenv import load_dotenv

load_dotenv()

def getCreds() :
	""" Get creds required for use in the applications
	
	Returns:
		dictonary: credentials needed globally

	"""

	creds = dict() # dictionary to hold everything
	creds['access_token'] = os.getenv('ACCESS_TOKEN') # access token for use with all api calls
	creds['graph_domain'] = 'https://graph.facebook.com/' # base domain for api calls
	creds['graph_version'] = 'v21.0' # version of the api we are hitting
	creds['endpoint_base'] = creds['graph_domain'] + creds['graph_version'] + '/' # base endpoint with domain and version
	creds['instagram_account_id'] = os.getenv('INSTAGRAM_ACCOUNT_ID') # users instagram account id
	creds['page_id'] = os.getenv('FB_PAGE_ID') # users page id
	creds['debug'] = 'no'

	return creds

def makeApiCall( url, endpointParams, type ) :
	""" Request data from endpoint with params
	
	Args:
		url: string of the url endpoint to make request from
		endpointParams: dictionary keyed by the names of the url parameters


	Returns:
		object: data from the endpoint

	"""

	if type == 'POST' : # post request
		data = requests.post( url, endpointParams )
	else : # get request
		data = requests.get( url, endpointParams )

	response = dict() # hold response info
	response['url'] = url # url we are hitting
	response['endpoint_params'] = endpointParams #parameters for the endpoint
	response['endpoint_params_pretty'] = json.dumps( endpointParams, indent = 4 ) # pretty print for cli
	response['json_data'] = json.loads( data.content ) # response data from the api
	response['json_data_pretty'] = json.dumps( response['json_data'], indent = 4 ) # pretty print for cli

	return response # get and return content

def getUserMedia( params ) :
	""" Get users media
	
	API Endpoint:
		https://graph.facebook.com/{graph-api-version}/{ig-user-id}/media?fields={fields}

	Returns:
		object: data from the endpoint

	"""

	endpointParams = dict() # parameter to send to the endpoint
	endpointParams['fields'] = 'id,caption,media_type,media_url,permalink,thumbnail_url,timestamp,username' # fields to get back
	endpointParams['access_token'] = params['access_token'] # access token

	url = params['endpoint_base'] + params['instagram_account_id'] + '/media' # endpoint url

	return makeApiCall( url, endpointParams, params['debug'] ) # make the api call

def getMediaInsights( params ) :
	""" Get insights for a specific media id
	
	API Endpoint:
		https://graph.facebook.com/{graph-api-version}/{ig-media-id}/insights?metric={metric}

	Returns:
		object: data from the endpoint

	"""
	endpointParams = dict() # parameter to send to the endpoint
	endpointParams['metric'] = params['metric'] # fields to get back
	endpointParams['access_token'] = params['access_token'] # access token

	url = params['endpoint_base'] + params['latest_media_id'] + '/insights' # endpoint url

	return makeApiCall( url, endpointParams, params['debug'] ) # make the api call

def getMostRecentMediaByType(media_data, media_type):
    """Filter the most recent media of a specific type.
    
    Args:
        media_data (list): List of media items from the API.
        media_type (str): The media type to filter for ('IMAGE', 'CAROUSEL_ALBUM', 'VIDEO').
    
    Returns:
        dict: The most recent media of the specified type.
    """
    if (media_type == 'VIDEO'):
        for media in media_data:
            if media['media_type'] == media_type:
                return media
    else:
        for media in media_data:
            if media['media_type'] == 'IMAGE' or media['media_type'] == 'CAROUSEL_ALBUM':
                return media
    return None

def getMostRecentMediaByType(media_data, media_type):
    """Filter the most recent media of a specific type.
    
    Args:
        media_data (list): List of media items from the API.
        media_type (str): The media type to filter for ('IMAGE', 'CAROUSEL_ALBUM', 'VIDEO').
    
    Returns:
        dict: The most recent media of the specified type.
    """
    if (media_type == 'VIDEO'):
        for media in media_data:
            if media['media_type'] == media_type:
                return media
    else:
        for media in media_data:
            if media['media_type'] == 'IMAGE' or media['media_type'] == 'CAROUSEL_ALBUM':
                return media
    return None

def format_breakdown(breakdown_name, data):
	# Sort data in descending order by value
	sorted_data = sorted(data, key=lambda x: x["value"], reverse=True)
	
	# Limit to top 5 for city and country
	is_city_or_country = breakdown_name in ["city", "country"]
	limited_data = sorted_data[:10] if is_city_or_country else sorted_data

	formatted = f"{breakdown_name.title()} (Top {len(limited_data)})\n"
	for entry in limited_data:
		value_label = entry["dimension_values"][0]
		value = entry["value"]
		formatted += f"  - {value_label}: {value}\n"
	return formatted + "\n"

def format_metrics(data):
	result_str = ""
	for metric, breakdowns in data.items():
		result_str += f"{metric.replace('_', ' ').title()}\n"
		result_str += "=" * len(metric) + "\n"
		for breakdown_name, breakdown_data in breakdowns.items():
			result_str += format_breakdown(breakdown_name, breakdown_data)
	return result_str

def demographics_insights():
	creds = getCreds()

	url = f'https://graph.facebook.com/{creds['instagram_account_id']}/insights'

	# Base parameters for the API
	base_params = {
		'metric': 'follower_demographics,engaged_audience_demographics,reached_audience_demographics',
		'period': 'lifetime',
		'timeframe': 'this_month',
		'access_token': creds['access_token'],
		'metric_type': 'total_value',
	}

	# List of breakdowns
	breakdowns = ["country", "city", "gender", "age"]

	# Initialize dictionary to store aggregated data by metric
	metrics_data = {}

	# Fetch data for each breakdown
	for breakdown in breakdowns:
		params = {**base_params, 'breakdown': breakdown}
		response = requests.get(url, params=params)

		if response.status_code == 200:
			try:
				data = response.json()["data"]
				
				for entry in data:
					metric_name = entry["name"]
					breakdown_results = entry["total_value"]["breakdowns"][0]["results"]

					if metric_name not in metrics_data:
						metrics_data[metric_name] = {}

					# Store the breakdown data
					metrics_data[metric_name][breakdown] = breakdown_results
			except KeyError as e:
				print(f"KeyError: {e} in breakdown {breakdown}")
		else:
			print(f"Failed to fetch data for breakdown {breakdown}. Status: {response.status_code}")

	return format_metrics(metrics_data)