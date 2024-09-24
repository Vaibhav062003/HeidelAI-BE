from telegram import InputMediaPhoto, Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import os, requests, json
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse


load_dotenv()


BOT_USERNAME = os.getenv('USERNAME')
TOKEN = os.getenv('TOKEN')
URL = os.getenv('URL')
ACCESS_TOKEN_API = os.getenv('ACCESS_TOKEN_API')

bot = telegram.Bot(token=TOKEN)


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://{URL}", "http://localhost"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Send an image with a caption
    await update.message.reply_photo(
        photo="egenie.jpeg",  # Replace with your image URL or local path
        caption=(
            "Hi there!👋 I'm Talkative, an AI-powered agent for eGenie Stores. "
            "I'm here to help you find the perfect tech products for your needs. \n \n"
            "✅ Whether you're looking for: \n - A new phone \n - A powerful gaming laptop \n - A custom-built PC \n \n"
            "I can help you find the best options for your budget and preferences.🎉 \n \n"
            "Feel free to ask me any questions you may have about our services or policies, and I'll be happy to assist!📕"
        )
    )


async def send_carousel(update: Update, context: ContextTypes.DEFAULT_TYPE, products_space):
    # Define the keyboard with buttons to simulate next/previous
    try:
        keyboard = [
            [
                InlineKeyboardButton("Know more", callback_data="know_more"),
                InlineKeyboardButton("Next Item ⏭️", callback_data="next_item_0")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        # Send the first "card" with the inline keyboard
        await update.message.reply_photo(
            photo=products_space[0]['image_link'],  # Replace with the first item image URL
            caption= f"{products_space[0]['name']} priced at: Rs.{products_space[0]['price']} " ,  # Replace with the first item caption
            reply_markup=reply_markup
        )
    except Exception as e:
        print("Error :", e)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global websocket_product_data

    query = update.callback_query
    if query is None or query.message is None:
        print("Callback query or message is None")
        return

    await query.answer()  # Acknowledge the callback
    
    # Extract the current index from the callback data
    data = query.data.split('_')
    print("Data: ", data)
    action = data[0]
    index = int(data[2]) if len(data) > 2 else None
    
    # Retrieve products_space from context or a global variable
    user_id = query.message.chat.id
    print("User ID: ", user_id)
    client_id = str(user_id)
    print("Client ID: ", client_id)
    products_space = websocket_product_data.get(client_id)  # Retrieve products_space for the user


    print("products space inside funciton",products_space)

    if not products_space:
        await query.message.reply_text("No products available.")
        return

    print("Action: ", action)
    
    # Handle the "Next Item" button press
    if action == "next":
        print("came in nexty button?")
        print(action)
        index += 1
        if index >= len(products_space):
            index = 0  # Loop back to the first item
        keyboard = [
            [
                InlineKeyboardButton("⏮️", callback_data=f"previous_item_{index}"),
                InlineKeyboardButton("Know more", callback_data=f"know_more"),
                InlineKeyboardButton("⏭️", callback_data=f"next_item_{index}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Edit the message with the new image and text (simulate next item)
        await query.edit_message_media(
            media=InputMediaPhoto(products_space[index]['image_link'])
        )
        await query.edit_message_caption(
            caption=f"{products_space[index]['name']} priced at: Rs.{products_space[index]['price']}",  # Replace with next item caption
            reply_markup=reply_markup
        )
    
    # Handle the "Previous Item" button press
    elif action == "previous":
        index -= 1
        if index < 0:
            index = len(products_space) - 1  # Loop back to the last item
        keyboard = [
            [
                InlineKeyboardButton("⏮️", callback_data=f"previous_item_{index}"),
                InlineKeyboardButton("Know more", callback_data=f"know_more"),
                InlineKeyboardButton("⏭️", callback_data=f"next_item_{index}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Edit the message with the new image and text (simulate previous item)
        await query.edit_message_media(
            media=InputMediaPhoto(products_space[index]['image_link'])
        )
        await query.edit_message_caption(
            caption=f"{products_space[index]['name']} priced at: Rs.{products_space[index]['price']}",  # Replace with previous item caption
            reply_markup=reply_markup
        )
    
    # Handle the "Know more" button press
    elif action == "know":
        # Implement the logic to handle the "Know more" action
        
        await query.message.reply_text(f"This functionality is under development.")



async def send_message(text, chat_id):
    async with bot:
        await bot.send_message(text=text, chat_id=chat_id)

async def show_typing(chat_id):
    async with bot:
        await bot.send_chat_action(chat_id=chat_id, action=telegram.constants.ChatAction.TYPING)

async def handle_response(text, client_id):
    user_id = str(client_id)
    user_input = f"(client_id: {user_id}): " + text

   
async def ask_agent(text, client_id):
    url = f"https://{URL}/generate_response"
    
    headers = {
        "Content-Type": "application/json",
    }
    
    data = {
        "user_input": text,
        "client_id": client_id,
    }

    response = requests.post(url, headers=headers, data=json.dumps(data))
    return response.json()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message_type = update.message.chat.type
    text = update.message.text
    user_id = update.message.chat.id
    client_id = str(user_id)

    print("First User ID: ", user_id)
    
    await show_typing(client_id)


    print(f'User ({client_id}) in {message_type}: "{text}"')
    
    username = "@" + BOT_USERNAME

    if message_type == 'group':

        if username in text:
            new_text = text.replace(username, '').strip()
            print("New text: ", new_text)

            response = await ask_agent(new_text, client_id)

            if response['message_type'] == 'products':
                products_space = response['products_space']
                agent_response = response['response']

                await send_message(text=agent_response, chat_id=client_id)
                await send_carousel(update, context, products_space)
            else:
                await update.message.reply_text(response['response'])
        else:
            return
        
    else:
        response = await ask_agent(text, client_id)

        if response['message_type'] == 'products':
                products_space = response['products_space']
                agent_response = response['response']

                await send_message(text=agent_response, chat_id=client_id)
                await send_carousel(update, context, products_space)
        else:
            agent_response = response['response']
            await update.message.reply_text(agent_response)

    print('Bot:', agent_response)


async def error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f'Update {update} caused error {context.error}')


if __name__ == '__main__':
    print("Starting bot...")
    app = Application.builder().token(TOKEN).build()

    # Commands
     # Register existing /start command
    app.add_handler(CommandHandler('start', start_command))

    # Register the new /carousel command
    # app.add_handler(CommandHandler('carousel', carousel_command))

    # Register button handler for inline keyboard button clicks
    app.add_handler(CallbackQueryHandler(button_handler))

    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Errors
    app.add_error_handler(error)

    print("Polling...")
    app.run_polling(poll_interval=1)