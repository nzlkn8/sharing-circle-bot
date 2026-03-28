import os
import re
import random
import string
import asyncio
import urllib.parse
import httpx
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse
from supabase import create_client
from apscheduler.schedulers.asyncio import AsyncIOScheduler

app = FastAPI()
scheduler = AsyncIOScheduler()

# Environment variables
MY_TOKEN = "SharingCircle2026"
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
PHONE_NUMBER_ID = "918546528019408"
BASE_URL = "https://favefinds.app"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

NOTIFY_ON_ADD = False

processed_messages = set()

# --- Helpers ---

def normalize_phone(phone):
    return re.sub(r'\D', '', phone)

def is_valid_email(text):
    return bool(re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', text.strip()))

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
            # Apple Music
            if "music.apple.com" in url and ("/album/" in url or "/song/" in url or "/music-video/" in url):
                try:
                    r = await client.get(f"https://music.apple.com/oembed?url={urllib.parse.quote(url, safe='')}")
                    data = r.json()
                    title = data.get("title", "")
                    thumbnail = data.get("thumbnail_url")
                    author_name = data.get("author_name", "")
                    summary = f"🎵 {title} by {author_name}" if author_name else f"🎵 {title}"
                    return "music", summary, title, thumbnail, source_type
                except Exception:
                    pass
            # Apple Podcasts
            elif "podcasts.apple.com" in url:
                try:
                    r = await client.get(f"https://music.apple.com/oembed?url={urllib.parse.quote(url, safe='')}")
                    data = r.json()
                    title = data.get("title", "")
                    thumbnail = data.get("thumbnail_url")
                    return "podcast", "", title, thumbnail, source_type
                except Exception:
                    pass
            # Spotify
            elif "open.spotify.com" in url:
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
            # YouTube — detect podcast by keywords, return early (no AI)
            elif "youtube.com" in url or "youtu.be" in url:
                source_type = "youtube"
                podcast_keywords = [
                    "episode", "ep.", "ep ", "#", "podcast", "interview",
                    "conversation with", "talk with", "talks with", "with guest", "season"
                ]
                try:
                    r = await client.get(f"https://www.youtube.com/oembed?url={url}&format=json")
                    data = r.json()
                    title = data.get("title", "")
                    thumbnail = data.get("thumbnail_url")
                    author_name = data.get("author_name", "")
                    combined = f"{title} {author_name}".lower()
                    if any(kw in combined for kw in podcast_keywords):
                        return "podcast", "", title, thumbnail, source_type
                    else:
                        return "other", "", title, thumbnail, source_type
                except Exception:
                    pass
            # No-scrape blocklist — return OG title/thumbnail only, no AI summary
            elif any(d in url for d in ["x.com", "twitter.com", "instagram.com", "facebook.com", "tiktok.com", "linkedin.com"]):
                try:
                    r = await client.get(url, headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    })
                    og_title_match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', r.text, re.IGNORECASE)
                    title_match = re.search(r'<title[^>]*>([^<]+)</title>', r.text, re.IGNORECASE)
                    thumb_match = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', r.text, re.IGNORECASE)
                    if og_title_match:
                        title = og_title_match.group(1).strip()
                    elif title_match:
                        title = title_match.group(1).strip()
                    if thumb_match:
                        thumbnail = thumb_match.group(1).strip()
                except Exception:
                    pass
                return "other", "", title, thumbnail, source_type
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

        is_spotify_episode = "open.spotify.com" in url and "/episode/" in url
        if is_spotify_episode:
            prompt = f"""Analyze this podcast episode and provide:
1. Exactly 2-3 bullet points summarizing what this episode is about. Put each bullet point on a new line.

Content: {page_text}

Respond in this exact format:
CATEGORY: podcast
BULLETS:
- [point 1]
- [point 2]
- [optional point 3]"""
        else:
            prompt = f"""Analyze this web content and provide:
1. Category (one of: article, other)
2. Exactly 2-3 bullet points summarizing what this is about. Put each bullet point on a new line.

Content: {page_text}

Respond in this exact format:
CATEGORY: [category]
BULLETS:
- [point 1]
- [point 2]
- [optional point 3]"""
    else:
        prompt = f"""Analyze this thought/message and provide:
1. Category (one of: music, podcast, article, other)
2. Exactly 2-3 bullet points summarizing the key points. Put each bullet point on a new line.

Text: {content}

Respond in this exact format:
CATEGORY: [category]
BULLETS:
- [point 1]
- [point 2]
- [optional point 3]"""

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
            elif in_bullets and line.strip().startswith("-"):
                bullet_lines.append(line.strip())

        if bullet_lines:
            summary = "\n".join(bullet_lines)

        return category, summary, title, thumbnail, source_type

