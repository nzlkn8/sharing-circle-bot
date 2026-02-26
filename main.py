import os
import re
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from supabase import create_client
import httpx

app = FastAPI()

# Environment variables
MY_TOKEN = "SharingCircle2026"
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = "918546528019408"

# Supabase client
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def extract_url(text):
    pattern = r'https?://[^\s]+'
    urls = re.findall(pattern, text)
    return urls[0] if urls else None

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
        await client.post(url, json=payload, headers=headers)

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
        from_number = message["from"]
        text = message.get("text", {}).get("body", "")
        
        # Register user if not exists
        existing = supabase.table("users").select("*").eq("phone_number", from_number).execute()
        if not existing.data:
            supabase.table("users").insert({"phone_number": from_number}).execute()
        
        # Extract URL
        url = extract_url(text)
        
        if url:
            # Save link to database
            supabase.table("links").insert({
                "phone_number": from_number,
                "url": url,
                "category": "general"
            }).execute()
            await send_whatsapp_message(from_number, f"✅ Link saved to your SharingCircle!\n\n{url}")
        else:
            await send_whatsapp_message(from_number, "👋 Welcome to SharingCircle! Send me any link and I'll save it to your feed.")
    
    except Exception as e:
        print(f"Error: {e}")
    
    return {"status": "ok"}
