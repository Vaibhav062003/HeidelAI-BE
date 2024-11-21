app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://egeniestores.com", "http://localhost"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def remove_expired_message_ids():
    current_time = time.time()
    expired_ids = []

    # Iterate through the dictionary to find expired message IDs
    for message_id, timestamp in processed_message_ids.items():
        if current_time - timestamp > EXPIRY_TIME_SECONDS:
            expired_ids.append(message_id)

    # Remove the expired message IDs
    for message_id in expired_ids:
        del processed_message_ids[message_id]

def validate_signature(payload, signature):
    # Hash the payload with the app secret
    expected_signature = hmac.new(
        bytes(APP_SECRET, 'latin-1'),
        msg=payload.encode('utf-8'),
        digestmod=hashlib.sha256
    ).hexdigest()
    
    # Compare signatures to ensure they match
    return hmac.compare_digest(f"sha256={expected_signature}", signature)

# Middleware or decorator to validate signature for POST requests
async def signature_required(request: Request, x_hub_signature_256: str = Header(None)):
    # Get the request payload
    body = await request.body()
    payload = body.decode('utf-8')

    if not x_hub_signature_256:
        raise HTTPException(status_code=403, detail="Signature missing")

    if not validate_signature(payload, x_hub_signature_256):
        raise HTTPException(status_code=403, detail="Invalid signature")

@app.get("/webhook")
async def verify_webhook(request: Request):
    # This is for the webhook verification (GET request)
    args = request.query_params
    if args.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(content=str(args.get("hub.challenge")))
    return {"status": "Verification failed"}


@app.post("/webhook")
async def handle_webhook(request: Request, x_hub_signature_256: str = Header(None)):
    global flag
    global search_term
    global websocket_product_data
    # First, validate the signature
    await signature_required(request, x_hub_signature_256)

    # Remove expired message IDs
    remove_expired_message_ids()

    # Process the incoming message (if the signature is valid)
    payload = await request.json()
    print("Incoming webhook payload:", json.dumps(payload, indent=2))

    # Handle incoming messages
    if "entry" in payload and len(payload["entry"]) > 0:
        for entry in payload["entry"]:
            for change in entry.get("changes", []):
                value = change.get("value", {})

                statuses = value.get("statuses", [])
                if statuses:
                    for status in statuses:
                        message_status = status["status"]
                        recipient_id = status["recipient_id"]
                        print("Message Status: ", message_status, ", Recipient ID: ", recipient_id)

                contacts = value.get("contacts", [])
                if contacts:
                    for contact in contacts:
                        sender_name = contact["profile"]["name"]
                        # print("Sender name: ", sender_name)

                messages = value.get("messages", [])
                if messages:
                    for message in messages:
                        message_id = message["id"]

                        await mark_read(message_id)

                        # Check if message ID is already processed
                        if message_id in processed_message_ids:
                            print("Duplicate message, skipping processing.")
                            continue
                        
                        # Add message ID with the current timestamp
                        processed_message_ids[message_id] = time.time()

                        sender = message["from"]
                        
                        

                        if message["type"] == "text":
                            user_input = message["text"]["body"]
                            print("Message Received from Name: ", sender_name, ", Number: ",sender ,", Message: ", user_input)

                            if user_input == "Hi":
                                await send_introduction(sender, sender_name)
                                print("Introduction sent to Name: ", sender_name, ", Number: ",sender )
                                continue
    

                            client_id = sender
                            user_input = f"(client_id: {client_id}): " + user_input

                            response = agent_executor.invoke(
                                {"input": user_input},
                                config={"configurable": {"session_id": client_id}},
                            )

                            async with websocket_data_lock:
                                print("websocket_product_data 1: ", websocket_product_data)
                                products_space = websocket_product_data.get(client_id)
                                

                                print("Products space 2: ", products_space)

                                if products_space:
                                    print("Came in if")
                                    await send_message(sender, "Please give me a minute.")
                                    del websocket_product_data[client_id] 
                                    await send_carousel(sender, search_term, products_space)
                                    search_term = ""
                                    print("websocket_product_data 2: ", websocket_product_data) 
                                else:
                                    print("Came in else")
                                    await send_message(sender, response['output'])

                            print("Products space 3: ", products_space)

                            if flag:
                                flag = False
                                doc_ref = client.collection("HistoryMessages").document(client_id)
                                doc = doc_ref.get()
                                
                                if doc.exists:
                                    messages = doc.to_dict().get('messages', [])
                                    print(f"Original messages count: {len(messages)}")
                                    excess_count = len(messages) - 15
                                    messages = messages[excess_count:] 
                                    
                                    doc_ref.update({'messages': messages})
                                    print("Document updated with trimmed messages.")

                            print("Message sent to Name: ", sender_name, ", Number: ", sender, ", Response: ", response['output'])

                        elif message["type"] == "button":
                            print("Name: ", sender_name, ", Number: ",sender ,", clicked on know more")
                            payload = message["button"]["payload"]
                            print("Button Payload: ", payload)

                            

                            payload = payload.split("_")
                            print("Payload: ", payload)


                            # output = agent(payload)
                            # response = output['output']
                            # await send_message(sender, "This functionality is in development.")
                            print("Sender clicked Know More")
                            await send_product(sender, payload)
                            # print("Sent message to Name: ", sender_name, ", Number: ", sender, "")

                            # await send_product(sender, payload)
                        elif message["type"] == "order":
                            # response = f"Hey {sender_name},Thank you for trying to place and order with us. \n\n We're sorry this feature is not available yet."
                            # await send_message(sender, response)

                            product_items = message["order"]["product_items"]

                            for product in product_items:
                                product_id = product["product_retailer_id"]
                                quantity = product["quantity"]
                                item_price = product["item_price"]
                                currency = product["currency"]

                                print(f"Product ID: {product_id}, Quantity: {quantity}, Price: {item_price}, Currency: {currency}")

                            print("Sender sent cart")
                            add_order_details(sender, product_items)
                            await get_details(sender, again=False)

                            # print("Sent message to Name: ", sender_name, ", Number: ", sender, ", Response: ", response)

                        elif message["type"] == "interactive":
                            print("Name: ", sender_name, ", Number: ",sender ,", sent details")
                            print("Sender clicked on interactive message")
                            interactive_type = message["interactive"]["type"]
                            
                            if interactive_type == "nfm_reply":
                                # Extract the JSON string from the message 
                                response_json_str = message["interactive"]["nfm_reply"]["response_json"]

                                # Parse the JSON string into a Python dictionary
                                response_json = json.loads(response_json_str)

                                # Add the sender details to the address details list
                                add_address_details(sender, response_json)
                                print(address_details)
                                # Print the parsed JSON
                                # print(response_json)
                                # {'pin_code': '411058', 'full_name': 'Rounak longani', 'flow_token': 'AQAAAAACS5FpgQ_cAAAAAD0QI3s.', 'state': 'Maharashtra ', 'email': 'Rounaklongani@gmail.com', 'city': 'Pune', 'country': '0_India', 'address': 'Hahsjs'}

                                await confirm_address(sender, response_json)

                            elif interactive_type == "button_reply":
                                
                                payload = message["interactive"]["button_reply"]["id"]
                                print("Reply Payload: ", payload)
                                if payload == "info_correct":
                                    print("Info is correct")
                                    await get_payment(sender, sender_name)
                                elif payload == "info_incorrect":
                                    print("Info is incorrect")
                                    delete_address_details(sender)
                                    await get_details(sender, again=True)

                            # await send_message(sender, "Got your responses. Thank you!\n\nThis functionality is still in development.")

    return {"status": "ok"}