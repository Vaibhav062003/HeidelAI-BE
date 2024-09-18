from langchain_google_firestore import FirestoreChatMessageHistory


history = FirestoreChatMessageHistory(
    session_id="my-session-id",
    collection="HistoryMessages"
)