from google.cloud import firestore
from google.oauth2 import service_account
# Define the path to your service account JSON file
SERVICE_ACCOUNT_FILE = 'whatsapp-chatbot-history-cf43052a413d.json'

# Load the credentials from the JSON file
credentials = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE)

# Initialize Firestore using the credentials
db = firestore.Client(credentials=credentials)

def get_and_trim_session_history(session_id: str):
    # Reference to the document for the specific session
    doc_ref = db.collection("HistoryMessages").document(session_id)
    
    # Fetch the document
    doc = doc_ref.get()
    
    if doc.exists:
        messages = doc.to_dict().get('messages', [])
        print(f"Original messages count: {len(messages)}")
        
        # Check if the number of messages exceeds 20
        if len(messages) > 20:
            excess_count = len(messages) - 20
            messages = messages[excess_count:]  # Keep only the last 20 messages
            print(f"Trimmed messages count: {len(messages)}")
            
            # Update the document with the trimmed list of messages
            doc_ref.update({'messages': messages})
            print("Document updated with trimmed messages.")
        else:
            print("No trimming needed. Message count is within limit.")
    else:
        print("Document does not exist.")
    
    return doc_ref

# Example usage
session_id = "ron-test"
doc_ref = get_and_trim_session_history(session_id)
