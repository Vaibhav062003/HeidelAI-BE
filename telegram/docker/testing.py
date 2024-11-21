app_id = '509681741462833'
app_secret = '117677cf3fa4108613b044bcbff9e6f3'
access_token = 'EAAHPjYclZCTEBO9k47ZCwZCDdPAtLutdwULVtKTg6Lu5RuRtRO8mLyDmZAganPIqG0LoLNIyyDv3lATPXaoEUeQZA6R1ooynFKn1b3lsyVehC26IjSddZCXTykUU663uokVQEjmZAyAmARKvxKD0lbkYWDZBjwvhZB0HwZAEw4nZBbxrxl5OyZBb8Qch30Qj4I5ZCqyi48SqP6uXejd3tCfSs'

from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.adaccount import AdAccount

FacebookAdsApi.init(app_id, app_secret, access_token)
my_account = AdAccount('act_2353530418276809')
campaigns = my_account.get_campaigns(fields=['insights'])
print(campaigns) 