import discord
import aiohttp
import asyncio
import os
from discord.ext import commands
from aiohttp import web
import threading
import time
import json
from collections import defaultdict
import re
from datetime import datetime, timedelta

# Configuration from environment variables
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GROUPME_BOT_ID = os.getenv("GROUPME_BOT_ID")
GROUPME_ACCESS_TOKEN = os.getenv("GROUPME_ACCESS_TOKEN")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
GROUPME_GROUP_ID = os.getenv("GROUPME_GROUP_ID")
PORT = int(os.getenv("PORT", "8080"))  # Cloud Run default

# GroupMe API endpoints
GROUPME_POST_URL = "https://api.groupme.com/v3/bots/post"
GROUPME_IMAGE_UPLOAD_URL = "https://image.groupme.com/pictures"
GROUPME_GROUPS_URL = f"https://api.groupme.com/v3/groups/{GROUPME_GROUP_ID}"
GROUPME_MESSAGES_URL = f"https://api.groupme.com/v3/groups/{GROUPME_GROUP_ID}/messages"
GROUPME_POLLS_CREATE_URL = f"https://api.groupme.com/v3/poll/{GROUPME_GROUP_ID}"
GROUPME_POLLS_SHOW_URL = "https://api.groupme.com/v3/poll"  # + /{poll_id}
GROUPME_POLLS_LIST_URL = f"https://api.groupme.com/v3/groups/{GROUPME_GROUP_ID}/polls"

# Discord bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Global variables
bot_status = {"ready": False, "start_time": time.time()}
message_mapping = {}  # Maps Discord message IDs to GroupMe message IDs
groupme_to_discord = {}  # Maps GroupMe message IDs to Discord message IDs
recent_messages = defaultdict(list)  # Stores recent messages for threading context

# NEW: Poll mapping and tracking
poll_mapping = {}  # Maps Discord poll IDs to GroupMe poll IDs
groupme_poll_mapping = {}  # Maps GroupMe poll IDs to Discord poll IDs
active_polls = {}  # Stores active poll data for vote synchronization
poll_vote_tracking = defaultdict(dict)  # Tracks who voted for what to prevent loops

# Emoji mapping for reactions
EMOJI_MAPPING = {
    '❤️': '❤️', '👍': '👍', '👎': '👎', '😂': '😂', '😮': '😮', '😢': '😢', '😡': '😡',
    '✅': '✅', '❌': '❌', '🔥': '🔥', '💯': '💯', '🎉': '🎉', '👏': '👏', '💪': '💪',
    '🤔': '🤔', '😍': '😍', '🙄': '🙄', '😴': '😴', '🤷': '🤷', '🤦': '🤦', '💀': '💀',
    '🪩': '🪩'
}

