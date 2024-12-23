import json
from google.cloud import firestore
from google.oauth2 import service_account

# Define the path to your service account JSON file
SERVICE_ACCOUNT_FILE = 'whatsapp-chatbot-history-cf43052a413d.json'

# Load the credentials from the JSON file
credentials = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE)

# Initialize Firestore using the credentials
db = firestore.Client(credentials=credentials)

def get_combined_session_history():
    # Reference to the "HistoryMessages" collection
    collection_ref = db.collection("HistoryMessages")
    
    # Query all documents in the collection
    docs = collection_ref.stream()

    user_chats = {}  # Dictionary to store chats by user (document ID as user_id)
    
    # Iterate through each document in the collection
    for doc in docs:
        doc_data = doc.to_dict()
        messages = doc_data.get('messages', [])

        user_id = doc.id  # Use document ID as user ID
        user_chats[user_id] = []  # Initialize the user chat list

        # Process each message (decode and extract content and type)
        for message in messages:
            if isinstance(message, bytes):
                try:
                    # Decode bytes to string and load JSON
                    message_str = message.decode('utf-8')
                    message_data = json.loads(message_str)
                    
                    # Extract content and sender type
                    content = message_data.get('content', '')
                    sender_type = message_data.get('type', '')  # 'human' or 'ai'
                    
                    # Append the message as a tuple (sender_type, content) to the user's chat history
                    user_chats[user_id].append({sender_type: content})
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    print(f"Error decoding message: {e}")
                    continue

    # Save the user chats to a JSON file
    with open('chat_history_new.json', 'w') as f:
        json.dump(user_chats, f, indent=4)

    print(f"User chat history saved successfully to user_chat_history.json with {len(user_chats)} users.")
    
    return user_chats

# Call the function to get combined session history (all messages)
combined_history = get_combined_session_history()
