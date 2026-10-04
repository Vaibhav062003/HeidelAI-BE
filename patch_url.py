with open('services/shakti_bot_service.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace(
    'img_url = "https://raw.githubusercontent.com/egenie4u/chatbot_prod/dev/assets/shakti_welcome.jpg"',
    'img_url = "https://res.cloudinary.com/djpc5zfgz/image/upload/v1791111128/whatsapp_media/org_2zV2OOhodLocEnTybzR8t9MIYCE/user/d5a6853c277e4f4989ba593aa056c2c1-shakti_welcome.jpg"'
)

with open('services/shakti_bot_service.py', 'w', encoding='utf-8') as f:
    f.write(content)