def run_health_server():
    """Run health check server in a separate thread - Cloud Run compatible"""
    async def health_check(request):
        return web.json_response({
            "status": "healthy",
            "bot_ready": bot_status["ready"],
            "uptime": time.time() - bot_status["start_time"],
            "platform": "Google Cloud Run",
            "features": {
                "image_support": bool(GROUPME_ACCESS_TOKEN),
                "reactions": bool(GROUPME_ACCESS_TOKEN and GROUPME_GROUP_ID),
                "threading": True,
                "polls": bool(GROUPME_ACCESS_TOKEN and GROUPME_GROUP_ID)
            },
            "active_polls": len(active_polls)
        })

    async def groupme_webhook(request):
        """Handle GroupMe webhook events including polls"""
        try:
            data = await request.json()
            print(f"📨 GroupMe webhook received: {data}")
            
            # Handle poll events from GroupMe
            if data.get('group_id') == GROUPME_GROUP_ID:
                await handle_groupme_webhook_event(data)
                
            return web.json_response({"status": "success"})
        except Exception as e:
            print(f"❌ Error handling GroupMe webhook: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def start_server():
        app = web.Application()
        
        # Cloud Run requires health checks on root
        app.router.add_get('/', health_check)
        app.router.add_get('/health', health_check)
        app.router.add_get('/_ah/health', health_check)  # Google App Engine style
        app.router.add_post('/groupme/webhook', groupme_webhook)
        
        # Add CORS for Cloud Run
        app.router.add_options('/{path:.*}', lambda request: web.Response())
        
        runner = web.AppRunner(app)
        await runner.setup()
        
        # Cloud Run binds to 0.0.0.0 and uses PORT env var
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        print(f"🌐 Health check server running on 0.0.0.0:{PORT} (Google Cloud Run)")
        print(f"🔗 GroupMe webhook endpoint: https://your-service.a.run.app/groupme/webhook")
        
        # Keep server running
        try:
            while True:
                await asyncio.sleep(3600)  # Check every hour
        except asyncio.CancelledError:
            print("🛑 Health server shutting down...")
            await runner.cleanup()

    # Create new event loop for this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(start_server())
    except Exception as e:
        print(f"❌ Health server error: {e}")
    finally:
        loop.close()

async def get_groupme_poll(poll_id):
    """Fetch GroupMe poll data using native API"""
    if not GROUPME_ACCESS_TOKEN:
        return None
    
    async with aiohttp.ClientSession() as session:
        try:
            url = f"{GROUPME_POLLS_SHOW_URL}/{poll_id}?token={GROUPME_ACCESS_TOKEN}"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get('poll', {}).get('data', {})
                else:
                    print(f"❌ Failed to fetch GroupMe poll {poll_id}. Status: {resp.status}")
                    return None
        except Exception as e:
            print(f"❌ Error fetching GroupMe poll: {e}")
            return None

async def handle_groupme_webhook_event(data):
    """Handle incoming GroupMe webhook events including native poll events"""
    try:
        # Check if this is a poll-related event
        event = data.get('event', {})
        event_type = event.get('type', '')
        
        if event_type == 'poll.created':
            print("📊 GroupMe poll created event detected")
            await handle_groupme_poll_created(data)
        elif event_type == 'poll.vote':
            print("🗳️ GroupMe poll vote event detected")
            await handle_groupme_poll_vote(data)
        elif event_type == 'poll.ended':
            print("📊 GroupMe poll ended event detected")
            await handle_groupme_poll_ended(data)
        else:
            # Handle regular messages
            if data.get('sender_type') != 'bot' and data.get('name', '') != 'Bot':
                await forward_groupme_to_discord(data)
            
            # Also check for text-based poll patterns in regular messages
            message_text = data.get('text', '')
            if message_text and ('poll:' in message_text.lower() or '📊' in message_text):
                await handle_groupme_text_poll(data)
                
    except Exception as e:
        print(f"❌ Error processing GroupMe webhook event: {e}")

async def handle_groupme_poll_created(data):
    """Handle native GroupMe poll creation and forward to Discord"""
    try:
        event_data = data.get('event', {}).get('data', {})
        poll_data = event_data.get('poll', {})
        user_data = event_data.get('user', {})
        
        poll_id = poll_data.get('id')
        poll_subject = poll_data.get('subject', 'Poll')
        author_name = user_data.get('nickname', 'Unknown')
        
        if not poll_id:
            print("❌ No poll ID found in GroupMe poll created event")
            return
        
        # Fetch full poll data
        full_poll_data = await get_groupme_poll(poll_id)
        if not full_poll_data:
            print(f"❌ Could not fetch full poll data for {poll_id}")
            return
        
        options = [opt.get('title', '') for opt in full_poll_data.get('options', [])]
        
        if len(options) < 2:
            print("❌ GroupMe poll has insufficient options")
            return
        
        # Create corresponding Discord poll
        discord_channel = bot.get_channel(DISCORD_CHANNEL_ID)
        if discord_channel:
            await create_discord_poll_from_groupme_native(discord_channel, {
                'question': poll_subject,
                'options': options,
                'poll_id': poll_id
            }, author_name)
            
    except Exception as e:
        print(f"❌ Error handling GroupMe poll creation: {e}")

async def create_discord_poll_from_groupme_native(channel, poll_data, author_name):
    """Create a Discord poll from native GroupMe poll data"""
    try:
        question = poll_data['question']
        options = poll_data['options']
        groupme_poll_id = poll_data['poll_id']
        
        # Create Discord poll options
        poll_options = []
        for i, option in enumerate(options[:10]):  # Discord limit
            # Use simple number emojis for GroupMe polls
            emoji = f"{i+1}\u20e3"  # Number emoji (1️⃣, 2️⃣, etc.)
            poll_options.append(discord.PollMedia(text=option[:55], emoji=emoji))
        
        # Create the Discord poll
        poll = discord.Poll(
            question=f"📊 {question} (from {author_name})",
            options=poll_options,
            multiple=False,
            duration=24  # 24 hours
        )
        
        # Send poll to Discord
        poll_message = await channel.send(poll=poll)
        
        # Track the poll for vote synchronization
        poll_id = f"groupme_{groupme_poll_id}"
        active_polls[poll_id] = {
            'discord_message': poll_message,
            'discord_poll': poll,
            'groupme_poll_id': groupme_poll_id,
            'author': author_name,
            'created_at': time.time(),
            'source': 'groupme',
            'options': options
        }
        
        groupme_poll_mapping[groupme_poll_id] = poll_id
        
        print(f"✅ Created Discord poll from native GroupMe poll: {question}")
        
    except Exception as e:
        print(f"❌ Error creating Discord poll from native GroupMe: {e}")

async def handle_groupme_poll_vote(data):
    """Handle GroupMe poll vote events and sync to Discord"""
    try:
        event_data = data.get('event', {}).get('data', {})
        poll_data = event_data.get('poll', {})
        user_data = event_data.get('user', {})
        vote_data = event_data.get('vote', {})
        
        poll_id = poll_data.get('id')
        user_name = user_data.get('nickname', 'Unknown')
        option_title = vote_data.get('option', {}).get('title', 'Unknown option')
        
        if poll_id in groupme_poll_mapping:
            # This is a tracked poll, send notification to Discord
            discord_channel = bot.get_channel(DISCORD_CHANNEL_ID)
            if discord_channel:
                vote_notification = f"🗳️ **{user_name}** voted for: **{option_title}**"
                await discord_channel.send(vote_notification)
                print(f"✅ Forwarded GroupMe vote to Discord: {user_name} -> {option_title}")
        
    except Exception as e:
        print(f"❌ Error handling GroupMe poll vote: {e}")

async def handle_groupme_poll_ended(data):
    """Handle GroupMe poll end events"""
    try:
        event_data = data.get('event', {}).get('data', {})
        poll_data = event_data.get('poll', {})
        
        poll_id = poll_data.get('id')
        poll_subject = poll_data.get('subject', 'Poll')
        
        if poll_id in groupme_poll_mapping:
            # Send poll results to Discord
            discord_channel = bot.get_channel(DISCORD_CHANNEL_ID)
            if discord_channel:
                # Fetch final poll results
                final_poll_data = await get_groupme_poll(poll_id)
                if final_poll_data:
                    results_text = f"📊 **Poll Ended:** {poll_subject}\n\n**Results:**\n"
                    
                    for option in final_poll_data.get('options', []):
                        title = option.get('title', 'Unknown')
                        votes = option.get('votes', 0)
                        results_text += f"• **{title}**: {votes} votes\n"
                    
                    await discord_channel.send(results_text)
                    print(f"✅ Sent GroupMe poll results to Discord")
                
                # Clean up tracking
                tracked_poll_id = groupme_poll_mapping.get(poll_id)
                if tracked_poll_id:
                    active_polls.pop(tracked_poll_id, None)
                    groupme_poll_mapping.pop(poll_id, None)
        
    except Exception as e:
        print(f"❌ Error handling GroupMe poll end: {e}")

async def handle_groupme_text_poll(data):
    """Handle text-based poll patterns from GroupMe (fallback)"""
    try:
        message_text = data.get('text', '')
        sender_name = data.get('name', 'Unknown')
        
        # Parse poll from message text
        poll_data = parse_groupme_poll_text(message_text)
        if poll_data and len(poll_data['options']) >= 2:
            # Create corresponding Discord poll
            discord_channel = bot.get_channel(DISCORD_CHANNEL_ID)
            if discord_channel:
                await create_discord_poll_from_groupme_native(discord_channel, {
                    'question': poll_data['question'],
                    'options': poll_data['options'],
                    'poll_id': f"text_{int(time.time())}"  # Generate fake ID for text polls
                }, sender_name)
                
    except Exception as e:
        print(f"❌ Error handling GroupMe text poll: {e}")

async def create_groupme_poll_from_discord(poll, author_name, discord_message):
    """Create a native GroupMe poll from Discord poll using GroupMe's poll API"""
    try:
        if not GROUPME_ACCESS_TOKEN:
            print("❌ GROUPME_ACCESS_TOKEN required for native polls")
            return False
            
        print(f"🔍 Creating native GroupMe poll from Discord poll...")
        print(f"🔍 Poll object type: {type(poll)}")
        
        # Extract question - handle different possible formats
        if hasattr(poll.question, 'text'):
            question = poll.question.text
        elif hasattr(poll, 'question') and isinstance(poll.question, str):
            question = poll.question
        else:
            question = str(poll.question)
        
        print(f"📊 Extracted question: {question}")
        
        # Extract options - handle different possible formats
        options = []
        if hasattr(poll, 'answers'):
            for answer in poll.answers:
                if hasattr(answer, 'text'):
                    options.append(answer.text)
                else:
                    options.append(str(answer))
        elif hasattr(poll, 'options'):
            for option in poll.options:
                if hasattr(option, 'text'):
                    options.append(option.text)
                else:
                    options.append(str(option))
        
        print(f"📊 Extracted options: {options}")
        
        if not options or len(options) < 2:
            print("❌ Need at least 2 poll options")
            return False
        
        # Prepare GroupMe poll data
        poll_options = []
        for option in options[:10]:  # GroupMe max 10 options
            poll_options.append({"title": option[:160]})  # GroupMe max 160 chars per option
        
        # Set expiration (default to 24 hours from now)
        expiration_time = int(time.time()) + (24 * 60 * 60)  # 24 hours
        
        # Create poll payload for GroupMe API
        poll_payload = {
            "subject": f"{question[:160]}",  # GroupMe max 160 chars for subject
            "options": poll_options,
            "expiration": expiration_time,
            "type": "single",  # Discord polls are typically single choice
            "visibility": "public"
        }
        
        print(f"📊 Creating GroupMe poll with payload: {poll_payload}")
        
        # Create the poll using GroupMe's native API
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{GROUPME_POLLS_CREATE_URL}?token={GROUPME_ACCESS_TOKEN}",
                json=poll_payload,
                headers={'Content-Type': 'application/json'}
            ) as response:
                response_text = await response.text()
                print(f"🔍 GroupMe poll response status: {response.status}")
                print(f"🔍 GroupMe poll response: {response_text}")
                
                if response.status == 201:  # Created
                    poll_data = await response.json()
                    groupme_poll = poll_data.get('poll', {}).get('data', {})
                    groupme_poll_id = groupme_poll.get('id')
                    
                    print(f"✅ Created native GroupMe poll with ID: {groupme_poll_id}")
                    
                    # Track the poll for vote synchronization
                    poll_id = f"discord_{discord_message.id}"
                    active_polls[poll_id] = {
                        'discord_message': discord_message,
                        'discord_poll': poll,
                        'groupme_poll_id': groupme_poll_id,
                        'groupme_poll_data': groupme_poll,
                        'author': author_name,
                        'created_at': time.time(),
                        'source': 'discord',
                        'options': options
                    }
                    
                    poll_mapping[discord_message.id] = poll_id
                    groupme_poll_mapping[groupme_poll_id] = poll_id
                    
                    print(f"✅ Successfully created native GroupMe poll: {question}")
                    return True
                else:
                    print(f"❌ Failed to create GroupMe poll. Status: {response.status}, Response: {response_text}")
                    return False
                    
    except Exception as e:
        print(f"❌ Error creating native GroupMe poll: {e}")
        print(f"❌ Exception type: {type(e)}")
        import traceback
        print(f"❌ Traceback: {traceback.format_exc()}")
        return False

