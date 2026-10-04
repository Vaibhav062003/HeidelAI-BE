import re

with open('services/shakti_bot_service.py', 'r', encoding='utf-8') as f:
    content = f.read()

new_outlets = '''OUTLETS = [
    {"id": "out_baner", "title": "Baner", "desc": "Baner, Pune", "url": "https://maps.google.com/?q=Jawa+Yezdi+Baner+Pune"},
    {"id": "out_wakad", "title": "Wakad", "desc": "Wakad, Pune", "url": "https://maps.google.com/?q=Jawa+Yezdi+Wakad+Pune"},
    {"id": "out_chinchwad", "title": "Chinchwad", "desc": "Chinchwad, Pune", "url": "https://maps.google.com/?q=Jawa+Yezdi+Chinchwad+Pune"},
    {"id": "out_ravet", "title": "Ravet", "desc": "Survey No 288, Pune Bhir Plaza", "url": "https://maps.google.com/?q=Jawa+Yezdi+Ravet+Pune"},
    {"id": "out_sinhagad", "title": "Sinhagad Road", "desc": "Sinhagad Road, Pune", "url": "https://maps.google.com/?q=Jawa+Yezdi+Sinhagad+Road+Pune"},
    {"id": "out_goa", "title": "Goa", "desc": "Goa Showroom", "url": "https://maps.google.com/?q=Jawa+Yezdi+Goa"}
]'''

content = re.sub(r'OUTLETS = \[.*?\]', new_outlets, content, flags=re.DOTALL)

with open('services/shakti_bot_service.py', 'w', encoding='utf-8') as f:
    f.write(content)
