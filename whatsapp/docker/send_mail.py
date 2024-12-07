from email.utils import formataddr
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import os
from dotenv import load_dotenv

load_dotenv()

my_email = os.getenv("EMAIL")
password_key = os.getenv("EMAIL_KEY")

server = os.getenv("EMAIL_SERVER")
port = os.getenv("EMAIL_PORT")

def notify(name, recipient_address):
    try:
        # Establish an encrypted connection with SSL (correct for port 465)
        my_server = smtplib.SMTP_SSL(server, port)
        my_server.ehlo()

        # Login to the email server
        my_server.login(my_email, password_key)

        # Creating email message
        message = MIMEMultipart("alternative")
        message["From"] = formataddr(("Notifications @ HeidelAI",f"{my_email}"))
        message["To"] = recipient_address
        message["Subject"] = "[Urgent] New Support Request - WhatsApp"

        # Add text to the email
        text = f"Hello, the customer {name} has requested for live agent support. \nPlease login to the wordpress plugin to take action."
        message.attach(MIMEText(text, "plain"))
        list = ["rounaklongani@gmail.com"]
        list.append(recipient_address)
        my_server.sendmail(from_addr=my_email, to_addrs=list,  msg=message.as_string())

        print("Email sent successfully!")

    except Exception as e:
        print(f"An error occurred: {e}")

    finally:
        # Close the connection
        my_server.quit()
