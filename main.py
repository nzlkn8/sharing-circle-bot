import os
import re
import random
import string
import httpx
from fastapi import FastAPI, Request
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
CIRCLE_LIMIT = 15

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

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

async def ai_process(content, is_url=True):
    """Get category and summary from Claude API"""
    if is_url:
        prompt = f"""Analyze this URL and provide:
1. Category (one of: music, markets, health, news, tech, food, travel, sports, entertainment, other)
2. A 2-3 sentence summary of what this link is likely about based on the URL

URL: {content}

Respond in this exact format:
CATEGORY: [category]
SUMMARY: [summary]"""
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
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        result = response.json()
        text = result["content"][0]["text"]
        
        category = "other"
        summary = content
        
        for line in text.split('\n'):
            if line.startswith("CATEGORY:"):
                category = line.replace("CATEGORY:", "").strip().lower()
            elif line.startswith("SUMMARY:"):
                summary = line.replace("SUMMARY:", "").strip()
        
        return category, summary

def get_user(phone):
    result = supabase.table("users").select("*").eq("phone_number", phone).execute()
    return result.data[0] if result.data else None

async def handle_new_user(phone):
    await send_whatsapp_message(phone,
        "👋 Welcome to SharingCircle! I help you share links and thoughts with your closest friends.\n\nWhat's your first name?")
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
            f"Your feed: {feed_url}\n"
            f"⚠️ Keep this link private — it's public to anyone who has it.\n\n"
            f"Build your circle here: {setup_url}\n\n"
            f"Send me any link or thought anytime to share with your circle.")
        return True
    
    return False

async def handle_command(phone, text, user):
    cmd = text.lower().strip()
    
    if cmd == "help":
        await send_whatsapp_message(phone,
            "📖 *SharingCircle Commands*\n\n"
            "*my circle* — see who's in your circle\n"
            "*my links* — see your recent shares\n"
            "*delete last* — remove your last post\n"
            "*pause* — stop sending to your circle\n"
            "*resume* — resume sending to your circle\n"
            "*help* — show this message")
        return True
    
    if cmd == "my circle":
        circle = supabase.table("circle").select("*").eq("sender_phone", phone).execute()
        if not circle.data:
            await send_whatsapp_message(phone, 
                f"Your circle is empty! Add friends at:\n{BASE_URL}/setup/{user['feed_slug']}")
        else:
            names = [f"• {r['recipient_name']}" for r in circle.data]
            await send_whatsapp_message(phone,
                f"👥 *Your Circle* ({len(circle.data)}/{CIRCLE_LIMIT})\n\n" + "\n".join(names))
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
    
    # AI processing
    try:
        category, summary = await ai_process(content, is_url=is_link)
    except:
        category = "other"
        summary = content
    
    # Save post
    supabase.table("posts").insert({
        "phone_number": phone,
        "type": post_type,
        "content": content,
        "category": category,
        "summary": summary
    }).execute()
    
    # Get circle
    circle = supabase.table("circle").select("*").eq("sender_phone", phone).execute()
    circle_count = len(circle.data) if circle.data else 0
    
    emoji = "🔗" if is_link else "💭"
    await send_whatsapp_message(phone,
        f"{emoji} Saved! Category: *{category}*\n"
        f"Sent to {circle_count} people in your circle.\n\n"
        f"_{summary}_")

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

@app.post("/webhook")
async def handle_message(request: Request):
    data = await request.json()
    try:
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]

        if "messages" not in value:
            return {"status": "no message"}

        message = value["messages"][0]
        phone = message["from"]
        text = message.get("text", {}).get("body", "").strip()

        if not text:
            return {"status": "no text"}

        user = get_user(phone)

        if not user:
            await handle_new_user(phone)
            return {"status": "ok"}

        if user["name"] is None or user["name"] == "__awaiting_name__" or not user.get("email"):
            await handle_onboarding(user, phone, text)
            return {"status": "ok"}

        handled = await handle_command(phone, text, user)
        if handled:
            return {"status": "ok"}

        await handle_post(phone, text, user)

    except Exception as e:
        print(f"Error: {e}")

    return {"status": "ok"}
