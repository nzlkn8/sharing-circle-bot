import os
import re
import random
import string
import asyncio
import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse
from supabase import create_client

app = FastAPI()

# Environment variables
MY_TOKEN = "SharingCircle2026"
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY")
PHONE_NUMBER_ID = "918546528019408"
BASE_URL = "https://sharing-circle-web.vercel.app"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

processed_messages = set()

# --- Helpers ---

def extract_url(text):
    pattern = r'https?://[^\s]+'
    urls = re.findall(pattern, text)
    return urls[0] if urls else None

def generate_slug(name):
    clean = re.sub(r'[^a-z0-9]', '', name.lower())[:10]
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{clean}-{suffix}"

async def send_whatsapp_message(to, message):
    url = f"https://graph.facebook.com/v22.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message}
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers)
        print(f"[WhatsApp API] status={response.status_code} body={response.text}")

def strip_html(html):
    """Strip HTML tags and collapse whitespace."""
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

async def ai_process(content, is_url=True):
    """Get category, summary, title, thumbnail, and source_type from real content."""
    title = None
    thumbnail = None
    source_type = "link"
    page_text = ""

    if is_url:
        url = content
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            # Spotify
            if "open.spotify.com" in url:
                source_type = "spotify"
                try:
                    r = await client.get(f"https://open.spotify.com/oembed?url={url}")
                    data = r.json()
                    title = data.get("title", "")
                    thumbnail = data.get("thumbnail_url")
                    author_name = data.get("author_name", "")
                    if "/track/" in url:
                        if author_name:
                            summary = f"🎵 {title} by {author_name}"
                        else:
                            summary = f"🎵 {title}"
                        return "music", summary, title, thumbnail, source_type
                    elif "/album/" in url:
                        if " - " in title:
                            album, artist = title.rsplit(" - ", 1)
                            summary = f"💿 {album} by {artist}"
                        else:
                            summary = f"💿 {title}"
                        return "music", summary, title, thumbnail, source_type
                    elif "/playlist/" in url:
                        summary = f"🎵 Playlist: {title}"
                        return "music", summary, title, thumbnail, source_type
                    elif "/episode/" in url:
                        # Podcast episode — get description and pass to AI
                        description = data.get("description", "")
                        page_text = f"Podcast episode: {title}\n{description}"
                except Exception:
                    page_text = url
            # YouTube oEmbed — get title then summarize with AI
            elif "youtube.com" in url or "youtu.be" in url:
                source_type = "youtube"
                try:
                    r = await client.get(f"https://www.youtube.com/oembed?url={url}&format=json")
                    data = r.json()
                    title = data.get("title")
                    thumbnail = data.get("thumbnail_url")
                    page_text = f"YouTube video: {title}"
                except Exception:
                    pass
            # Generic URL — fetch and extract text
            if not page_text:
                try:
                    r = await client.get(url, headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    })
                    page_text = strip_html(r.text)[:3000]
                    # Try to pull <title> for display
                    title_match = re.search(r'<title[^>]*>([^<]+)</title>', r.text, re.IGNORECASE)
                    if title_match and not title:
                        title = title_match.group(1).strip()
                    # Try og:image for thumbnail
                    thumb_match = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', r.text, re.IGNORECASE)
                    if thumb_match and not thumbnail:
                        thumbnail = thumb_match.group(1).strip()
                except Exception:
                    page_text = url

        is_youtube = "youtube.com" in url or "youtu.be" in url
        is_podcast = "open.spotify.com" in url and "/episode/" in url
        if is_youtube:
            prompt = f"""Analyze this YouTube video and provide:
1. Category (one of: music, markets, health, news, tech, food, travel, sports, entertainment, other)
2. Exactly 1-2 bullet points about what this video is likely about, based on the title

Content: {page_text}

Respond in this exact format:
CATEGORY: [category]
BULLETS:
• [point 1]
• [optional point 2]"""
        elif is_podcast:
            prompt = f"""Analyze this podcast episode and provide:
1. Category (one of: music, markets, health, news, tech, food, travel, sports, entertainment, other)
2. Exactly 2-3 bullet points summarizing what this episode is about

Content: {page_text}

Respond in this exact format:
CATEGORY: [category]
BULLETS:
• [point 1]
• [point 2]
• [optional point 3]"""
        else:
            prompt = f"""Analyze this web content and provide:
1. Category (one of: music, markets, health, news, tech, food, travel, sports, entertainment, other)
2. Exactly 2-3 bullet points summarizing what this is about (each bullet on its own line starting with •)

Content: {page_text}

Respond in this exact format:
CATEGORY: [category]
BULLETS:
• [point 1]
• [point 2]
• [optional point 3]"""
    else:
        prompt = f"""Analyze this thought/message and provide:
1. Category (one of: music, markets, health, news, tech, food, travel, sports, entertainment, other)
2. A 1-2 sentence restatement of the key point

Text: {content}

Respond in this exact format:
CATEGORY: [category]
SUMMARY: [summary]"""

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        result = response.json()
        text = result["content"][0]["text"]

        category = "other"
        summary = content
        in_bullets = False
        bullet_lines = []

        for line in text.split('\n'):
            if line.startswith("CATEGORY:"):
                category = line.replace("CATEGORY:", "").strip().lower()
            elif line.startswith("BULLETS:"):
                in_bullets = True
            elif in_bullets and line.strip().startswith("•"):
                bullet_lines.append(line.strip())
            elif line.startswith("SUMMARY:"):
                summary = line.replace("SUMMARY:", "").strip()

        if bullet_lines:
            summary = "\n".join(bullet_lines)

        return category, summary, title, thumbnail, source_type