def get_user(phone):
    result = supabase.table("users").select("*").eq("phone_number", phone).execute()
    return result.data[0] if result.data else None

def schedule_message(phone, message, send_at):
    supabase.table("scheduled_messages").insert({
        "phone_number": phone,
        "message": message,
        "send_at": send_at.isoformat(),
        "sent": False,
    }).execute()

async def process_contacts_batch(sender_phone, sender_name, contacts):
    """
    Process all contacts in a list, adding new ones to the sender's circle.
    Returns (added, skipped, at_limit, new_circle_count).
    """
    circle_result = supabase.table("circle").select("recipient_phone").eq("sender_phone", sender_phone).execute()
    circle_phones = {r["recipient_phone"] for r in (circle_result.data or [])}
    circle_count = len(circle_phones)

    added = 0
    skipped = 0
    at_limit = False

    for contact in contacts:
        name = contact.get("name", {}).get("formatted_name", "Unknown")
        phones = contact.get("phones", [])
        if not phones:
            continue
        contact_phone = normalize_phone(phones[0].get("phone", ""))
        if not contact_phone:
            continue

        if contact_phone in circle_phones:
            skipped += 1
            continue

        if circle_count >= 50:
            at_limit = True
            break

        existing = supabase.table("users").select("email").eq("phone_number", contact_phone).execute()
        contact_email = None
        if existing.data and existing.data[0].get("email"):
            contact_email = existing.data[0]["email"]

        duplicate_check = supabase.table("circle").select("sender_phone").eq("sender_phone", sender_phone).eq("recipient_phone", contact_phone).execute()
        if duplicate_check.data:
            skipped += 1
            continue

        supabase.table("circle").insert({
            "sender_phone": sender_phone,
            "recipient_name": name,
            "recipient_phone": contact_phone,
            "recipient_email": contact_email
        }).execute()

        circle_phones.add(contact_phone)
        circle_count += 1
        added += 1

        if NOTIFY_ON_ADD:
            recipient_user = supabase.table("users").select("phone_number").eq("phone_number", contact_phone).execute()
            if recipient_user.data:
                await send_whatsapp_message(contact_phone,
                    f"Hey! {sender_name} ({sender_phone}) added you to their FaveFinds. "
                    f"You'll start seeing their shares in your feed and next Sunday digest 🎉")
            else:
                await send_whatsapp_message(contact_phone,
                    f"Hey! {sender_name} ({sender_phone}) added you to their FaveFinds. "
                    f"Type *JOIN* to sign up and see your friends' favorite finds — music, articles, podcasts. "
                    f"Takes less than 2 minutes! 🎉")

    return added, skipped, at_limit, circle_count


def build_contacts_summary(added, skipped, onboarding=True):
    suffix = " Send more contacts or type *done* to continue." if onboarding else ""
    if added == 0 and skipped > 0:
        return f"ℹ️ {'Those contacts are' if skipped > 1 else 'That contact is'} already in your FaveFinds.{suffix}"
    elif skipped > 0:
        return (f"✅ Added {added} {'person' if added == 1 else 'people'} "
                f"({skipped} already in your FaveFinds).{suffix}")
    else:
        return f"✅ Added {added} {'person' if added == 1 else 'people'} to your FaveFinds!{suffix}"

