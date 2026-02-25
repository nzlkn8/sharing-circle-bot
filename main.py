from fastapi import FastAPI, Request, Response

app = FastAPI()

# Matches the "Verify Token" you'll put in Meta
MY_TOKEN = "SharingCircle2026"

@app.get("/webhook")
async def verify(request: Request):
    params = request.query_params
    if params.get("hub.verify_token") == MY_TOKEN:
        return Response(content=params.get("hub.challenge"), status_code=200)
    return "Error, wrong token", 403

@app.post("/webhook")
async def handle_message(request: Request):
    data = await request.json()
    try:
        # This looks for the link in the WhatsApp message
        msg_body = data['entry'][0]['changes'][0]['value']['messages'][0]['text']['body']
        sender = data['entry'][0]['changes'][0]['value']['messages'][0]['from']
        print(f"RECIEVED: {msg_body} FROM: {sender}")
    except:
        pass
    return Response(content="ok", status_code=200)