async def forward_groupme_to_discord(data):
    """Forward regular GroupMe messages to Discord"""
    try:
        sender_name = data.get('name', 'Unknown')
        message_text = data.get('text', '')
        
        if message_text and sender_name != 'Bot':
            discord_channel = bot.get_channel(DISCORD_CHANNEL_ID)
            if discord_channel:
                formatted_message = f"**{sender_name}**: {message_text}"
                sent_message = await discord_channel.send(formatted_message)
                
                # Store mapping for reactions
                groupme_msg_id = data.get('id')
                if groupme_msg_id:
                    groupme_to_discord[groupme_msg_id] = sent_message.id
                    message_mapping[sent_message.id] = groupme_msg_id
                
                print(f"✅ Forwarded GroupMe message to Discord: {message_text[:50]}...")
                
    except Exception as e:
        print(f"❌ Error forwarding GroupMe message to Discord: {e}")

async def get_groupme_message(message_id):
    """Fetch a specific GroupMe message by ID"""
    if not GROUPME_ACCESS_TOKEN or not GROUPME_GROUP_ID:
        return None
    
    async with aiohttp.ClientSession() as session:
        try:
            url = f"{GROUPME_MESSAGES_URL}?token={GROUPME_ACCESS_TOKEN}&limit=100"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    messages = data.get('response', {}).get('messages', [])
                    for msg in messages:
                        if msg.get('id') == message_id:
                            return msg
                    return None
                else:
                    print(f"❌ Failed to fetch GroupMe messages. Status: {resp.status}")
                    return None
        except Exception as e:
            print(f"❌ Error fetching GroupMe message: {e}")
            return None