async def handle_onboarding(user, phone, message):
    step = user.get("onboarding_step", "awaiting_name")
    msg_type = message.get("type")
    text = message.get("text", {}).get("body", "").strip() if msg_type == "text" else ""

    if step == "awaiting_name":
        if not text:
            return
        slug = generate_slug(text)
        supabase.table("users").update({
            "name": text,
            "feed_slug": slug,
            "onboarding_step": "awaiting_email"
        }).eq("phone_number", phone).execute()
        await send_whatsapp_message(phone,
            f"Nice to meet you, {text}! 👋\n\nWhat's your email address? (We'll send you a weekly digest every Sunday)")
        return

    if step == "awaiting_email":
        if not text:
            return
        supabase.table("users").update({
            "email": text,
            "onboarding_step": "awaiting_circle_contact"
        }).eq("phone_number", phone).execute()
        await send_whatsapp_message(phone,
            "Let's add some people to your FaveFinds! Tap + → Contact to share contact cards — you can select multiple at once.\n\n"
            "You can always send contacts to this chat anytime to add more friends later.")
        return

    if step == "awaiting_circle_contact":
        if msg_type == "contacts":
            contacts = message.get("contacts", [])
            if not contacts:
                return
            sender_name = user.get("name", phone)
            added, skipped, at_limit, circle_count = await process_contacts_batch(phone, sender_name, contacts)
            if at_limit:
                await send_whatsapp_message(phone,
                    "You've reached the 50 person limit. Remove someone to add a new person.")
                return
            supabase.table("users").update({"onboarding_step": "awaiting_first_link"}).eq("phone_number", phone).execute()
            await send_whatsapp_message(phone,
                "🎉 All set! Last step — share your first find. A song, podcast, or article you've enjoyed recently. Just paste the link here!")
        else:
            supabase.table("users").update({"onboarding_step": "awaiting_first_link"}).eq("phone_number", phone).execute()
            await send_whatsapp_message(phone,
                "🎉 Last step — share your first find. A song, podcast, or article you've enjoyed recently. Just paste the link here!")
        return

    if step == "awaiting_more_contacts":
        if msg_type == "contacts":
            contacts = message.get("contacts", [])
            if not contacts:
                return
            sender_name = user.get("name", phone)
            added, skipped, at_limit, circle_count = await process_contacts_batch(phone, sender_name, contacts)
            if at_limit:
                await send_whatsapp_message(phone,
                    "You've reached the 50 person limit. Remove someone to add a new person.")
                return
            supabase.table("users").update({"onboarding_step": "awaiting_first_link"}).eq("phone_number", phone).execute()
            await send_whatsapp_message(phone,
                "🎉 All set! Last step — share your first find. A song, podcast, or article you've enjoyed recently. Just paste the link here!")
        return

    if step == "awaiting_first_link":
        content_text = text if text else ""
        if not content_text and msg_type != "text":
            return
        slug = user["feed_slug"]
        feed_url = f"{BASE_URL}/u/{slug}"
        setup_url = f"{BASE_URL}/setup/{slug}"

        url = extract_url(content_text)
        is_link = url is not None
        content = url if is_link else content_text
        post_type = "link" if is_link else "thought"
        category = "other" if is_link else "thought"
        caption = content_text.replace(url, "").strip() if is_link else None

        result = supabase.table("posts").insert({
            "phone_number": phone,
            "type": post_type,
            "content": content,
            "category": category,
            "summary": "",
            "title": None,
            "thumbnail": None,
            "source_type": post_type,
            "caption": caption
        }).execute()
        post_id = result.data[0]["id"] if result.data else None

        user_email = supabase.table("users").select("email").eq("phone_number", phone).execute()
        user_email = user_email.data[0]["email"] if user_email.data else None

        supabase.table("users").update({"onboarding_step": "complete"}).eq("phone_number", phone).execute()

        # Retroactively fill in email across all circles this user has been added to
        async def update_circle_email():
            try:
                if user_email:
                    supabase.table("circle").update({"recipient_email": user_email}).eq("recipient_phone", phone).execute()
            except Exception as e:
                print(f"[Onboarding] Failed to update circle email for {phone}: {e}")
        asyncio.create_task(update_circle_email())

        await send_whatsapp_message(phone,
            f"🎉 Perfect! Your people will see this in their feed and weekly digest.\n\n"
            f"📱 Your feed: {feed_url}\n"
            f"👥 Manage your people: {setup_url}")

        await send_whatsapp_message(phone,
            "Quick commands:\n"
            "*help* — all commands\n"
            "*my people* — see who's in your FaveFinds\n"
            "*my links* — your recent shares\n"
            "*my feed* — get your feed link")

        if post_id:
            async def enrich_first_post():
                try:
                    cat, summary, title, thumbnail, source_type = await ai_process(content, is_url=is_link)
                    supabase.table("posts").update({
                        "category": cat,
                        "summary": summary,
                        "title": title,
                        "thumbnail": thumbnail,
                        "source_type": source_type
                    }).eq("id", post_id).execute()
                except:
                    pass
            asyncio.create_task(enrich_first_post())
        return

