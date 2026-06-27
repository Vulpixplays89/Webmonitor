import time
import requests
import logging
from telebot import TeleBot
from pymongo import MongoClient
from threading import Thread
from bson import ObjectId
import re
from flask import Flask

# --- NEW IMPORTS ---
import asyncio
import aiohttp


# Telegram bot token and MongoDB connection details
BOT_TOKEN = "8120748600:AAHWKZSwocxdaD4d7qchNRO920Z5kQl5q60"
MONGO_URL = "mongodb+srv://botplays:botplays@vulpix.ffdea.mongodb.net/?retryWrites=true&w=majority&appName=Vulpix"

# Admin list (Telegram user IDs)
ADMINS = [6897739611]  # Replace with admin IDs

app = Flask('')

@app.route('/')
def home():
    return "I am alive"

def run_http_server():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_http_server)
    t.start()

# Initialize the Telegram bot and MongoDB client
bot = TeleBot(BOT_TOKEN)
client = MongoClient(MONGO_URL)
db = client.website_monitoring  # Database name
websites_collection = db.websites  # Collection for website monitoring
users_collection = db.users  # Collection for tracking users

# Configure logging
logging.basicConfig(
    filename="monitor.log",
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)


async def fetch_status(session, website):
    """
    Asynchronously checks a single website.
    Returns a tuple of (website_data, is_up_boolean).
    """
    url = website["website_url"]
    try:
        # We use a strict 10-second timeout. 
        # If it doesn't respond by then, we consider it down.
        async with session.get(url, timeout=10) as response:
            return website, response.status < 500
    except Exception as e:
        logging.warning(f"Error checking website {url}: {e}")
        return website, False

async def check_all_websites(websites):
    """
    Fires off all HTTP requests simultaneously.
    """
    async with aiohttp.ClientSession() as session:
        # Create a concurrent task for every website in the list
        tasks = [fetch_status(session, w) for w in websites]
        # Gather and return all results at once
        return await asyncio.gather(*tasks)

def monitor_websites():
    """
    The main monitoring loop running in a background thread.
    It gathers URLs, uses async to check them instantly, then updates MongoDB.
    """
    while True:
        try:
            websites = list(websites_collection.find())
            current_time = time.time()

            websites_to_check = [
                w for w in websites 
                if current_time - w.get("last_checked_time", 0) >= 60
            ]

            if websites_to_check:
                results = asyncio.run(check_all_websites(websites_to_check))

                for website, is_up in results:
                    chat_id = website["chat_id"]
                    url = website["website_url"]
                    last_update_time = website.get("last_update_time", 0)
                    
                    # Track if the website was already known to be down
                    was_down = website.get("is_currently_down", False)

                    # Prepare updates for MongoDB
                    update_data = {"last_checked_time": current_time}

                    if not is_up:
                        # Only send alert if it just went down
                        if not was_down:
                            send_telegram_message(chat_id, f"⚠️ Alert: The website {url} is down!")
                            update_data["is_currently_down"] = True
                    else:
                        # If it is up, but was previously down, send a recovery message
                        if was_down:
                            send_telegram_message(chat_id, f"✅ Recovery: The website {url} is back online!")
                            update_data["is_currently_down"] = False
                            update_data["last_update_time"] = current_time
                        
                        # Otherwise, just send the 6-hour routine check-in
                        elif current_time - last_update_time >= 6 * 60 * 60:
                            send_telegram_message(chat_id, f"✅ Status Update: The website {url} is still up and running!")
                            update_data["last_update_time"] = current_time

                    # Update the database once per website with the new status
                    websites_collection.update_one(
                        {"_id": website["_id"]}, 
                        {"$set": update_data}
                    )

            time.sleep(5) 
            
        except Exception as e:
            logging.error(f"Error in monitoring loop: {e}")
            time.sleep(10)



def send_telegram_message(chat_id, message):
    """
    Sends a message to the specified Telegram chat.
    Handles any API errors gracefully.
    """
    try:
        bot.send_message(chat_id, message)
    except Exception as e:
        logging.error(f"Error sending message to {chat_id}: {e}")

@bot.message_handler(commands=['start'])
def handle_start(message):
    """
    Handles the /start command.
    Sends a personalized welcome message.
    """
    chat_id = message.chat.id
    first_name = message.from_user.first_name

    # Add user to the users collection
    users_collection.update_one({"chat_id": chat_id}, {"$set": {"first_name": first_name}}, upsert=True)

    try:
        bot.send_message(chat_id, f"Welcome {first_name}!\n\n"
                                  "You can monitor your favorite websites and get notified if they go down.\n\n"
                                  "Commands:\n"
                                  "/addwebsite <url> - Add a website for monitoring.\n"
                                  "/list - List your monitored websites.\n"
                                  "/remove <website_id> - Remove a monitored website.\n\n"
                                  "Monitoring happens every 30 seconds with updates every 6 hours.")
    except Exception as e:
        logging.error(f"Error handling /start for chat {chat_id}: {e}")

