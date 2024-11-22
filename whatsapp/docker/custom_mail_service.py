from email.utils import formataddr
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
import os
from dotenv import load_dotenv

load_dotenv()

my_email = os.getenv('EMAIL')
password_key = os.getenv('EMAIL_KEY')

# SMTP Server and port no for GMAIL.com (or replace with your mail server details)
server = os.getenv('EMAIL_SERVER')
port = int(os.getenv('EMAIL_PORT'))

def send_email(sender_email, mail_from, subject, text):
    try:
        # Establish an encrypted connection with SSL (correct for port 465)
        my_server = smtplib.SMTP_SSL(server, port)
        my_server.ehlo()

        # Login to the email server
        my_server.login(my_email, password_key)

        # Creating email message
        message = MIMEMultipart("alternative")
        message["From"] = formataddr((f"{mail_from}",f"{my_email}"))
        message["To"] = sender_email
        message["Subject"] = subject

        # Add text to the email
        message.attach(MIMEText(text, "plain"))

        # Image path
        # grade_card_path = 'egenie.jpeg'

        # Attach image to the email
        # with open(grade_card_path, 'rb') as img_file:
        #     grade_card_img = img_file.read()
        #     message.attach(MIMEImage(grade_card_img, name=os.path.basename(grade_card_path)))

        # Send the email
        email_list = ["coderron579@gmail.com"]
        email_list.append(sender_email)
        my_server.sendmail(from_addr=my_email, to_addrs=email_list,  msg=message.as_string())

        print("Email sent successfully!")

    except Exception as e:
        print(f"An error occurred: {e}")

    finally:
        # Close the connection
        my_server.quit()