async def handle_command(phone, text, user):
    cmd = text.lower().strip()

    if cmd == "help":
        await send_whatsapp_message(phone,
            "📖 *FaveFinds Commands*\n\n"
            "*my feed* — get your feed URL\n"
            "*my people* — see who's in your FaveFinds\n"
            "*my links* — see your recent shares\n"
            "*delete last* — remove your last post\n"
            "*pause* — stop sending to your people\n"
            "*resume* — resume sending to your people\n"
            "*stop prompts* — turn off sharing reminders\n"
            "*prompts on* — turn reminders back on\n"
            "*help* — show this message")
        return True

    if cmd in ("stop", "stop prompts", "no more reminders", "stop reminders"):
        supabase.table("users").update({"prompts_enabled": False}).eq("phone_number", phone).execute()
        await send_whatsapp_message(phone,
            "Got it — no more reminders! Type *prompts on* anytime to turn them back on.")
        return True

    if cmd == "prompts on":
        supabase.table("users").update({"prompts_enabled": True}).eq("phone_number", phone).execute()
        await send_whatsapp_message(phone,
            "Reminders back on! I'll nudge you a couple times a week if you haven't shared recently.")
        return True

    if cmd == "my feed":
        feed_url = f"{BASE_URL}/u/{user['feed_slug']}"
        await send_whatsapp_message(phone,
            f"📡 Your feed: {feed_url}\n"
            f"⚠️ Keep this link private — it's public to anyone who has it.")
        return True

    if cmd in ("my circle", "my people"):
        circle = supabase.table("circle").select("*").eq("sender_phone", phone).execute()
        if not circle.data:
            await send_whatsapp_message(phone,
                f"You haven't added anyone yet! Add friends at:\n{BASE_URL}/setup/{user['feed_slug']}")
        else:
            names = [f"• {r['recipient_name']}" for r in circle.data]
            await send_whatsapp_message(phone,
                f"👥 *Your People* ({len(circle.data)} people)\n\n" + "\n".join(names))
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
        await send_whatsapp_message(phone, "⏸ Sharing paused. Your people won't receive new posts until you resume.")
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

    # Text-only messages: guide user to share a link instead
    if not is_link:
        await send_whatsapp_message(phone,
            "FaveFinds is for sharing links — articles, music, podcasts. Got a link? Send it here! "
            "You can add a few words of context alongside the link if you'd like 🔗")
        return

    content = url
    # Extract caption: text surrounding the URL
    caption = text.replace(url, "").strip() or None

    # Save post immediately with placeholder AI data
    result = supabase.table("posts").insert({
        "phone_number": phone,
        "type": "link",
        "content": content,
        "category": "other",
        "summary": "",
        "title": None,
        "thumbnail": None,
        "source_type": "link",
        "caption": caption
    }).execute()
    post_id = result.data[0]["id"] if result.data else None

    # Run AI processing then send confirmation
    if post_id:
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
            await send_whatsapp_message(phone, "✅ Nice find — sent to your people!")
        asyncio.create_task(enrich_post())
    else:
        await send_whatsapp_message(phone, "✅ Nice find — sent to your people!")


# --- Digest ---

