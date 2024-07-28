import mysql.connector

db_config = {
    'user': 'doadmin',
    'password': 'AVNS_tiVES2PpTq7pDxUq42s',
    'host': '68.183.94.53',
    'port': 3306,
    'database': 'egen_chatbot'
}
# mysql://doadmin:AVNS_tiVES2PpTq7pDxUq42s@68.183.94.53:3306/egen_chatbot

try:
    connection = mysql.connector.connect(**db_config)
    if connection.is_connected():
        print("Successfully connected to the database")
        cursor = connection.cursor()
        cursor.execute("SELECT DATABASE();")
        record = cursor.fetchone()
        print("You're connected to database: ", record)
except mysql.connector.Error as err:
    print("Error: ", err)
finally:
    if connection.is_connected():
        cursor.close()
        connection.close()
        print("MySQL connection is closed")
