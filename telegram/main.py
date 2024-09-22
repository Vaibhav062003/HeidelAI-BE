from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
import os

load_dotenv()

BOT_USERNAME = os.getenv('BOT_USERNAME')
TOKEN = os.getenv('TOKEN')

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hi there!👋 I'm Talkative, an AI-powered agent for eGenie Stores. I'm here to help you find the perfect tech products for your needs. \n \n ✅ Whether you're looking for: \n - A new phone \n - A powerful gaming laptop \n - A custom-built PC \n \n I can help you find the best options for your budget and preferences.🎉 \n \n Feel free to ask me any questions you may have about our services or policies, and I'll be happy to assist!📕")


def handle_response(text):
    processed_text = text.lower()

    if 'hello' in processed_text:
        return 'Hey there'

    if 'how are you' in processed_text:
        return 'I am fine, thank you! How can I help you today?'
    
    if 'bye' in processed_text:
        return 'Goodbye! Have a great day!'
    
    return "I'm dumb i dont get you"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_type = update.message.chat.type
    text = update.message.text
    
    print(f'User in ({update.message.chat.id}) in {message_type}: "{text}"')
    
    if message_type == 'group':
        if BOT_USERNAME in text:
            new_text = text.replace(f'@{BOT_USERNAME}', '').strip()
            response = handle_response(new_text)
        else:
            return
        
    else:
        response = handle_response(text)


    print('Bot:', response)
    await update.message.reply_text(response)

async def error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f'Update {update} caused error {context.error}')

if __name__ == '__main__':
    print("Starting bot...")
    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler('start', start_command))

    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Errors
    app.add_error_handler(error)

    print("Polling...")
    app.run_polling(poll_interval=1)