async def send_digest(phone_number, period):
    try:
        user_result = supabase.table("users").select("*").eq("phone_number", phone_number).execute()
        if not user_result.data:
            return
        user = user_result.data[0]
        email = user.get("email")
        if not email:
            return

        circle_result = supabase.table("circle").select("*").eq("recipient_phone", phone_number).execute()
        if not circle_result.data:
            return

        sender_phones = [row["sender_phone"] for row in circle_result.data]
        sender_names = {row["sender_phone"]: row["recipient_name"] for row in circle_result.data}

        # Also pull the actual sender's name from users table for accuracy
        for sp in sender_phones:
            u = supabase.table("users").select("name").eq("phone_number", sp).execute()
            if u.data and u.data[0].get("name"):
                sender_names[sp] = u.data[0]["name"]

        days = 7 if period == "weekly" else 1
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        posts_result = supabase.table("posts").select("*").in_("phone_number", sender_phones).gte("created_at", cutoff).order("created_at", desc=True).execute()
        posts = posts_result.data or []

        if not posts:
            return

        # Group by sender
        by_sender = {}
        for post in posts:
            sp = post["phone_number"]
            by_sender.setdefault(sp, []).append(post)

        period_label = "this week" if period == "weekly" else "today"
        subject = f"Your FaveFinds weekly digest 🔗" if period == "weekly" else "Your FaveFinds daily digest 🔗"

        sender_sections = ""
        for sp, sender_posts in by_sender.items():
            sname = sender_names.get(sp, sp)
            items_html = ""
            for p in sender_posts:
                thumbnail_html = ""
                if p.get("thumbnail"):
                    thumbnail_html = f'<img src="{p["thumbnail"]}" style="width:80px;height:60px;object-fit:cover;border-radius:4px;margin-right:12px;flex-shrink:0;" />'
                title = p.get("title") or p.get("content", "")[:80]
                content_url = p.get("content", "")
                summary = p.get("summary", "")
                bullets_html = ""
                if summary:
                    for bullet in summary.split("\n"):
                        b = bullet.strip().lstrip("-").lstrip("•").strip()
                        if b:
                            bullets_html += f'<li style="margin:2px 0;color:#555;font-size:14px;">{b}</li>'
                    if bullets_html:
                        bullets_html = f'<ul style="margin:6px 0 0 0;padding-left:18px;">{bullets_html}</ul>'
                caption_html = ""
                if p.get("caption"):
                    caption_html = f'<p style="margin:4px 0 0 0;color:#666;font-size:13px;font-style:italic;">{p["caption"]}</p>'
                items_html += f'''
                <div style="display:flex;align-items:flex-start;margin-bottom:16px;">
                    {thumbnail_html}
                    <div>
                        <a href="{content_url}" style="font-weight:bold;color:#2c2c2c;text-decoration:none;font-size:15px;">{title}</a>
                        {caption_html}
                        {bullets_html}
                    </div>
                </div>'''
            sender_sections += f'''
            <div style="margin-bottom:28px;">
                <h3 style="color:#c0614a;font-size:16px;margin:0 0 12px 0;border-bottom:1px solid #e8ddd6;padding-bottom:6px;">{sname}</h3>
                {items_html}
            </div>'''

        html_body = f'''
        <div style="background:#faf8f5;padding:40px 20px;font-family:Georgia,serif;">
            <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:8px;padding:36px;box-shadow:0 1px 4px rgba(0,0,0,0.06);">
                <h1 style="color:#2c2c2c;font-size:26px;margin:0 0 4px 0;">FaveFinds</h1>
                <p style="color:#888;font-size:15px;margin:0 0 28px 0;">Here's what your people shared {period_label}</p>
                {sender_sections}
                <hr style="border:none;border-top:1px solid #e8ddd6;margin:28px 0 16px 0;" />
                <p style="color:#aaa;font-size:12px;margin:0;">You're receiving this because someone added you to their FaveFinds.</p>
            </div>
        </div>'''

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "from": "FaveFinds <digest@favefinds.app>",
                    "to": [email],
                    "subject": subject,
                    "html": html_body
                },
                timeout=15
            )
            print(f"[Digest] Sent to {email} ({period}): status={resp.status_code}")
    except Exception as e:
        print(f"[Digest] Error for {phone_number}: {e}")


