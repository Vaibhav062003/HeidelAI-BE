from telegram import InputMediaPhoto, Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from dotenv import load_dotenv
import os

load_dotenv()

BOT_USERNAME = os.getenv('BOT_USERNAME2')
TOKEN = os.getenv('TOKEN2')


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



async def carousel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Define the keyboard with buttons to simulate next/previous
    keyboard = [
        [InlineKeyboardButton("Next Item", callback_data="next_item")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Send the first "card" with the inline keyboard
    await update.message.reply_photo(
        photo="downloaded_image.jpg",  # Replace with the first item image URL
        caption="This is the first item of the carousel",
        reply_markup=reply_markup
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # Acknowledge the callback
    
    # Handle the "Next Item" button press
    if query.data == "next_item":
        keyboard = [
            [InlineKeyboardButton("Previous Item", callback_data="previous_item"),
             InlineKeyboardButton("Next Item", callback_data="next_item")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Edit the message with the new image and text (simulate next item)
        await query.edit_message_media(
            media=InputMediaPhoto("https://egeniestores.com/wp-content/uploads/2024/04/81njZjDqc6L._SL1500_.jpg")
        )
        await query.edit_message_caption(
            caption="This is the next item of the carousel",  # Replace with next item caption
            reply_markup=reply_markup
        )
    
    # Handle the "Previous Item" button press if needed
    if query.data == "previous_item":
        keyboard = [
            [InlineKeyboardButton("Next Item", callback_data="next_item")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Edit the message with the new image and text (simulate next item)
        await query.edit_message_media(
            media=InputMediaPhoto("https://egeniestores.com/wp-content/uploads/2024/04/71atfTZpTaL._SL1500_.jpg")
        )
        await query.edit_message_caption(
            caption="This is the first item of the carousel",  # Replace with next item caption
            reply_markup=reply_markup
        )







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
     # Register existing /start command
    app.add_handler(CommandHandler('start', start_command))

    # Register the new /carousel command
    app.add_handler(CommandHandler('carousel', carousel_command))

    # Register button handler for inline keyboard button clicks
    app.add_handler(CallbackQueryHandler(button_handler))

    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Errors
    app.add_error_handler(error)

    print("Polling...")
    app.run_polling(poll_interval=1)