@bot.message_handler(commands=['addwebsite'])
def handle_addwebsite(message):
    """
    Handles the /addwebsite command to add a website for monitoring.
    """
    chat_id = message.chat.id
    try:
        website_url = message.text.split()[1]
        if not website_url.startswith("http"):
            bot.send_message(chat_id, "Please provide a valid URL (starting with http or https).")
            return
        # Add the website to the database
        websites_collection.insert_one({
            "chat_id": chat_id,
            "website_url": website_url,
            "last_checked_time": 0,
            "last_update_time": 0
        })
        bot.send_message(chat_id, f"Website monitoring started for URL: {website_url}")
    except IndexError:
        bot.send_message(chat_id, "Please provide a valid URL. Example:\n/addwebsite <url>")
    except Exception as e:
        logging.error(f"Error adding website for chat {chat_id}: {e}")
        bot.send_message(chat_id, "An error occurred while adding the website.")

@bot.message_handler(commands=['list'])
def handle_list(message):
    """
    Handles the /list command to display monitored websites for the user.
    """
    chat_id = message.chat.id
    try:
        websites = list(websites_collection.find({"chat_id": chat_id}))
        if not websites:
            bot.send_message(chat_id, "You are not monitoring any websites.")
            return

        response = "Your monitored websites:\n"
        for website in websites:
            response += f"- ID: {website['_id']} | URL: {website['website_url']}\n"
        bot.send_message(chat_id, response)
    except Exception as e:
        logging.error(f"Error listing websites for chat {chat_id}: {e}")
        bot.send_message(chat_id, "An error occurred while fetching your monitored websites.")

@bot.message_handler(commands=['remove'])
def handle_remove(message):
    """
    Handles the /remove command to remove a monitored website.
    """
    chat_id = message.chat.id
    try:
        website_id = message.text.split()[1]
        result = websites_collection.delete_one({"_id": ObjectId(website_id), "chat_id": chat_id})
        if result.deleted_count > 0:
            bot.send_message(chat_id, "The website has been removed.")
        else:
            bot.send_message(chat_id, "Website not found or you don't have permission to remove it.")
    except IndexError:
        bot.send_message(chat_id, "Please provide a valid website ID. Example:\n/remove <website_id>")
    except Exception as e:
        logging.error(f"Error removing website for chat {chat_id}: {e}")
        bot.send_message(chat_id, "An error occurred while removing the website.")
        
@bot.message_handler(commands=['help'])
def handle_help(message):
    """
    Handles the /help command.
    Sends a list of all available commands to the user.
    """
    chat_id = message.chat.id

    try:
        bot.send_message(
            chat_id,
            "Here are the available commands:\n\n"
            "/start - Start the bot and get a welcome message.\n"
            "/help - Show this help message with a list of all commands.\n"
            "/addwebsite <url> - Add a website to monitor.\n"
            "/list - List all websites you are monitoring.\n"
            "/remove <website_id> - Remove a monitored website.\n\n"
            "Admins Only:\n"
            "/broadcast <message> - Send a broadcast message to all users."
        )
    except Exception as e:
        logging.error(f"Error handling /help for chat {chat_id}: {e}")
        
@bot.message_handler(commands=['broadcast'])
def handle_broadcast(message):
    """
    Handles the /broadcast command to send a message to all users.
    Only accessible by admins.
    """
    chat_id = message.chat.id
    if chat_id not in ADMINS:
        bot.send_message(chat_id, "You do not have permission to use this command.")
        return

    try:
        broadcast_message = " ".join(message.text.split()[1:])
        if not broadcast_message:
            bot.send_message(chat_id, "Please provide a message to broadcast. Example:\n/broadcast <message>")
            return

        users = users_collection.find()
        sent_count = 0  # Initialize counter for successful messages

        for user in users:
            try:
                send_telegram_message(user["chat_id"], f"{broadcast_message}")
                sent_count += 1
            except Exception as e:
                logging.error(f"Error sending broadcast to {user['chat_id']}: {e}")

        bot.send_message(chat_id, f"Broadcast message sent successfully to {sent_count} users.")
    except Exception as e:
        logging.error(f"Error broadcasting message: {e}")
        bot.send_message(chat_id, "An error occurred while broadcasting the message.")


# Start monitoring in a separate thread
monitor_thread = Thread(target=monitor_websites)
monitor_thread.start()


if __name__ == "__main__":
    keep_alive()
    
    while True:
        try:
            bot.polling(none_stop=True)
        except Exception as e:
            print(f"Error occurred: {str(e)}")
            time.sleep(5)
    