# --- Scheduled message processor ---

async def check_scheduled_messages():
    try:
        now = datetime.now(timezone.utc).isoformat()
        result = supabase.table("scheduled_messages").select("*").eq("sent", False).lte("send_at", now).execute()
        for msg in (result.data or []):
            await send_whatsapp_message(msg["phone_number"], msg["message"])
            supabase.table("scheduled_messages").update({"sent": True}).eq("id", msg["id"]).execute()
    except Exception as e:
        print(f"[Scheduler] Error: {e}")

# --- Routes ---

async def run_daily_digest():
    users = supabase.table("users").select("phone_number").eq("digest_daily", True).execute()
    for u in (users.data or []):
        await send_digest(u["phone_number"], "daily")

async def run_weekly_digest():
    users = supabase.table("users").select("phone_number").eq("digest_weekly", True).execute()
    for u in (users.data or []):
        await send_digest(u["phone_number"], "weekly")

PROMPTS = [
    "🎵 Heard anything good lately? Share it with your people.",
    "📖 Read anything worth passing along this week?",
    "🎙️ Any podcasts moving you lately? Your people would love to know.",
    "✨ What's been moving you this week? Share it here.",
]

async def run_prompts_scheduler():
    """Every 6 hours: nudge eligible users who haven't shared recently."""
    try:
        now = datetime.now(timezone.utc)
        # Skip Saturday 22:00 UTC through Sunday 14:00 UTC (digest window)
        weekday = now.weekday()  # Mon=0 ... Sun=6
        hour = now.hour
        in_digest_window = (weekday == 5 and hour >= 22) or (weekday == 6 and hour < 14)
        if in_digest_window:
            return

        cutoff_48h = (now - timedelta(hours=48)).isoformat()

        users = supabase.table("users").select("*").eq("onboarding_step", "complete").eq("prompts_enabled", True).execute()
        for u in (users.data or []):
            phone = u["phone_number"]

            # Check circle has at least 1 person
            circle = supabase.table("circle").select("id").eq("sender_phone", phone).limit(1).execute()
            if not circle.data:
                continue

            # Check no post in last 48 hours
            recent_post = supabase.table("posts").select("id").eq("phone_number", phone).gte("created_at", cutoff_48h).limit(1).execute()
            if recent_post.data:
                continue

            # Check no prompt sent in last 48 hours
            last_prompted = u.get("last_prompted_at")
            if last_prompted and last_prompted > cutoff_48h:
                continue

            # Send prompt at current index
            prompt_index = u.get("last_prompt_index") or 0
            message = PROMPTS[prompt_index % len(PROMPTS)]
            await send_whatsapp_message(phone, message)

            # Update last_prompted_at and advance index
            supabase.table("users").update({
                "last_prompted_at": now.isoformat(),
                "last_prompt_index": (prompt_index + 1) % len(PROMPTS)
            }).eq("phone_number", phone).execute()

    except Exception as e:
        print(f"[Prompts scheduler] Error: {e}")

