from email.utils import formataddr
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
import os

my_email = 'support@egeniestores.com'
password_key = '%,86,elibIA'

# SMTP Server and port no for GMAIL.com (or replace with your mail server details)
server = "mail.egeniestores.com"
port = 465

try:
    # Establish an encrypted connection with SSL (correct for port 465)
    my_server = smtplib.SMTP_SSL(server, port)
    my_server.ehlo()

    # Login to the email server
    my_server.login(my_email, password_key)

    # Creating email message
    message = MIMEMultipart("alternative")
    message["From"] = formataddr(("Egenie Stores",f"{my_email}"))
    message["To"] = "rounaklongani@gmail.com"
    message["Subject"] = "Test Email with Image"

    # Add text to the email
    text = "Hello, this is a test message"
    message.attach(MIMEText(text, "plain"))

    # # Image path
    # grade_card_path = 'egenie.jpeg'

    # # Attach image to the email
    # with open(grade_card_path, 'rb') as img_file:
    #     grade_card_img = img_file.read()
    #     message.attach(MIMEImage(grade_card_img, name=os.path.basename(grade_card_path)))

    # Send the email
    
    my_server.sendmail(from_addr=my_email, to_addrs="rounaklongani@gmail.com",  msg=message.as_string())

    print("Email sent successfully!")

except Exception as e:
    print(f"An error occurred: {e}")

finally:
    # Close the connection
    my_server.quit()
