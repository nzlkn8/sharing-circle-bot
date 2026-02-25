from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

app = FastAPI()

MY_TOKEN = "SharingCircle2026"

# 1. This handles the main URL (https://sharing-circle.onrender.com)
@app.get("/")
async def home():
    return {"status": "SharingCircle Brain is Online"}

# 2. This handles the Meta Handshake (MUST end in /webhook)
@app.get("/webhook")
async def verify(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == MY_TOKEN:
        return PlainTextResponse(content=challenge, status_code=200)
    
    return PlainTextResponse(content="Verification failed", status_code=403)

# 3. This handles the actual messages
@app.post("/webhook")
async def handle_message(request: Request):
    data = await request.json()
    try:
        # This will print the message in your Render logs
        msg = data['entry'][0]['changes'][0]['value']['messages'][0]['text']['body']
        sender = data['entry'][0]['changes'][0]['value']['messages'][0]['from']
        print(f"✅ LINK RECEIVED: {msg} FROM: {sender}")
    except:
        pass
    return {"status": "ok"}