async def run_midweek_nudge():
    """Every Wednesday at 15:00 UTC: nudge users who haven't shared since last Sunday."""
    try:
        users = supabase.table("users").select("phone_number").eq("onboarding_step", "complete").execute()
        now = datetime.now(timezone.utc)
        # weekday(): Mon=0 ... Sun=6; days since last Sunday = (weekday + 1) % 7
        days_since_sunday = (now.weekday() + 1) % 7
        last_sunday = (now - timedelta(days=days_since_sunday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        cutoff = last_sunday.isoformat()

        for u in (users.data or []):
            phone = u["phone_number"]
            posts = supabase.table("posts").select("id").eq("phone_number", phone).gte("created_at", cutoff).limit(1).execute()
            if not posts.data:
                await send_whatsapp_message(phone,
                    "👋 Midweek check-in — anything worth sharing with your people this week? "
                    "A song, article, or podcast you've been enjoying? Just send it here 🔗")
    except Exception as e:
        print(f"[Midweek nudge] Error: {e}")

@app.on_event("startup")
async def startup_event():
    scheduler.add_job(check_scheduled_messages, 'interval', minutes=15)
    scheduler.add_job(run_daily_digest, 'cron', hour=23, minute=0)
    scheduler.add_job(run_weekly_digest, 'cron', day_of_week='sun', hour=15, minute=0)
    scheduler.add_job(run_midweek_nudge, 'cron', day_of_week='wed', hour=15, minute=0)
    scheduler.add_job(run_prompts_scheduler, 'interval', hours=6)
    scheduler.start()

@app.on_event("shutdown")
async def shutdown_event():
    scheduler.shutdown()

@app.get("/")
async def home():
    return {"status": "FaveFinds Brain is Online"}

@app.get("/trigger-digest/{phone_number}")
async def trigger_digest(phone_number: str):
    await send_digest(phone_number, "weekly")
    return {"status": "sent", "phone_number": phone_number}

@app.get("/trigger-digest-all")
async def trigger_digest_all():
    users = supabase.table("users").select("phone_number").eq("digest_weekly", True).execute()
    phones = [u["phone_number"] for u in (users.data or [])]
    for phone in phones:
        await send_digest(phone, "weekly")
    return {"status": "sent", "count": len(phones)}

@app.get("/webhook")
async def verify(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == MY_TOKEN:
        return PlainTextResponse(content=challenge, status_code=200)
    return PlainTextResponse(content="Verification failed", status_code=403)

async def process_message(phone, message, message_id):
    """Handles complete users only (AI/post path runs in background)."""
    try:
        user = get_user(phone)
        if not user:
            return

        msg_type = message.get("type")

        if msg_type == "contacts":
            contacts = message.get("contacts", [])
            if contacts:
                sender_name = user.get("name", phone)
                added, skipped, at_limit, circle_count = await process_contacts_batch(phone, sender_name, contacts)
                if at_limit:
                    await send_whatsapp_message(phone,
                        "You've reached the 50 person limit. Remove someone to add a new person.")
                else:
                    summary = build_contacts_summary(added, skipped, onboarding=False)
                    await send_whatsapp_message(phone, summary)
                    if 40 <= circle_count <= 49:
                        spots_remaining = 50 - circle_count
                        await send_whatsapp_message(phone,
                            f"👥 Heads up — you've added {circle_count} people, {spots_remaining} spots remaining in your FaveFinds.")
            return

        text = message.get("text", {}).get("body", "").strip() if msg_type == "text" else ""

        if not text:
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
        phone = message["from"]
        msg_type = message.get("type")
        text = message.get("text", {}).get("body", "").strip() if msg_type == "text" else ""

        # Dedup
        if message_id in processed_messages:
            print(f"[Dedup] Skipping duplicate message {message_id}")
            return {"status": "ok"}
        processed_messages.add(message_id)
        if len(processed_messages) > 10000:
            processed_messages.clear()

        if msg_type not in ("text", "contacts"):
            return {"status": "ok"}

        if msg_type == "text" and not text:
            return {"status": "ok"}

        # New user: create row and send welcome synchronously
        user = get_user(phone)
        if not user:
            slug = generate_slug(phone[-6:])
            supabase.table("users").insert({
                "phone_number": phone,
                "feed_slug": slug,
                "onboarding_step": "awaiting_name"
            }).execute()
            await send_whatsapp_message(phone,
                "👋 Welcome to FaveFinds!\n\n"
                "Social media — too many people, too much noise.\n"
                "Messaging apps — too much thinking about who to send what.\n\n"
                "FaveFinds — simply share your favorite finds here. Music, podcasts, articles, anything that moves you. We drop them to your friends' feeds and into a beautiful weekly newsletter.\n\n"
                "What's your first name?")
            return {"status": "ok"}

        # Onboarding: handle synchronously
        step = user.get("onboarding_step", "complete")
        if step != "complete":
            await handle_onboarding(user, phone, message)
            return {"status": "ok"}

        # Complete users: background (AI for links is slow)
        background_tasks.add_task(process_message, phone, message, message_id)

    except Exception as e:
        print(f"Error: {e}")

    return {"status": "ok"}
