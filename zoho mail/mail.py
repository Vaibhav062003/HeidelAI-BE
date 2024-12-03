from email.utils import formataddr
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
import os
from dotenv import load_dotenv

# Load the environment variables
load_dotenv()



my_email = os.getenv("EMAIL")
password_key = os.getenv("PASS")

# SMTP Server and port no for GMAIL.com (or replace with your mail server details)
server = "smtp.zoho.in"
port = 465

try:
    # Establish an encrypted connection with SSL (correct for port 465)
    my_server = smtplib.SMTP_SSL(server, port)
    my_server.ehlo()

    # Login to the email server
    my_server.login(my_email, password_key)

    # Creating email message
    message = MIMEMultipart("alternative")
    message["From"] = formataddr(("Egenie Stores", f"{my_email}"))
    message["To"] = "rounaklongani@gmail.com"
    message["Subject"] = "Email from python😎"

    # Add text to the email
    html = """
    <html>
    <body>
        <h1>Hello,</h1>
        <p>This is a test message from <b>Python</b> code.</p>
        <p>Best regards,<br>Team Egenie Stores</p>
    </body>
    </html>
    """

    # Attach HTML content to the email
    message.attach(MIMEText(html, "html"))

  
    # Send the email
    my_server.sendmail(my_email, "rounaklongani@gmail.com", message.as_string())
    print("Email Sent")
    # Close the server connection
    my_server.quit()

except Exception as e:
    print(f"Error: {e}")