def get_user(phone):
    result = supabase.table("users").select("*").eq("phone_number", phone).execute()
    return result.data[0] if result.data else None

async def handle_new_user(phone):
    await send_whatsapp_message(phone,
        "👋 Welcome to SharingCircle!\n\n"
        "The best things you find online — music, articles, ideas — deserve better than getting lost in a group chat.\n\n"
        "Here's how it works:\n"
        "- Send me any link or thought → it goes to your circle\n"
        "- Your circle gets a beautiful weekly digest every Sunday\n"
        "- Everyone has their own personal feed\n\n"
        "Let's get you set up. What's your first name?")
    supabase.table("users").insert({
        "phone_number": phone,
        "name": "__awaiting_name__"
    }).execute()

async def handle_onboarding(user, phone, text):
    if user["name"] == "__awaiting_name__":
        slug = generate_slug(text)
        supabase.table("users").update({
            "name": text,
            "feed_slug": slug
        }).eq("phone_number", phone).execute()
        await send_whatsapp_message(phone,
            f"Nice to meet you, {text}! 👋\n\nWhat's your email address? (Used for your daily digest)")
        return True
    
    if not user.get("email"):
        slug = user.get("feed_slug") or generate_slug(user["name"])
        supabase.table("users").update({
            "email": text,
            "feed_slug": slug
        }).eq("phone_number", phone).execute()
        
        feed_url = f"{BASE_URL}/u/{slug}"
        setup_url = f"{BASE_URL}/setup/{slug}"
        
        await send_whatsapp_message(phone,
            f"🎉 You're all set, {user['name']}!\n\n"
            f"📱 Your feed: {feed_url}\n"
            f"⚠️ Keep this link private — it's public to anyone who has it.\n\n"
            f"👥 Add friends to your circle: {setup_url}\n\n"
            f"Send me any link to try it now!")
        return True
    
    return False

