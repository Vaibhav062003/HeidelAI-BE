from database import get_mongo_db

db = get_mongo_db()
docs = list(db['automation_scheduled_followups'].find({
    'conversation_id': 'whatsapp_918421264032',
    'flow_id': '__purchase_intent_followup__'
}).sort('created_at', -1).limit(5))

print(f"Found {len(docs)} jobs in automation_queue")
for d in docs:
    print(d)
    print("-" * 40)
