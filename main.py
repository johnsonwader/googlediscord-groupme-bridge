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
PORT = int(os.getenv("PORT", "8000"))

# GroupMe API endpoints
GROUPME_POST_URL = "https://api.groupme.com/v3/bots/post"
GROUPME_IMAGE_UPLOAD_URL = "https://image.groupme.com/pictures"
GROUPME_GROUPS_URL = f"https://api.groupme.com/v3/groups/{GROUPME_GROUP_ID}"
GROUPME_MESSAGES_URL = f"https://api.groupme.com/v3/groups/{GROUPME_GROUP_ID}/messages"
GROUPME_POLLS_URL = f"https://api.groupme.com/v3/polls"

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
    """Run health check server in a separate thread"""
    async def health_check(request):
        return web.json_response({
            "status": "healthy",
            "bot_ready": bot_status["ready"],
            "uptime": time.time() - bot_status["start_time"],
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
                
            return web.Response(status=200)
        except Exception as e:
            print(f"❌ Error handling GroupMe webhook: {e}")
            return web.Response(status=500)

    async def start_server():
        app = web.Application()
        app.router.add_get('/', health_check)
        app.router.add_get('/health', health_check)
        app.router.add_post('/groupme/webhook', groupme_webhook)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        print(f"🏥 Health check server running on port {PORT}")
        print(f"🔗 GroupMe webhook endpoint: /groupme/webhook")
        
        while True:
            await asyncio.sleep(60)

    asyncio.new_event_loop().run_until_complete(start_server())

async def handle_groupme_webhook_event(data):
    """Handle incoming GroupMe webhook events"""
    try:
        # Check if this is a poll-related event
        message_text = data.get('text', '')
        sender_name = data.get('name', 'Unknown')
        
        # Detect poll creation pattern (GroupMe polls often include poll syntax)
        if 'poll:' in message_text.lower() or '📊' in message_text:
            await handle_groupme_poll_detection(data)
            
        # Handle regular messages from GroupMe to Discord
        if data.get('sender_type') != 'bot' and sender_name != 'Bot':
            await forward_groupme_to_discord(data)
            
    except Exception as e:
        print(f"❌ Error processing GroupMe webhook event: {e}")

async def handle_groupme_poll_detection(data):
    """Detect and handle GroupMe poll creation"""
    try:
        message_text = data.get('text', '')
        sender_name = data.get('name', 'Unknown')
        
        # Parse poll from message (GroupMe doesn't have native poll API, so we simulate)
        poll_data = parse_groupme_poll_text(message_text)
        if poll_data:
            # Create corresponding Discord poll
            discord_channel = bot.get_channel(DISCORD_CHANNEL_ID)
            if discord_channel:
                await create_discord_poll_from_groupme(discord_channel, poll_data, sender_name)
                
    except Exception as e:
        print(f"❌ Error handling GroupMe poll detection: {e}")

def parse_groupme_poll_text(text):
    """Parse poll information from GroupMe message text"""
    # Look for poll patterns like:
    # "📊 Poll: What's your favorite color? 1️⃣ Red 2️⃣ Blue 3️⃣ Green"
    # or "Poll: Question? A) Option1 B) Option2 C) Option3"
    
    poll_patterns = [
        r'(?:📊\s*)?[Pp]oll:\s*(.+?)\?\s*(.+)',
        r'📊\s*(.+?)\?\s*(.+)'
    ]
    
    for pattern in poll_patterns:
        match = re.match(pattern, text, re.DOTALL)
        if match:
            question = match.group(1).strip()
            options_text = match.group(2).strip()
            
            # Parse options
            options = []
            option_patterns = [
                r'(\d+)[️⃣]\s*([^1-9]+?)(?=\d+[️⃣]|$)',  # 1️⃣ Option
                r'([A-Z])\)\s*([^A-Z)]+?)(?=[A-Z]\)|$)',  # A) Option
                r'•\s*([^•\n]+)',  # • Option
                r'-\s*([^-\n]+)'   # - Option
            ]
            
            for opt_pattern in option_patterns:
                option_matches = re.findall(opt_pattern, options_text)
                if option_matches:
                    if isinstance(option_matches[0], tuple):
                        options = [match[1].strip() for match in option_matches]
                    else:
                        options = [match.strip() for match in option_matches]
                    break
            
            if len(options) >= 2:
                return {
                    'question': question,
                    'options': options[:10]  # Discord limit
                }
    
    return None

async def create_discord_poll_from_groupme(channel, poll_data, author_name):
    """Create a Discord poll from GroupMe poll data"""
    try:
        # Create poll options
        poll_options = []
        for i, option in enumerate(poll_data['options']):
            # Discord polls have emoji limits, so we'll use numbers
            emoji = f"{i+1}\u20e3"  # Number emoji (1️⃣, 2️⃣, etc.)
            poll_options.append(discord.PollMedia(text=option[:55], emoji=emoji))
        
        # Create the poll
        poll = discord.Poll(
            question=f"📊 {poll_data['question']} (from {author_name})",
            options=poll_options,
            multiple=False,
            duration=24  # 24 hours
        )
        
        # Send poll to Discord
        poll_message = await channel.send(poll=poll)
        
        # Track the poll
        poll_id = f"groupme_{int(time.time())}"
        active_polls[poll_id] = {
            'discord_message': poll_message,
            'groupme_poll': poll_data,
            'author': author_name,
            'created_at': time.time(),
            'source': 'groupme'
        }
        
        print(f"✅ Created Discord poll from GroupMe: {poll_data['question']}")
        
    except Exception as e:
        print(f"❌ Error creating Discord poll from GroupMe: {e}")

async def create_groupme_poll_from_discord(poll, author_name, discord_message):
    """Create a GroupMe poll representation from Discord poll"""
    try:
        question = poll.question.text
        options = [answer.text for answer in poll.answers]
        
        # Since GroupMe doesn't have native polls, we'll create a formatted message
        # with reaction tracking
        poll_text = f"📊 Poll from {author_name}: {question}\n\n"
        
        # Add options with number emojis
        option_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        
        for i, option in enumerate(options[:10]):
            emoji = option_emojis[i] if i < len(option_emojis) else f"{i+1}."
            poll_text += f"{emoji} {option}\n"
        
        poll_text += f"\nReact with the corresponding number to vote! 🗳️"
        
        # Send to GroupMe
        payload = {
            "bot_id": GROUPME_BOT_ID,
            "text": poll_text
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(GROUPME_POST_URL, json=payload) as response:
                if response.status == 202:
                    # Track the poll
                    poll_id = f"discord_{discord_message.id}"
                    active_polls[poll_id] = {
                        'discord_message': discord_message,
                        'discord_poll': poll,
                        'groupme_text': poll_text,
                        'author': author_name,
                        'created_at': time.time(),
                        'source': 'discord',
                        'option_emojis': option_emojis[:len(options)]
                    }
                    
                    poll_mapping[discord_message.id] = poll_id
                    
                    print(f"✅ Created GroupMe poll from Discord: {question}")
                    return True
                else:
                    print(f"❌ Failed to create GroupMe poll. Status: {response.status}")
                    return False
                    
    except Exception as e:
        print(f"❌ Error creating GroupMe poll from Discord: {e}")
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
    print(f'🚀 Enhanced bot with poll support is ready and running on Railway!')

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    
    if message.channel.id == DISCORD_CHANNEL_ID:
        print(f"📨 Processing message from {message.author.display_name}...")
        
        # Check if message contains a poll
        if message.poll:
            print(f"📊 Poll detected from {message.author.display_name}")
            await create_groupme_poll_from_discord(message.poll, message.author.display_name, message)
            return
        
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
        await send_to_groupme("🧪 Enhanced bridge test message with poll support from Railway!", "Bot Test")
        await ctx.send("✅ Test message sent to GroupMe!")
    else:
        await ctx.send("❌ This command only works in the monitored channel.")

@bot.command(name='testpoll')
async def test_poll(ctx):
    """Create a test poll to verify poll functionality"""
    if ctx.channel.id == DISCORD_CHANNEL_ID:
        # Create a simple test poll
        poll_options = [
            discord.PollMedia(text="Option A", emoji="🅰️"),
            discord.PollMedia(text="Option B", emoji="🅱️"),
            discord.PollMedia(text="Option C", emoji="🇨")
        ]
        
        poll = discord.Poll(
            question="🧪 Test Poll: Which option do you prefer?",
            options=poll_options,
            multiple=False,
            duration=1  # 1 hour
        )
        
        await ctx.send(poll=poll)
        await ctx.send("✅ Test poll created! It will be forwarded to GroupMe.")
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
    """List active polls"""
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        await ctx.send("❌ This command only works in the monitored channel.")
        return
    
    if not active_polls:
        await ctx.send("📭 No active polls currently.")
        return
    
    poll_list = []
    for poll_id, poll_data in active_polls.items():
        source = "📱 Discord" if poll_data['source'] == 'discord' else "📞 GroupMe"
        age = int(time.time() - poll_data['created_at'])
        age_str = f"{age//3600}h {(age%3600)//60}m" if age >= 3600 else f"{age//60}m"
        
        if poll_data['source'] == 'discord':
            question = poll_data['discord_poll'].question.text[:50]
        else:
            question = poll_data['groupme_poll']['question'][:50]
        
        poll_list.append(f"{source} **{question}...** (by {poll_data['author']}, {age_str} ago)")
    
    embed = discord.Embed(
        title="📊 Active Polls", 
        description="\n".join(poll_list), 
        color=0x00ff00
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