async def handle_command(phone, text, user):
    cmd = text.lower().strip()
    
    if cmd == "help":
        await send_whatsapp_message(phone,
            "📖 *SharingCircle Commands*\n\n"
            "*my feed* — get your feed URL\n"
            "*my circle* — see who's in your circle\n"
            "*my links* — see your recent shares\n"
            "*delete last* — remove your last post\n"
            "*pause* — stop sending to your circle\n"
            "*resume* — resume sending to your circle\n"
            "*help* — show this message")
        return True
    
    if cmd == "my feed":
        feed_url = f"{BASE_URL}/u/{user['feed_slug']}"
        await send_whatsapp_message(phone,
            f"📡 Your feed: {feed_url}\n"
            f"⚠️ Keep this link private — it's public to anyone who has it.")
        return True

    if cmd == "my circle":
        circle = supabase.table("circle").select("*").eq("sender_phone", phone).execute()
        if not circle.data:
            await send_whatsapp_message(phone, 
                f"Your circle is empty! Add friends at:\n{BASE_URL}/setup/{user['feed_slug']}")
        else:
            names = [f"• {r['recipient_name']}" for r in circle.data]
            await send_whatsapp_message(phone,
                f"👥 *Your Circle* ({len(circle.data)} people)\n\n" + "\n".join(names))
        return True
    
    if cmd == "my links":
        posts = supabase.table("posts").select("*").eq("phone_number", phone).order("created_at", desc=True).limit(5).execute()
        if not posts.data:
            await send_whatsapp_message(phone, "You haven't shared anything yet!")
        else:
            items = []
            for p in posts.data:
                if p["type"] == "link":
                    items.append(f"🔗 {p['content'][:50]}...")
                else:
                    items.append(f"💭 {p['content'][:50]}...")
            await send_whatsapp_message(phone, 
                "📋 *Your recent shares:*\n\n" + "\n".join(items))
        return True
    
    if cmd == "delete last":
        posts = supabase.table("posts").select("*").eq("phone_number", phone).order("created_at", desc=True).limit(1).execute()
        if posts.data:
            supabase.table("posts").delete().eq("id", posts.data[0]["id"]).execute()
            await send_whatsapp_message(phone, "✅ Last post deleted.")
        else:
            await send_whatsapp_message(phone, "Nothing to delete!")
        return True
    
    if cmd == "pause":
        supabase.table("users").update({"is_paused": True}).eq("phone_number", phone).execute()
        await send_whatsapp_message(phone, "⏸ Sharing paused. Your circle won't receive new posts until you resume.")
        return True
    
    if cmd == "resume":
        supabase.table("users").update({"is_paused": False}).eq("phone_number", phone).execute()
        await send_whatsapp_message(phone, "▶️ Sharing resumed!")
        return True
    
    return False

async def handle_post(phone, text, user):
    if user.get("is_paused"):
        await send_whatsapp_message(phone, "⏸ Your sharing is paused. Type *resume* to start sharing again.")
        return
    
    url = extract_url(text)
    is_link = url is not None
    content = url if is_link else text
    post_type = "link" if is_link else "thought"

    # Save post immediately with placeholder AI data
    category = "other" if is_link else "thought"
    result = supabase.table("posts").insert({
        "phone_number": phone,
        "type": post_type,
        "content": content,
        "category": category,
        "summary": "",
        "title": None,
        "thumbnail": None,
        "source_type": post_type
    }).execute()
    post_id = result.data[0]["id"] if result.data else None

    # Get circle
    circle = supabase.table("circle").select("*").eq("sender_phone", phone).execute()
    circle_count = len(circle.data) if circle.data else 0

    if is_link:
        await send_whatsapp_message(phone,
            f"🔗 Saved! Sent to {circle_count} people in your circle.")
    else:
        await send_whatsapp_message(phone,
            f"💭 Saved! Sent to {circle_count} people in your circle.")

    # Run AI processing in background so feed gets enriched data
    if is_link and post_id:
        async def enrich_post():
            try:
                category, summary, title, thumbnail, source_type = await ai_process(content, is_url=True)
                supabase.table("posts").update({
                    "category": category,
                    "summary": summary,
                    "title": title,
                    "thumbnail": thumbnail,
                    "source_type": source_type
                }).eq("id", post_id).execute()
            except:
                pass
        asyncio.create_task(enrich_post())

# --- Routes ---

@app.get("/")
async def home():
    return {"status": "SharingCircle Brain is Online"}

@app.get("/webhook")
async def verify(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == MY_TOKEN:
        return PlainTextResponse(content=challenge, status_code=200)
    return PlainTextResponse(content="Verification failed", status_code=403)

async def process_message(phone, text, message_id):
    try:
        user = get_user(phone)

        if not user:
            await handle_new_user(phone)
            return

        if user["name"] is None or user["name"] == "__awaiting_name__" or not user.get("email"):
            await handle_onboarding(user, phone, text)
            return

        handled = await handle_command(phone, text, user)
        if handled:
            return

        await handle_post(phone, text, user)

    except Exception as e:
        print(f"Error processing message {message_id}: {e}")


@app.post("/webhook")
async def handle_message(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    try:
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]

        if "messages" not in value:
            return {"status": "no message"}

        message = value["messages"][0]
        message_id = message["id"]

        if message_id in processed_messages:
            print(f"[Dedup] Skipping duplicate message {message_id}")
            return {"status": "ok"}
        processed_messages.add(message_id)

        phone = message["from"]
        text = message.get("text", {}).get("body", "").strip()

        if not text:
            return {"status": "ok"}

        background_tasks.add_task(process_message, phone, text, message_id)

    except Exception as e:
        print(f"Error: {e}")

    return {"status": "ok"}
