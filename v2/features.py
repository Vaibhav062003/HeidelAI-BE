from datetime import datetime

def assign_priority(messages):
    pass

def generate_summary(messages):
    pass

def run_analysis(chat_id, chat):
    """
    Runs the priority tagging and summary generation for the chat.
    """
    # Perform priority tagging and summary generation
    priority = assign_priority(chat["messages"])
    summary = generate_summary(chat["messages"])

    # Update MongoDB with priority, summary, and reset counter
    db.chats.update_one(
        {"_id": chat_id},
        {
            "$set": {
                "priority": priority,
                "ai_summary": summary,
                "message_counter": 0,  # Reset the counter
                "last_tagged": datetime.now()
            }
        }
    )

def add_message(chat_id, role, content):
    """
    Adds a message to the conversation and triggers analysis if counter reaches threshold.
    """
    # Fetch the chat document
    chat = db.chats.find_one({"_id": chat_id})
    if not chat:
        raise ValueError("Chat not found!")

    # Add the new message
    db.chats.update_one(
        {"_id": chat_id},
        {"$push": {"messages": {"role": role, "content": content}}}
    )

    # Increment the message counter
    new_counter = chat.get("message_counter", 0) + 1
    db.chats.update_one({"_id": chat_id}, {"$set": {"message_counter": new_counter}})

    # Check if counter reached threshold
    if new_counter >= 10:
        run_analysis(chat_id, chat)