async def send_reaction_to_groupme(message_id, emoji, user_name):
    """Send a reaction as a message to GroupMe"""
    if not GROUPME_ACCESS_TOKEN or not GROUPME_GROUP_ID:
        print("❌ GroupMe access token or group ID not available for reactions")
        return False
    
    # Check if this is a poll vote
    poll_vote_handled = await handle_poll_vote_to_groupme(message_id, emoji, user_name)
    if poll_vote_handled:
        return True
    
    # Get the original message to provide context
    original_msg = await get_groupme_message(message_id)
    if original_msg:
        original_text = original_msg.get('text', '')[:50]
        original_author = original_msg.get('name', 'Unknown')
        context = f"'{original_text}...' by {original_author}" if original_text else f"message by {original_author}"
    else:
        context = "a message"
    
    reaction_text = f"{user_name} reacted {emoji} to {context}"
    
    payload = {
        "bot_id": GROUPME_BOT_ID,
        "text": reaction_text
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(GROUPME_POST_URL, json=payload) as response:
                if response.status == 202:
                    print(f"✅ Reaction sent to GroupMe: {reaction_text}")
                    return True
                else:
                    print(f"❌ Failed to send reaction to GroupMe. Status: {response.status}")
                    return False
        except Exception as e:
            print(f"❌ Error sending reaction to GroupMe: {e}")
            return False

async def handle_poll_vote_to_groupme(message_id, emoji, user_name):
    """Handle poll votes from Discord to GroupMe"""
    try:
        # Check if this reaction is on a poll message
        for poll_id, poll_data in active_polls.items():
            if (poll_data.get('source') == 'discord' and 
                poll_data['discord_message'].id == message_id):
                
                # Check if emoji corresponds to a poll option
                option_emojis = poll_data.get('option_emojis', [])
                if emoji in option_emojis:
                    option_index = option_emojis.index(emoji)
                    option_text = poll_data['discord_poll'].answers[option_index].text
                    
                    # Prevent vote loops
                    vote_key = f"{poll_id}_{user_name}"
                    if vote_key in poll_vote_tracking:
                        return True  # Already handled
                    
                    poll_vote_tracking[vote_key] = {
                        'option': option_text,
                        'timestamp': time.time()
                    }
                    
                    # Send vote notification to GroupMe
                    vote_text = f"🗳️ {user_name} voted for: {option_text}"
                    payload = {
                        "bot_id": GROUPME_BOT_ID,
                        "text": vote_text
                    }
                    
                    async with aiohttp.ClientSession() as session:
                        async with session.post(GROUPME_POST_URL, json=payload) as response:
                            if response.status == 202:
                                print(f"✅ Poll vote forwarded to GroupMe: {vote_text}")
                                return True
                    
        return False
    except Exception as e:
        print(f"❌ Error handling poll vote to GroupMe: {e}")
        return False

async def upload_image_to_groupme(image_url):
    """Download image from Discord and upload to GroupMe"""
    if not GROUPME_ACCESS_TOKEN:
        print("❌ GroupMe access token not available for image upload")
        return None
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(image_url) as resp:
                if resp.status == 200:
                    image_data = await resp.read()
                    print(f"📥 Downloaded image from Discord ({len(image_data)} bytes)")
                else:
                    print(f"❌ Failed to download image from Discord. Status: {resp.status}")
                    return None
            
            data = aiohttp.FormData()
            data.add_field('file', image_data, filename='discord_image.png', content_type='image/png')
            
            async with session.post(
                GROUPME_IMAGE_UPLOAD_URL,
                data=data,
                headers={'X-Access-Token': GROUPME_ACCESS_TOKEN}
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    groupme_image_url = result['payload']['url']
                    print(f"📤 Successfully uploaded image to GroupMe: {groupme_image_url}")
                    return groupme_image_url
                else:
                    print(f"❌ Failed to upload image to GroupMe. Status: {resp.status}")
                    return None
                    
        except Exception as e:
            print(f"❌ Error handling image upload: {e}")
            return None

def detect_reply_context(message_content):
    """Detect if a message is replying to another message and extract context"""
    reply_patterns = [
        r'^Reply to @(\w+):\s*(.+)',
        r'^@(\w+)\s+(.+)',
        r'^>\s*(.+?)\n(.+)',
        r'^"(.+?)"\s*(.+)'
    ]
    
    for pattern in reply_patterns:
        match = re.match(pattern, message_content, re.DOTALL)
        if match:
            if len(match.groups()) == 2:
                return match.group(1), match.group(2)
    
    return None, message_content

async def send_to_groupme(message_text, author_name, image_url=None, reply_context=None):
    """Send a message to GroupMe with optional image and reply context"""
    
    if reply_context:
        quoted_text, reply_author = reply_context
        message_text = f"↪️ Replying to {reply_author}: \"{quoted_text[:50]}{'...' if len(quoted_text) > 50 else ''}\"\n\n{author_name}: {message_text}"
    else:
        reply_author, clean_message = detect_reply_context(message_text)
        if reply_author:
            message_text = f"↪️ Replying to {reply_author}:\n\n{author_name}: {clean_message}"
        else:
            message_text = f"{author_name}: {message_text}" if message_text.strip() else f"{author_name} sent an image"
    
    payload = {
        "bot_id": GROUPME_BOT_ID,
        "text": message_text
    }
    
    if image_url:
        payload["attachments"] = [{
            "type": "image",
            "url": image_url
        }]
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(GROUPME_POST_URL, json=payload) as response:
                if response.status == 202:
                    image_info = " with image" if image_url else ""
                    print(f"✅ Message sent to GroupMe{image_info}: {message_text[:50]}...")
                    return True
                else:
                    print(f"❌ Failed to send to GroupMe. Status: {response.status}")
                    return False
        except Exception as e:
            print(f"❌ Error sending to GroupMe: {e}")
            return False

@bot.event
async def on_ready():
    global bot_status
    bot_status["ready"] = True
    print(f'🤖 {bot.user} has connected to Discord!')
    print(f'📺 Monitoring channel ID: {DISCORD_CHANNEL_ID}')
    print(f'🖼️ Image support: {"✅" if GROUPME_ACCESS_TOKEN else "❌ (GROUPME_ACCESS_TOKEN not set)"}')
    print(f'🔗 GroupMe Group ID: {"✅" if GROUPME_GROUP_ID else "❌ (GROUPME_GROUP_ID not set)"}')
    print(f'😀 Reaction support: {"✅" if GROUPME_ACCESS_TOKEN and GROUPME_GROUP_ID else "❌"}')
    print(f'📊 Poll support: {"✅" if GROUPME_ACCESS_TOKEN and GROUPME_GROUP_ID else "❌"}')
    print(f'🧵 Threading support: ✅')
    print(f'☁️ Enhanced bot with poll support is ready and running on Google Cloud Run!')
    print(f'🌐 Server running on port: {PORT}')

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    
    if message.channel.id == DISCORD_CHANNEL_ID:
        print(f"📨 Processing message from {message.author.display_name}...")
        
        # Debug: Check message attributes
        print(f"🔍 Message type: {type(message)}")
        print(f"🔍 Has poll attribute: {hasattr(message, 'poll')}")
        
        # Check multiple ways to detect polls
        if hasattr(message, 'poll') and message.poll is not None:
            print(f"📊 POLL DETECTED! Type: {type(message.poll)}")
            print(f"📊 Poll question: {message.poll.question}")
            print(f"📊 Poll answers: {[str(answer) for answer in message.poll.answers]}")
            
            try:
                success = await create_groupme_poll_from_discord(message.poll, message.author.display_name, message)
                if success:
                    await message.add_reaction("✅")
                    print("✅ Poll creation successful")
                else:
                    await message.add_reaction("❌")
                    print("❌ Poll creation failed")
            except Exception as e:
                print(f"❌ Error creating GroupMe poll: {e}")
                await message.add_reaction("❌")
                # Send error info to Discord for debugging
                await message.reply(f"Poll error: {str(e)[:100]}")
            return
        
        # Check if message content looks like a poll
        if message.content:
            poll_keywords = ['poll:', '📊', 'vote:', 'survey:']
            content_lower = message.content.lower()
            if any(keyword in content_lower for keyword in poll_keywords):
                print(f"📊 Potential poll detected in content: {message.content[:100]}")
                await handle_text_based_poll(message)
        
        # Store message for threading context
        recent_messages[message.channel.id].append({
            'author': message.author.display_name,
            'content': message.content,
            'timestamp': time.time(),
            'message_id': message.id
        })
        
        # Keep only last 20 messages for context
        if len(recent_messages[message.channel.id]) > 20:
            recent_messages[message.channel.id].pop(0)
        
        # Handle replies
        reply_context = None
        if message.reference and message.reference.message_id:
            try:
                replied_message = await message.channel.fetch_message(message.reference.message_id)
                reply_context = (replied_message.content[:100], replied_message.author.display_name)
            except:
                pass
        
        # Handle images
        if message.attachments:
            for attachment in message.attachments:
                if attachment.content_type and attachment.content_type.startswith('image/'):
                    print(f"🖼️ Found image attachment: {attachment.filename}")
                    groupme_image_url = await upload_image_to_groupme(attachment.url)
                    
                    if groupme_image_url:
                        await send_to_groupme(message.content, message.author.display_name, 
                                            groupme_image_url, reply_context)
                    else:
                        await send_to_groupme(f"{message.content} [Image upload failed]", 
                                            message.author.display_name, reply_context=reply_context)
                else:
                    await send_to_groupme(f"{message.content} [Attached: {attachment.filename}]", 
                                        message.author.display_name, reply_context=reply_context)
        else:
            if message.content.strip():
                await send_to_groupme(message.content, message.author.display_name, 
                                    reply_context=reply_context)
    
    await bot.process_commands(message)

async def handle_text_based_poll(message):
    """Handle text-based poll detection when native polls aren't working"""
    try:
        content = message.content
        poll_data = parse_groupme_poll_text(content)
        
        if poll_data and len(poll_data['options']) >= 2:
            print(f"📊 Text-based poll detected: {poll_data}")
            
            # Send formatted poll to GroupMe
            option_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
            
            poll_text = f"📊 Poll from {message.author.display_name}: {poll_data['question']}\n\n"
            
            for i, option in enumerate(poll_data['options'][:10]):
                emoji = option_emojis[i] if i < len(option_emojis) else f"{i+1}."
                poll_text += f"{emoji} {option}\n"
            
            poll_text += f"\nReact with the corresponding number to vote! 🗳️"
            
            # Send to GroupMe
            success = await send_to_groupme(poll_text, "Poll Bot")
            if success:
                await message.add_reaction("📊")
                await message.reply("Poll forwarded to GroupMe!")
            
    except Exception as e:
        print(f"❌ Error handling text-based poll: {e}")
        await message.reply(f"Error processing poll: {str(e)[:100]}")

@bot.command(name='polltest')
async def poll_test(ctx):
    """Test Discord poll creation capabilities"""
    if ctx.channel.id == DISCORD_CHANNEL_ID:
        await ctx.send("Testing Discord poll support...")
        
        # Check if discord.py supports polls
        try:
            import discord
            print(f"Discord.py version: {discord.__version__}")
            
            # Check if Poll class exists
            if hasattr(discord, 'Poll'):
                await ctx.send(f"✅ Discord.py version {discord.__version__} - Poll class available")
                
                # Try to create a simple poll
                try:
                    poll_options = [
                        discord.PollMedia(text="Yes"),
                        discord.PollMedia(text="No")
                    ]
                    
                    poll = discord.Poll(
                        question="Can you see this poll?",
                        options=poll_options,
                        multiple=False,
                        duration=1
                    )
                    
                    poll_msg = await ctx.send(poll=poll)
                    await ctx.send("✅ Poll creation successful! Check if it forwards to GroupMe.")
                    
                    # Debug poll object
                    print(f"🔍 Created poll object: {type(poll)}")
                    print(f"🔍 Poll message: {type(poll_msg)}")
                    print(f"🔍 Poll message has poll: {hasattr(poll_msg, 'poll')}")
                    
                except Exception as e:
                    await ctx.send(f"❌ Poll creation failed: {e}")
                    print(f"Poll creation error: {e}")
                    
            else:
                await ctx.send(f"❌ Discord.py version {discord.__version__} - Poll class not available")
                await ctx.send("You may need to update discord.py: `pip install discord.py>=2.3.0`")
                
        except Exception as e:
            await ctx.send(f"❌ Error checking Discord.py: {e}")
    else:
        await ctx.send("❌ This command only works in the monitored channel.")

@bot.command(name='textpoll')
async def text_poll(ctx, *, poll_text):
    """Create a text-based poll that works regardless of Discord poll support"""
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        await ctx.send("❌ This command only works in the monitored channel.")
        return
    
    try:
        # Parse poll text
        if '?' not in poll_text:
            await ctx.send("❌ Format: `!textpoll Question? Option1, Option2, Option3`")
            return
            
        question, options_str = poll_text.split('?', 1)
        question = question.strip()
        
        options = [opt.strip() for opt in options_str.split(',') if opt.strip()]
        
        if len(options) < 2:
            await ctx.send("❌ Need at least 2 options.")
            return
        
        # Create poll text for both platforms
        option_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        
        discord_poll_text = f"📊 **{question}?**\n\n"
        groupme_poll_text = f"📊 Poll from {ctx.author.display_name}: {question}?\n\n"
        
        for i, option in enumerate(options[:10]):
            emoji = option_emojis[i] if i < len(option_emojis) else f"{i+1}️⃣"
            discord_poll_text += f"{emoji} {option}\n"
            groupme_poll_text += f"{emoji} {option}\n"
        
        discord_poll_text += "\nReact with the corresponding number to vote!"
        groupme_poll_text += "\nReact with the corresponding number to vote! 🗳️"
        
        # Send to Discord
        discord_msg = await ctx.send(discord_poll_text)
        
        # Add reaction options
        for i in range(min(len(options), 10)):
            await discord_msg.add_reaction(option_emojis[i])
        
        # Send to GroupMe
        payload = {
            "bot_id": GROUPME_BOT_ID,
            "text": groupme_poll_text
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(GROUPME_POST_URL, json=payload) as response:
                if response.status == 202:
                    await ctx.send("✅ Text-based poll created on both platforms!")
                else:
                    await ctx.send(f"⚠️ Discord poll created, GroupMe failed (status: {response.status})")
                    
    except Exception as e:
        await ctx.send(f"❌ Error creating text poll: {e}")
        print(f"Text poll error: {e}")

@bot.command(name='nativepoll')
async def create_native_poll(ctx, *, poll_text):
    """Create a native GroupMe poll directly: !nativepoll Question? Option1, Option2, Option3"""
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        await ctx.send("❌ This command only works in the monitored channel.")
        return
    
    if not GROUPME_ACCESS_TOKEN:
        await ctx.send("❌ GROUPME_ACCESS_TOKEN required for native polls")
        return
    
    try:
        # Parse the poll text
        if '?' not in poll_text:
            await ctx.send("❌ Format: `!nativepoll Question? Option1, Option2, Option3`")
            return
            
        question, options_str = poll_text.split('?', 1)
        question = question.strip()
        
        options = [opt.strip() for opt in options_str.split(',') if opt.strip()]
        
        if len(options) < 2:
            await ctx.send("❌ Need at least 2 options.")
            return
        
        if len(options) > 10:
            await ctx.send("⚠️ GroupMe supports max 10 options, truncating...")
            options = options[:10]
        
        # Prepare GroupMe poll data
        poll_options = []
        for option in options:
            poll_options.append({"title": option[:160]})  # GroupMe max 160 chars
        
        # Set expiration (24 hours from now)
        expiration_time = int(time.time()) + (24 * 60 * 60)
        
        poll_payload = {
            "subject": question[:160],  # GroupMe max 160 chars
            "options": poll_options,
            "expiration": expiration_time,
            "type": "single",
            "visibility": "public"
        }
        
        await ctx.send(f"Creating native GroupMe poll: **{question}?**")
        
        # Create the poll using GroupMe's native API
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{GROUPME_POLLS_CREATE_URL}?token={GROUPME_ACCESS_TOKEN}",
                json=poll_payload,
                headers={'Content-Type': 'application/json'}
            ) as response:
                response_text = await response.text()
                
                if response.status == 201:  # Created
                    poll_data = await response.json()
                    groupme_poll = poll_data.get('poll', {}).get('data', {})
                    poll_id = groupme_poll.get('id')
                    
                    await ctx.send(f"✅ Native GroupMe poll created! Poll ID: `{poll_id}`")
                    
                    # Also create Discord poll for comparison
                    try:
                        discord_poll_options = []
                        for i, option in enumerate(options):
                            emoji = f"{i+1}\u20e3"
                            discord_poll_options.append(discord.PollMedia(text=option[:55], emoji=emoji))
                        
                        discord_poll = discord.Poll(
                            question=f"{question}?",
                            options=discord_poll_options,
                            multiple=False,
                            duration=24
                        )
                        
                        await ctx.send(poll=discord_poll)
                        await ctx.send("✅ Discord poll created too for comparison!")
                        
                    except Exception as e:
                        await ctx.send(f"⚠️ GroupMe poll created, Discord poll failed: {e}")
                        
                else:
                    await ctx.send(f"❌ Failed to create GroupMe poll. Status: {response.status}")
                    await ctx.send(f"Response: {response_text[:200]}...")
                    
    except Exception as e:
        await ctx.send(f"❌ Error creating native poll: {e}")
        print(f"Native poll error: {e}")

@bot.command(name='listpolls')
async def list_groupme_polls(ctx):
    """List active GroupMe polls"""
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        await ctx.send("❌ This command only works in the monitored channel.")
        return
    
    if not GROUPME_ACCESS_TOKEN:
        await ctx.send("❌ GROUPME_ACCESS_TOKEN required to list polls")
        return
    
    try:
        # Get polls from GroupMe
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{GROUPME_POLLS_LIST_URL}?token={GROUPME_ACCESS_TOKEN}") as response:
                if response.status == 200:
                    data = await response.json()
                    polls = data.get('response', {}).get('polls', [])
                    
                    if not polls:
                        await ctx.send("📭 No active GroupMe polls found.")
                        return
                    
                    poll_list = []
                    for poll in polls[:10]:  # Show max 10
                        subject = poll.get('subject', 'No subject')[:50]
                        poll_id = poll.get('id', 'Unknown')
                        status = poll.get('status', 'unknown')
                        owner_id = poll.get('owner_id', 'Unknown')
                        
                        poll_list.append(f"📊 **{subject}...** (ID: {poll_id}, Status: {status})")
                    
                    embed = discord.Embed(
                        title="📊 Active GroupMe Polls",
                        description="\n".join(poll_list),
                        color=0x00ff00
                    )
                    await ctx.send(embed=embed)
                    
                else:
                    await ctx.send(f"❌ Failed to fetch GroupMe polls. Status: {response.status}")
                    
    except Exception as e:
        await ctx.send(f"❌ Error listing polls: {e}")

@bot.command(name='pollinfo')
async def get_poll_info(ctx, poll_id):
    """Get detailed info about a specific GroupMe poll"""
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        await ctx.send("❌ This command only works in the monitored channel.")
        return
    
    if not GROUPME_ACCESS_TOKEN:
        await ctx.send("❌ GROUPME_ACCESS_TOKEN required")
        return
    
    try:
        poll_data = await get_groupme_poll(poll_id)
        if not poll_data:
            await ctx.send(f"❌ Could not find poll with ID: {poll_id}")
            return
        
        subject = poll_data.get('subject', 'No subject')
        status = poll_data.get('status', 'unknown')
        created_at = poll_data.get('created_at', 0)
        expiration = poll_data.get('expiration', 0)
        poll_type = poll_data.get('type', 'unknown')
        visibility = poll_data.get('visibility', 'unknown')
        
        # Format timestamps
        created_time = datetime.fromtimestamp(created_at).strftime('%Y-%m-%d %H:%M:%S') if created_at else 'Unknown'
        expires_time = datetime.fromtimestamp(expiration).strftime('%Y-%m-%d %H:%M:%S') if expiration else 'Unknown'
        
        # Get options and votes
        options_text = ""
        total_votes = 0
        for i, option in enumerate(poll_data.get('options', []), 1):
            title = option.get('title', 'Unknown')
            votes = option.get('votes', 0)
            total_votes += votes
            options_text += f"{i}. **{title}**: {votes} votes\n"
        
        embed = discord.Embed(
            title=f"📊 Poll Info: {subject}",
            color=0x3498db
        )
        embed.add_field(name="📋 Details", value=f"""
**Status:** {status}
**Type:** {poll_type}
**Visibility:** {visibility}
**Total Votes:** {total_votes}
**Created:** {created_time}
**Expires:** {expires_time}
        """, inline=False)
        
        if options_text:
            embed.add_field(name="📊 Options & Results", value=options_text, inline=False)
        
        embed.add_field(name="🔍 Poll ID", value=f"`{poll_id}`", inline=False)
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ Error getting poll info: {e}")
    """Check what poll features are available"""
    if ctx.channel.id == DISCORD_CHANNEL_ID:
        try:
            import discord
            
            info = f"""🔍 **Poll Support Check**
**Discord.py version:** {discord.__version__}
**Poll class available:** {'✅' if hasattr(discord, 'Poll') else '❌'}
**PollMedia class available:** {'✅' if hasattr(discord, 'PollMedia') else '❌'}
**Bot permissions:** Checking...

**Environment:**
• GROUPME_BOT_ID: {'✅' if GROUPME_BOT_ID else '❌'}
• GROUPME_ACCESS_TOKEN: {'✅' if GROUPME_ACCESS_TOKEN else '❌'}
• GROUPME_GROUP_ID: {'✅' if GROUPME_GROUP_ID else '❌'}

**Recommended Actions:**
• If Poll class missing: Update discord.py
• If permissions missing: Check bot permissions
• Use `!textpoll` as alternative"""
            
            await ctx.send(info)
            
            # Check bot permissions
            permissions = ctx.channel.permissions_for(ctx.guild.me)
            perm_info = f"""**Bot Permissions in this channel:**
• Send Messages: {'✅' if permissions.send_messages else '❌'}
• Add Reactions: {'✅' if permissions.add_reactions else '❌'}
• Use External Emojis: {'✅' if permissions.use_external_emojis else '❌'}
• Embed Links: {'✅' if permissions.embed_links else '❌'}
• Manage Messages: {'✅' if permissions.manage_messages else '❌'}"""
            
            await ctx.send(perm_info)
            
        except Exception as e:
            await ctx.send(f"❌ Error checking poll support: {e}")
    else:
        await ctx.send("❌ This command only works in the monitored channel.")
        
        # Store message for threading context
        recent_messages[message.channel.id].append({
            'author': message.author.display_name,
            'content': message.content,
            'timestamp': time.time(),
            'message_id': message.id
        })
        
        # Keep only last 20 messages for context
        if len(recent_messages[message.channel.id]) > 20:
            recent_messages[message.channel.id].pop(0)
        
        # Handle replies
        reply_context = None
        if message.reference and message.reference.message_id:
            try:
                replied_message = await message.channel.fetch_message(message.reference.message_id)
                reply_context = (replied_message.content[:100], replied_message.author.display_name)
            except:
                pass
        
        # Handle images
        if message.attachments:
            for attachment in message.attachments:
                if attachment.content_type and attachment.content_type.startswith('image/'):
                    print(f"🖼️ Found image attachment: {attachment.filename}")
                    groupme_image_url = await upload_image_to_groupme(attachment.url)
                    
                    if groupme_image_url:
                        await send_to_groupme(message.content, message.author.display_name, 
                                            groupme_image_url, reply_context)
                    else:
                        await send_to_groupme(f"{message.content} [Image upload failed]", 
                                            message.author.display_name, reply_context=reply_context)
                else:
                    await send_to_groupme(f"{message.content} [Attached: {attachment.filename}]", 
                                        message.author.display_name, reply_context=reply_context)
        else:
            if message.content.strip():
                await send_to_groupme(message.content, message.author.display_name, 
                                    reply_context=reply_context)
    
    await bot.process_commands(message)

@bot.event
async def on_reaction_add(reaction, user):
    """Handle reactions added to messages"""
    if user.bot:
        return
    
    if reaction.message.channel.id == DISCORD_CHANNEL_ID:
        emoji = str(reaction.emoji)
        
        # Check if this is a supported emoji
        if emoji in EMOJI_MAPPING:
            print(f"😀 Processing reaction {emoji} from {user.display_name}")
            
            # Check if this message was sent from GroupMe (stored in our mapping)
            discord_msg_id = reaction.message.id
            if discord_msg_id in message_mapping:
                groupme_msg_id = message_mapping[discord_msg_id]
                success = await send_reaction_to_groupme(groupme_msg_id, emoji, user.display_name)
                if success:
                    print(f"✅ Reaction {emoji} forwarded to GroupMe")
            else:
                # This is a reaction to a Discord-originated message
                original_author = reaction.message.author.display_name
                original_content = reaction.message.content[:50] if reaction.message.content else "a message"
                context = f"'{original_content}...' by {original_author}" if original_content != "a message" else f"message by {original_author}"
                
                reaction_text = f"{user.display_name} reacted {emoji} to {context}"
                
                payload = {
                    "bot_id": GROUPME_BOT_ID,
                    "text": reaction_text
                }
                
                async with aiohttp.ClientSession() as session:
                    try:
                        async with session.post(GROUPME_POST_URL, json=payload) as response:
                            if response.status == 202:
                                print(f"✅ Discord reaction sent to GroupMe: {reaction_text}")
                    except Exception as e:
                        print(f"❌ Error sending Discord reaction to GroupMe: {e}")

@bot.command(name='test')
async def test_bridge(ctx):
    """Test command to verify the bridge is working"""
    if ctx.channel.id == DISCORD_CHANNEL_ID:
        await send_to_groupme("🧪 Enhanced bridge test message with poll support from Google Cloud Run!", "Bot Test")
        await ctx.send("✅ Test message sent to GroupMe!")
    else:
        await ctx.send("❌ This command only works in the monitored channel.")

@bot.command(name='testpoll')
async def test_poll(ctx):
    """Create a test poll to verify poll functionality"""
    if ctx.channel.id == DISCORD_CHANNEL_ID:
        await ctx.send("Creating test poll...")
        
        # Create a simple test poll
        try:
            poll_options = [
                discord.PollMedia(text="Red", emoji="🔴"),
                discord.PollMedia(text="Blue", emoji="🔵"),
                discord.PollMedia(text="Green", emoji="🟢")
            ]
            
            poll = discord.Poll(
                question="What's your favorite color?",
                options=poll_options,
                multiple=False,
                duration=1  # 1 hour
            )
            
            poll_message = await ctx.send(poll=poll)
            await ctx.send("✅ Test poll created! Check if it appears in GroupMe.")
            
            # Manually trigger the poll creation for testing
            try:
                await create_groupme_poll_from_discord(poll, ctx.author.display_name, poll_message)
                await ctx.send("🔄 Manually triggered GroupMe poll creation.")
            except Exception as e:
                await ctx.send(f"❌ Error creating GroupMe poll: {e}")
                
        except Exception as e:
            await ctx.send(f"❌ Error creating Discord poll: {e}")
            print(f"❌ Test poll error: {e}")
    else:
        await ctx.send("❌ This command only works in the monitored channel.")

@bot.command(name='status')
async def status(ctx):
    """Check bot status"""
    if ctx.channel.id == DISCORD_CHANNEL_ID:
        image_status = "✅" if GROUPME_ACCESS_TOKEN else "❌"
        reactions_status = "✅" if (GROUPME_ACCESS_TOKEN and GROUPME_GROUP_ID) else "❌"
        poll_status = "✅" if (GROUPME_ACCESS_TOKEN and GROUPME_GROUP_ID) else "❌"
        
        status_msg = f"""🟢 **Enhanced Bot Status**
🔗 Connected to GroupMe: {'✅' if GROUPME_BOT_ID else '❌'}
🖼️ Image support: {image_status}
😀 Reaction support: {reactions_status}
📊 Poll support: {poll_status}
🧵 Threading support: ✅
📊 Active polls: {len(active_polls)}
📈 Recent messages tracked: {len(recent_messages.get(DISCORD_CHANNEL_ID, []))}

**Supported Reactions:** {', '.join(EMOJI_MAPPING.keys())}
**Poll Features:** ✅ Discord→GroupMe ✅ GroupMe→Discord ✅ Vote Sync"""
        
        await ctx.send(status_msg)

@bot.command(name='polls')
async def list_polls(ctx):
    """List active tracked polls"""
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        await ctx.send("❌ This command only works in the monitored channel.")
        return
    
    if not active_polls:
        await ctx.send("📭 No active tracked polls currently.")
        await ctx.send("💡 **Try these commands:**\n• `!nativepoll Question? Option1, Option2` - Create native GroupMe poll\n• `!listpolls` - List all GroupMe polls\n• `!textpoll Question? Option1, Option2` - Create text-based poll")
        return
    
    poll_list = []
    for poll_id, poll_data in active_polls.items():
        source = "📱 Discord" if poll_data['source'] == 'discord' else "📞 GroupMe"
        age = int(time.time() - poll_data['created_at'])
        age_str = f"{age//3600}h {(age%3600)//60}m" if age >= 3600 else f"{age//60}m"
        
        if poll_data['source'] == 'discord':
            if hasattr(poll_data.get('discord_poll'), 'question'):
                if hasattr(poll_data['discord_poll'].question, 'text'):
                    question = poll_data['discord_poll'].question.text[:50]
                else:
                    question = str(poll_data['discord_poll'].question)[:50]
            else:
                question = "Unknown question"
        else:
            question = poll_data.get('groupme_poll', {}).get('question', 'Unknown')[:50]
        
        # Add GroupMe poll ID if available
        groupme_id = poll_data.get('groupme_poll_id', '')
        groupme_id_str = f" (ID: {groupme_id})" if groupme_id else ""
        
        poll_list.append(f"{source} **{question}...** (by {poll_data['author']}, {age_str} ago){groupme_id_str}")
    
    embed = discord.Embed(
        title="📊 Active Tracked Polls", 
        description="\n".join(poll_list), 
        color=0x00ff00
    )
    embed.add_field(
        name="💡 More Commands", 
        value="`!listpolls` - All GroupMe polls\n`!nativepoll` - Create native poll\n`!pollinfo <id>` - Poll details", 
        inline=False
    )
    await ctx.send(embed=embed)

@bot.command(name='react')
async def manual_react(ctx, emoji, *, message_context=None):
    """Manually send a reaction to GroupMe"""
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        await ctx.send("❌ This command only works in the monitored channel.")
        return
    
    if emoji not in EMOJI_MAPPING:
        await ctx.send(f"❌ Unsupported emoji. Supported: {', '.join(EMOJI_MAPPING.keys())}")
        return
    
    context = message_context or "the last message"
    reaction_text = f"{ctx.author.display_name} reacted {emoji} to {context}"
    
    payload = {
        "bot_id": GROUPME_BOT_ID,
        "text": reaction_text
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(GROUPME_POST_URL, json=payload) as response:
                if response.status == 202:
                    await ctx.send(f"✅ Reaction sent: {reaction_text}")
                else:
                    await ctx.send("❌ Failed to send reaction to GroupMe")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")

@bot.command(name='debug')
async def debug_info(ctx):
    """Show detailed debug information"""
    if ctx.channel.id == DISCORD_CHANNEL_ID:
        debug_msg = f"""🔍 **Debug Information (Google Cloud Run)**
**Environment Variables:**
• DISCORD_BOT_TOKEN: {'✅ Set' if DISCORD_BOT_TOKEN else '❌ Missing'}
• GROUPME_BOT_ID: {'✅ Set' if GROUPME_BOT_ID else '❌ Missing'} 
• GROUPME_ACCESS_TOKEN: {'✅ Set' if GROUPME_ACCESS_TOKEN else '❌ Missing'}
• GROUPME_GROUP_ID: {'✅ Set' if GROUPME_GROUP_ID else '❌ Missing'}
• DISCORD_CHANNEL_ID: {DISCORD_CHANNEL_ID}
• PORT: {PORT}

**Bot Status:**
• Platform: Google Cloud Run ☁️
• Bot Ready: {bot_status['ready']}
• Current Channel ID: {ctx.channel.id}
• Monitored Channel: {DISCORD_CHANNEL_ID}
• Channel Match: {'✅' if ctx.channel.id == DISCORD_CHANNEL_ID else '❌'}

**Active Data:**
• Active Polls: {len(active_polls)}
• Message Mappings: {len(message_mapping)}
• Recent Messages: {len(recent_messages.get(DISCORD_CHANNEL_ID, []))}

**API Endpoints:**
• GroupMe Post URL: {GROUPME_POST_URL}
• GroupMe Group: {GROUPME_GROUP_ID}
• Health Server Port: {PORT}
• Webhook URL: https://your-service.a.run.app/groupme/webhook

**Cloud Run Notes:**
• Make sure webhook URL points to your Cloud Run service
• Health checks available at: /, /health, /_ah/health"""
        
        await ctx.send(debug_msg)
    else:
        await ctx.send("❌ This command only works in the monitored channel.")

@bot.command(name='testgroupme')
async def test_groupme_connection(ctx):
    """Test basic GroupMe connection"""
    if ctx.channel.id == DISCORD_CHANNEL_ID:
        await ctx.send("Testing GroupMe connection...")
        
        test_message = "🧪 Direct GroupMe connection test"
        payload = {
            "bot_id": GROUPME_BOT_ID,
            "text": test_message
        }
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(GROUPME_POST_URL, json=payload) as response:
                    if response.status == 202:
                        await ctx.send("✅ GroupMe connection successful!")
                    else:
                        await ctx.send(f"❌ GroupMe connection failed. Status: {response.status}")
                        response_text = await response.text()
                        print(f"GroupMe API response: {response_text}")
            except Exception as e:
                await ctx.send(f"❌ GroupMe connection error: {e}")
    else:
        await ctx.send("❌ This command only works in the monitored channel.")
    """Show recent messages for threading context"""
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        await ctx.send("❌ This command only works in the monitored channel.")
        return
    
    recent = recent_messages.get(DISCORD_CHANNEL_ID, [])
    if not recent:
        await ctx.send("📭 No recent messages tracked.")
        return
    
    message_list = []
    for i, msg in enumerate(recent[-10:], 1):  # Show last 10 messages
        content = msg['content'][:50] + "..." if len(msg['content']) > 50 else msg['content']
        message_list.append(f"**{i}.** {msg['author']}: {content}")
    
    embed = discord.Embed(title="📋 Recent Messages", description="\n".join(message_list), color=0x00ff00)
    await ctx.send(embed=embed)

@bot.command(name='simplepoll')
async def simple_poll_test(ctx, *, poll_text):
    """Test poll creation with simple text format: !simplepoll Question? Option1, Option2, Option3"""
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        await ctx.send("❌ This command only works in the monitored channel.")
        return
    
    try:
        # Parse the poll text
        if '?' not in poll_text:
            await ctx.send("❌ Format: `!simplepoll Question? Option1, Option2, Option3`")
            return
            
        question, options_str = poll_text.split('?', 1)
        question = question.strip()
        
        options = [opt.strip() for opt in options_str.split(',') if opt.strip()]
        
        if len(options) < 2:
            await ctx.send("❌ Need at least 2 options. Format: `!simplepoll Question? Option1, Option2, Option3`")
            return
        
        if len(options) > 10:
            options = options[:10]  # Discord limit
            
        # Send poll to GroupMe first (simpler)
        option_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        
        poll_text_groupme = f"📊 Poll from {ctx.author.display_name}: {question}?\n\n"
        
        for i, option in enumerate(options):
            emoji = option_emojis[i] if i < len(option_emojis) else f"{i+1}."
            poll_text_groupme += f"{emoji} {option}\n"
        
        poll_text_groupme += f"\nReact with the corresponding number to vote! 🗳️"
        
        # Send to GroupMe
        payload = {
            "bot_id": GROUPME_BOT_ID,
            "text": poll_text_groupme
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(GROUPME_POST_URL, json=payload) as response:
                if response.status == 202:
                    await ctx.send(f"✅ Poll sent to GroupMe: {question}?")
                    
                    # Also create Discord poll
                    try:
                        poll_options = []
                        for i, option in enumerate(options):
                            emoji = option_emojis[i] if i < len(option_emojis) else None
                            poll_options.append(discord.PollMedia(text=option[:55], emoji=emoji))
                        
                        poll = discord.Poll(
                            question=f"{question}?",
                            options=poll_options,
                            multiple=False,
                            duration=24
                        )
                        
                        await ctx.send(poll=poll)
                        await ctx.send("✅ Discord poll created too!")
                        
                    except Exception as e:
                        await ctx.send(f"⚠️ GroupMe poll sent, but Discord poll failed: {e}")
                        
                else:
                    await ctx.send(f"❌ Failed to send poll to GroupMe. Status: {response.status}")
                    
    except Exception as e:
        await ctx.send(f"❌ Error creating poll: {e}")
        print(f"❌ Simple poll error: {e}")

@bot.command(name='recent')
async def show_recent(ctx):
    """Show recent messages for threading context"""
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        await ctx.send("❌ This command only works in the monitored channel.")
        return
    
    recent = recent_messages.get(DISCORD_CHANNEL_ID, [])
    if not recent:
        await ctx.send("📭 No recent messages tracked.")
        return
    
    message_list = []
    for i, msg in enumerate(recent[-10:], 1):  # Show last 10 messages
        content = msg['content'][:50] + "..." if len(msg['content']) > 50 else msg['content']
        message_list.append(f"**{i}.** {msg['author']}: {content}")
    
    embed = discord.Embed(title="📋 Recent Messages", description="\n".join(message_list), color=0x00ff00)
    await ctx.send(embed=embed)

# Cleanup old polls periodically
async def cleanup_old_polls():
    """Remove polls older than 24 hours"""
    while True:
        try:
            current_time = time.time()
            expired_polls = []
            
            for poll_id, poll_data in active_polls.items():
                if current_time - poll_data['created_at'] > 86400:  # 24 hours
                    expired_polls.append(poll_id)
            
            for poll_id in expired_polls:
                del active_polls[poll_id]
                print(f"🗑️ Cleaned up expired poll: {poll_id}")
            
            # Also cleanup old vote tracking
            old_votes = []
            for vote_key, vote_data in poll_vote_tracking.items():
                if current_time - vote_data['timestamp'] > 86400:
                    old_votes.append(vote_key)
            
            for vote_key in old_votes:
                del poll_vote_tracking[vote_key]
            
            await asyncio.sleep(3600)  # Check every hour
            
        except Exception as e:
            print(f"❌ Error in poll cleanup: {e}")
            await asyncio.sleep(3600)

if __name__ == "__main__":
    # Validate environment variables
    if not DISCORD_BOT_TOKEN:
        print("❌ DISCORD_BOT_TOKEN environment variable not set!")
        exit(1)
    
    if not GROUPME_BOT_ID:
        print("❌ GROUPME_BOT_ID environment variable not set!")
        exit(1)
    
    if DISCORD_CHANNEL_ID == 0:
        print("❌ DISCORD_CHANNEL_ID environment variable not set!")
        exit(1)
    
    if not GROUPME_ACCESS_TOKEN:
        print("⚠️ GROUPME_ACCESS_TOKEN not set - image uploads, reactions, and polls will be disabled")
    
    if not GROUPME_GROUP_ID:
        print("⚠️ GROUPME_GROUP_ID not set - advanced features including polls will be limited")
    
    # Start health check server
    print("🏥 Starting enhanced health check server with webhook support...")
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    
    time.sleep(2)
    
    # Start poll cleanup task
    asyncio.create_task(cleanup_old_polls())
    
    # Start Discord bot
    print("🚀 Starting Enhanced Discord to GroupMe bridge with poll support...")
    try:
        bot.run(DISCORD_BOT_TOKEN)
    except Exception as e:
        print(f"❌ Failed to start bot: {e}")
