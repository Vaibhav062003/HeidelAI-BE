from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.adaccount import AdAccount

def verify_token_permissions(app_id, app_secret, access_token):
    try:
        # Initialize the API
        FacebookAdsApi.init(app_id, app_secret, access_token)
        
        # Try to get basic account info first
        account = AdAccount('act_555483407117447')
        fields = ['name', 'account_status']
        
        # This will throw an error if permissions are missing
        account_info = account.api_get(fields=fields)
        print("Successfully connected to account:", account_info['name'])
        
    except Exception as e:
        print(f"Permission Error: {str(e)}")

# Test the token
verify_token_permissions("509681741462833", "117677cf3fa4108613b044bcbff9e6f3", "EAAHPjYclZCTEBO9k47ZCwZCDdPAtLutdwULVtKTg6Lu5RuRtRO8mLyDmZAganPIqG0LoLNIyyDv3lATPXaoEUeQZA6R1ooynFKn1b3lsyVehC26IjSddZCXTykUU663uokVQEjmZAyAmARKvxKD0lbkYWDZBjwvhZB0HwZAEw4nZBbxrxl5OyZBb8Qch30Qj4I5ZCqyi48SqP6uXejd3tCfSs")