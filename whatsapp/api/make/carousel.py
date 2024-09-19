import json
import os
import requests
from dotenv import load_dotenv

load_dotenv()

BUSINESS_ACCOUNT_ID = os.getenv("BUSINESS_ACCOUNT_ID")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")

blank_message = "\u3164"

def create_carousel():
    url = f"https://graph.facebook.com/{BUSINESS_ACCOUNT_ID}/message_templates"

    headers = {
        "Authorization": "Bearer " + ACCESS_TOKEN,
        "Content-Type": "application/json"
    }
# v3 - pending (only button last word parameter)
# v4 - approved (no params)
# v7 - approved (only body text one word parameter)
# v8 - approved (body text + end of url as parameters)
# v26 - approved (body text + product name + price, no params for link)
# v33 - approved (body text + product name + price, no params for buttons)

    data = {
    "name": "carousel_template_test_v38",
    "language": "en_US",
    "category": "marketing",
    "components": [
        {
            "type": "body",
            "text": "Here are some {{1}} that you might be interested in.",
            "example": {
                "body_text": [
                [
                    "keyboards"
                ]
                ]
            }
        },
        {
            "type": "carousel",
            "cards": [
                {
                    "components": [
                        {
                            "type": "header",
                            "format": "image",
                            "example": {
                                "header_handle": [
                                    "4:aW1hZ2UuanBn:aW1hZ2UvanBn:ARZZhd0b_0i_XF9EtK5hWlf0J8OytTIlbtD6GF0SUI9oer_GKPZlP7Htq1U1P1mx2u8z_x4a4YuFWrcU7EBd6ZYtn6vMb8yWEBHQDqMyN3Gycw:e:1726918737:509681741462833:61562573933016:ARYpyEaR_X6hrnbZYvQ"
                                ]
                            }
                        },
                        {
                            "type": "body",
                            "text": "Check out the {{1}}, priced at {{2}}.",
                            "example": {
                                    "body_text": [
                                    [
                                        "Zebronics 5 Gaming Keyboard",
                                        "Rs. 6,495"
                                    ]
                                    ]
                                }
                            },
                        {
                            "type": "buttons",
                            "buttons": [
                                {
                                    "type": "quick_reply",
                                    "text": "Know more."
                                },
                            ]
                        }
                    ]
                },
                {
                    "components": [
                        {
                            "type": "header",
                            "format": "image",
                            "example": {
                                "header_handle": [
                                    "4:aW1hZ2UuanBn:aW1hZ2UvanBn:ARbVTd8VHYC20JPsD7edl05BYnWfKu48MHO4Xz3rYgYaAl246RubD5JndCnXnlY6LL9VeoxcS30hUCss-2jIGLbkfnyAyh7nG_Ma7qktx2C_2Q:e:1726918782:509681741462833:61562573933016:ARbQ__6zbEZ3z3H8mEU"
                                ]
                            }
                        },
                        {
                            "type": "body",
                            "text": "Also check out {{1}}, priced at {{2}}.",
                            "example": {
                                    "body_text": [
                                    [
                                        "Cosmic Byte Ragers Mechanical Keyboard",
                                        "Rs. 1,499"
                                    ]
                                    ]
                                }
                        },
                        {
                            "type": "buttons",
                            "buttons": [
                                {
                                    "type": "quick_reply",
                                    "text": "Know more."
                                },
                            ]
                        }
                    ]
                },
                {
                    "components": [
                        {
                            "type": "header",
                            "format": "image",
                            "example": {
                                "header_handle": [
                                    "4:aW1hZ2UuanBn:aW1hZ2UvanBn:ARbVTd8VHYC20JPsD7edl05BYnWfKu48MHO4Xz3rYgYaAl246RubD5JndCnXnlY6LL9VeoxcS30hUCss-2jIGLbkfnyAyh7nG_Ma7qktx2C_2Q:e:1726918782:509681741462833:61562573933016:ARbQ__6zbEZ3z3H8mEU"
                                ]
                            }
                        },
                        {
                            "type": "body",
                            "text": "Also check out {{1}}, priced at {{2}}.",
                            "example": {
                                    "body_text": [
                                    [
                                        "Cosmic Byte Ragers Mechanical Keyboard",
                                        "Rs. 1,499"
                                    ]
                                    ]
                                }
                        },
                        {
                            "type": "buttons",
                            "buttons": [
                                {
                                    "type": "quick_reply",
                                    "text": "Know more."
                                },
                            ]
                        }
                    ]
                },
                {
                    "components": [
                        {
                            "type": "header",
                            "format": "image",
                            "example": {
                                "header_handle": [
                                    "4:aW1hZ2UuanBn:aW1hZ2UvanBn:ARbVTd8VHYC20JPsD7edl05BYnWfKu48MHO4Xz3rYgYaAl246RubD5JndCnXnlY6LL9VeoxcS30hUCss-2jIGLbkfnyAyh7nG_Ma7qktx2C_2Q:e:1726918782:509681741462833:61562573933016:ARbQ__6zbEZ3z3H8mEU"
                                ]
                            }
                        },
                        {
                            "type": "body",
                            "text": "Also check out {{1}}, priced at {{2}}.",
                            "example": {
                                    "body_text": [
                                    [
                                        "Cosmic Byte Ragers Mechanical Keyboard",
                                        "Rs. 1,499"
                                    ]
                                    ]
                                }
                        },
                        {
                            "type": "buttons",
                            "buttons": [
                                {
                                    "type": "quick_reply",
                                    "text": "Know more."
                                },
                            ]
                        }
                    ]
                },
                {
                    "components": [
                        {
                            "type": "header",
                            "format": "image",
                            "example": {
                                "header_handle": [
                                    "4:aW1hZ2UuanBn:aW1hZ2UvanBn:ARbVTd8VHYC20JPsD7edl05BYnWfKu48MHO4Xz3rYgYaAl246RubD5JndCnXnlY6LL9VeoxcS30hUCss-2jIGLbkfnyAyh7nG_Ma7qktx2C_2Q:e:1726918782:509681741462833:61562573933016:ARbQ__6zbEZ3z3H8mEU"
                                ]
                            }
                        },
                        {
                            "type": "body",
                            "text": "Also check out {{1}}, priced at {{2}}.",
                            "example": {
                                    "body_text": [
                                    [
                                        "Cosmic Byte Ragers Mechanical Keyboard",
                                        "Rs. 1,499"
                                    ]
                                    ]
                                }
                        },
                        {
                            "type": "buttons",
                            "buttons": [
                                {
                                    "type": "quick_reply",
                                    "text": "Know more."
                                },
                            ]
                        }
                    ]
                }
            ]
        }
    ]
}


    response = requests.post(url, headers=headers, data=json.dumps(data))
    return response

response = create_carousel()
print(response.status_code)
print(response.json())


def list_templates():
    url = f"https://graph.facebook.com/v20.0/{BUSINESS_ACCOUNT_ID}/message_templates"
    
    headers = {
        "Authorization": "Bearer " + ACCESS_TOKEN,
        "Content-Type": "application/json"
    }

    response = requests.get(url, headers=headers)
    return response

# response = list_templates()
# print(response.status_code)
# print(response.json())
