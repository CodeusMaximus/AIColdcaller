from datetime import timedelta
import os
import re
import json
import base64
import audioop
import asyncio
import httpx
from typing import Dict, Any, List, Optional
from datetime import datetime, time
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.responses import Response, PlainTextResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import websockets

load_dotenv()

# --------------------------
# ENV
# --------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
TWILIO_PHONE = os.getenv("TWILIO_PHONE_NUMBER", "")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
NEXTJS_BASE_URL = os.getenv(
    "NEXTJS_BASE_URL", "http://localhost:3000")  # ✅ ADD THIS

OPENAI_MODEL = os.getenv("OPENAI_REALTIME_MODEL",
                         "gpt-4o-realtime-preview-2024-12-17")
OPENAI_VOICE = os.getenv("OPENAI_VOICE", "alloy")

# MongoDB configuration - IMPORTANT: Must match your Next.js database!
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "CRM")
MONGODB_COLLECTION = os.getenv("MONGODB_COLLECTION", "high_value_leads")

# Campaign settings
CAMPAIGN_START_HOUR = int(os.getenv("CAMPAIGN_START_HOUR", "9"))
CAMPAIGN_END_HOUR = int(os.getenv("CAMPAIGN_END_HOUR", "17"))
SECONDS_BETWEEN_CALLS = int(os.getenv("SECONDS_BETWEEN_CALLS", "300"))

print("\n" + "="*60)
print("🚀 JENNIFER AI - CONFIGURATION")
print("="*60)
print(f"OpenAI API Key: {'✅ Present' if OPENAI_API_KEY else '❌ Missing'}")
print(
    f"Twilio Account SID: {'✅ Present' if TWILIO_ACCOUNT_SID else '❌ Missing'}")
print(
    f"Twilio Auth Token: {'✅ Present' if TWILIO_AUTH_TOKEN else '❌ Missing'}")
print(f"Twilio Phone: {TWILIO_PHONE or '❌ Missing'}")
print(f"Public URL: {PUBLIC_BASE_URL or '❌ Missing'}")
print(f"Next.js URL (API): {NEXTJS_BASE_URL or '❌ Missing'}")  # ✅ ADD THIS

print(f"MongoDB URI: {MONGODB_URI}")
print(f"MongoDB Database: {MONGODB_DATABASE}")
print(f"MongoDB Collection: {MONGODB_COLLECTION}")
print(f"Campaign Hours: {CAMPAIGN_START_HOUR}am - {CAMPAIGN_END_HOUR}pm")
print("="*60 + "\n")

# Initialize clients
twilio_client = Client(
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN else None
mongo_client = AsyncIOMotorClient(MONGODB_URI)
db = mongo_client[MONGODB_DATABASE]
leads_collection = db[MONGODB_COLLECTION]

# --------------------------
# FastAPI app
# --------------------------
app = FastAPI(title="Jennifer AI Calling System")

# CORS - Allow Next.js to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:3002",
        # Add your production Next.js URL here when deployed
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------
# Lead Model
# --------------------------


class Lead:
    """Lead model for scraped leads from Next.js"""

    def __init__(self, doc: dict):
        # Use 'id' or '_id' depending on how Next.js stores it
        self._id = str(doc.get("_id", doc.get("id", "")))
        self.id = str(doc.get("id", doc.get("_id", "")))

        # Basic info from scraper
        self.name = doc.get("business_name", doc.get("name", ""))
        self.business_name = doc.get("business_name", "")
        self.phone = doc.get("phone", "")
        self.email = doc.get("email", "")
        self.address = doc.get("address", "")
        self.keyword = doc.get("keyword", "")
        self.zip_code = doc.get("zip_code", "")

        # Scraper flags
        self.has_phone = doc.get("has_phone", False)
        self.has_email = doc.get("has_email", False)
        self.no_website = doc.get("no_website", False)
        self.high_value_lead = doc.get("high_value_lead", False)

        # Call tracking fields (may not exist yet)
        self.contacted = doc.get("contacted", False)
        self.answered = doc.get("answered", False)
        self.interested = doc.get("interested", False)
        self.meeting_booked = doc.get("meeting_booked", False)
        self.call_sid = doc.get("call_sid", None)
        self.call_duration = doc.get("call_duration", 0)
        self.call_date = doc.get("call_date", None)
        self.notes = doc.get("notes", "")

        # Source tracking
        self.source = doc.get("source", "nextjs_scraper")
        self.created_at = doc.get("created_at", doc.get("scraped_at", None))
        self.updated_at = doc.get("updated_at", None)

    def to_dict(self) -> dict:
        """Convert to dictionary for MongoDB update"""
        return {
            "business_name": self.business_name,
            "name": self.name,
            "phone": self.phone,
            "email": self.email,
            "address": self.address,
            "keyword": self.keyword,
            "zip_code": self.zip_code,
            "contacted": self.contacted,
            "answered": self.answered,
            "interested": self.interested,
            "meeting_booked": self.meeting_booked,
            "call_sid": self.call_sid,
            "call_duration": self.call_duration,
            "call_date": self.call_date,
            "notes": self.notes,
            "source": self.source,
            "updated_at": datetime.utcnow()
        }

# --------------------------
# Call State Management
# --------------------------


class CallState:
    def __init__(self, lead: Lead):
        self.lead = lead
        self.email_captured: str | None = None
        self.preferred_datetime: dict | None = None  # ✅ ADD THIS
        self.meeting_time: str | None = None
        self.link_sent: bool = False


CALLS: Dict[str, CallState] = {}

# Campaign stats
CAMPAIGN_STATS = {
    "total_calls": 0,
    "answered": 0,
    "interested": 0,
    "meetings_booked": 0,
    "start_time": None,
    "end_time": None,
    "is_running": False
}


def maybe_extract_datetime(text: str) -> dict | None:
    """
    Extract day and time from natural language.
    Returns: {"day": "monday", "time": "14:00"} or None
    """
    if not text:
        return None

    text_lower = text.lower()

    # Days of week
    days = {
        "monday": 0, "mon": 0,
        "tuesday": 1, "tues": 1, "tue": 1,
        "wednesday": 2, "wed": 2,
        "thursday": 3, "thurs": 3, "thu": 3,
        "friday": 4, "fri": 4,
        "saturday": 5, "sat": 5,
        "sunday": 6, "sun": 6,
        "tomorrow": -1,  # Special case
        "today": -2,  # Special case
    }

    # Find day
    found_day = None
    day_offset = None

    for day_name, offset in days.items():
        if day_name in text_lower:
            found_day = day_name
            day_offset = offset
            break

    # Find time - look for patterns like "2 PM", "10:30 AM", "3pm", "fourteen hundred"
    time_patterns = [
        r'(\d{1,2}):(\d{2})\s*(am|pm)',  # 2:30 PM
        r'(\d{1,2})\s*(am|pm)',          # 2 PM
        r'(\d{1,2}):(\d{2})',            # 14:30 (24-hour)
    ]

    found_time = None
    for pattern in time_patterns:
        match = re.search(pattern, text_lower)
        if match:
            groups = match.groups()

            if len(groups) == 2:  # Just hour and AM/PM
                hour = int(groups[0])
                ampm = groups[1]

                if ampm == "pm" and hour != 12:
                    hour += 12
                elif ampm == "am" and hour == 12:
                    hour = 0

                found_time = f"{hour:02d}:00"

            elif len(groups) == 3:  # Hour, minute, AM/PM
                hour = int(groups[0])
                minute = int(groups[1])
                ampm = groups[2]

                if ampm == "pm" and hour != 12:
                    hour += 12
                elif ampm == "am" and hour == 12:
                    hour = 0

                found_time = f"{hour:02d}:{minute:02d}"

            break

    if found_day and found_time:
        return {
            "day": found_day,
            "day_offset": day_offset,
            "time": found_time,
            "original": text
        }

    return None


# --------------------------
# Helper Functions
# --------------------------
EMAIL_REGEX = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def normalize_spoken_email(text: str) -> str:
    if not text:
        return ""
    normalized = re.sub(r'\s+at\s+', '@', text, flags=re.IGNORECASE)
    normalized = re.sub(r'\s+dot\s+', '.', normalized, flags=re.IGNORECASE)
    return normalized


def maybe_extract_email(text: str) -> str | None:
    normalized = normalize_spoken_email(text or "")
    m = EMAIL_REGEX.search(normalized)
    return m.group(0) if m else None


def normalize_phone_number(phone: str) -> str:
    """Normalize phone number to E.164 format"""
    if not phone:
        return ""

    # Remove all non-digit characters
    digits = re.sub(r'\D', '', phone)

    # If already has country code
    if phone.startswith('+'):
        return phone

    # If 11 digits and starts with 1 (US)
    if len(digits) == 11 and digits.startswith('1'):
        return f"+{digits}"

    # If 10 digits (assume US)
    if len(digits) == 10:
        return f"+1{digits}"

    # Otherwise return as-is with +
    return f"+{digits}"


def ws_url_from_public_base(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):] + "/stream"  # ✅ Changed
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):] + "/stream"    # ✅ Changed
    return "wss://" + url + "/stream"                        # ✅ Changed


def is_calling_hours() -> bool:
    now = datetime.now().time()
    start = time(CAMPAIGN_START_HOUR, 0)
    end = time(CAMPAIGN_END_HOUR, 0)
    return start <= now <= end

# --------------------------
# Auto-Booking Function
# --------------------------


async def auto_book_meeting_after_call(lead_id: str, lead_name: str, email: str, phone: str, preferred_datetime: dict | None = None):
    """Automatically book a meeting after AI call collects email and interest"""
    from datetime import timedelta

    if preferred_datetime:
        day_offset = preferred_datetime.get("day_offset")
        time_str = preferred_datetime.get("time", "14:00")
        hour, minute = map(int, time_str.split(":"))

        if day_offset == -1:
            target_date = datetime.utcnow() + timedelta(days=1)
        elif day_offset == -2:
            target_date = datetime.utcnow()
        elif day_offset is not None:
            current_day = datetime.utcnow().weekday()
            days_ahead = day_offset - current_day
            if days_ahead <= 0:
                days_ahead += 7
            target_date = datetime.utcnow() + timedelta(days=days_ahead)
        else:
            target_date = datetime.utcnow() + timedelta(days=1)
            hour, minute = 14, 0

        target_date = target_date.replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        print(
            f"   📅 Using preferred time: {preferred_datetime['day']} at {preferred_datetime['time']}")
    else:
        target_date = datetime.utcnow() + timedelta(days=1)
        target_date = target_date.replace(
            hour=19, minute=0, second=0, microsecond=0)
        print(f"   📅 Using default time: Tomorrow at 2 PM EST")

    start_iso = target_date.isoformat() + "Z"
    end_iso = (target_date + timedelta(minutes=30)).isoformat() + "Z"

    try:
        print(f"📅 Auto-booking meeting for {lead_name}...")
        print(f"   📧 Email: {email}")
        print(f"   📞 Phone: {phone}")
        print(f"   🕐 Time: {start_iso}")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{NEXTJS_BASE_URL}/api/calendar/book",
                json={
                    "leadId": lead_id,
                    "leadName": lead_name,
                    "attendeeEmail": email,
                    "attendeePhone": phone,
                    "startISO": start_iso,
                    "endISO": end_iso,
                    "service": "Discovery Call"
                },
                timeout=30.0
            )

            print(f"   📬 API Response Status: {response.status_code}")

            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    print(f"   ✅ Meeting auto-booked!")
                    print(f"   🎥 Meet Link: {data.get('meetLink')}")
                    return True

            try:
                error_data = response.json()
                print(f"   ❌ Booking failed: {error_data}")
            except:
                print(f"   ❌ Response: {response.text}")
            return False

    except Exception as e:
        print(f"   ❌ Error: {e}")
        return False

    except Exception as e:
        print(f"   ❌ Error: {e}")
        return False

    except Exception as e:
        print(f"   ❌ Error auto-booking: {e}")
        import traceback
        traceback.print_exc()
        return False

# --------------------------
# MongoDB Operations
# --------------------------


async def get_or_create_lead(lead_data: dict) -> Lead:
    """Get existing lead or create new one from Next.js data"""

    # Try to find by phone number first
    phone = normalize_phone_number(lead_data.get('phone', ''))

    if phone:
        existing = await leads_collection.find_one({"phone": phone})
        if existing:
            print(
                f"   📋 Found existing lead: {existing.get('business_name', 'Unknown')}")
            return Lead(existing)

    # Try to find by id
    lead_id = lead_data.get('id')
    if lead_id:
        # Try as ObjectId first
        try:
            existing = await leads_collection.find_one({"_id": ObjectId(lead_id)})
            if existing:
                print(f"   📋 Found existing lead by _id")
                return Lead(existing)
        except:
            pass

        # Try as string id
        existing = await leads_collection.find_one({"id": lead_id})
        if existing:
            print(f"   📋 Found existing lead by id")
            return Lead(existing)

    # Create new lead document
    print(f"   ✨ Creating new lead in MongoDB")
    new_lead = {
        "id": lead_data.get('id', ''),
        "business_name": lead_data.get('name', lead_data.get('business', '')),
        "name": lead_data.get('name', ''),
        "phone": phone,
        "email": lead_data.get('email', ''),
        "address": lead_data.get('address', ''),
        "keyword": lead_data.get('keyword', ''),
        "zip_code": lead_data.get('zip_code', ''),
        "has_phone": bool(phone),
        "has_email": bool(lead_data.get('email')),
        "contacted": False,
        "answered": False,
        "interested": False,
        "meeting_booked": False,
        "source": "nextjs_api",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }

    result = await leads_collection.insert_one(new_lead)
    new_lead["_id"] = result.inserted_id

    return Lead(new_lead)


async def update_lead_in_db(lead: Lead):
    """Update lead in MongoDB"""

    # Try to update by _id (ObjectId)
    try:
        if lead._id:
            result = await leads_collection.update_one(
                {"_id": ObjectId(lead._id)},
                {"$set": lead.to_dict()}
            )
            if result.modified_count > 0:
                return
    except:
        pass

    # Try to update by phone
    if lead.phone:
        await leads_collection.update_one(
            {"phone": lead.phone},
            {"$set": lead.to_dict()},
            upsert=True
        )


async def get_lead_by_call_sid(call_sid: str) -> Optional[Lead]:
    """Find lead by Twilio call SID"""
    doc = await leads_collection.find_one({"call_sid": call_sid})
    return Lead(doc) if doc else None

# --------------------------
# Outbound Calling
# --------------------------


async def make_outbound_call(lead: Lead) -> bool:
    """Initiate outbound call to a lead (always call, ignore hours)"""

    # Make sure we have a phone number
    if not lead.phone:
        print(f"❌ No phone number for {lead.name}")
        return False

    # If Twilio is NOT configured, just simulate success (dev mode)
    if not twilio_client:
        print("⚠️ Twilio client not initialized – simulating successful call for dev")
        lead.contacted = True
        lead.call_sid = "SIMULATED_SID"
        lead.call_date = datetime.utcnow()
        await update_lead_in_db(lead)
        CAMPAIGN_STATS["total_calls"] += 1
        return True

    # If Twilio IS configured, always try to call (ignore business hours)
    try:
        print(f"📞 Calling {lead.name} ({lead.business_name}) at {lead.phone}")

        call = twilio_client.calls.create(
            to=lead.phone,
            from_=TWILIO_PHONE,
            url=f"{PUBLIC_BASE_URL}/twilio-webhook?lead_id={lead._id or lead.id}",
            status_callback=f"{PUBLIC_BASE_URL}/call-status",
            status_callback_event=['initiated',
                                   'ringing', 'answered', 'completed'],
            machine_detection="DetectMessageEnd",
            timeout=30
        )

        lead.contacted = True
        lead.call_sid = call.sid
        lead.call_date = datetime.utcnow()
        await update_lead_in_db(lead)

        CAMPAIGN_STATS["total_calls"] += 1

        print(f"   ✅ Call initiated: {call.sid}")
        return True

    except Exception as e:
        print(f"   ❌ Failed to call {lead.name}: {e}")
        lead.notes = f"Call failed: {str(e)}"
        await update_lead_in_db(lead)
        return False


# --------------------------
# Campaign Manager
# --------------------------
async def run_calling_campaign(query: dict, max_calls: int = None):
    """Run calling campaign with MongoDB query"""

    if CAMPAIGN_STATS["is_running"]:
        print("⚠️  Campaign already running")
        return

    CAMPAIGN_STATS["is_running"] = True
    CAMPAIGN_STATS["start_time"] = datetime.utcnow()

    print("\n" + "="*60)
    print("🚀 STARTING CALLING CAMPAIGN")
    print("="*60)
    print(f"Query: {query}")
    print(f"Calling hours: {CAMPAIGN_START_HOUR}am - {CAMPAIGN_END_HOUR}pm")
    print(f"Delay between calls: {SECONDS_BETWEEN_CALLS} seconds")
    print("="*60 + "\n")

    # Get leads from MongoDB
    cursor = leads_collection.find(query)
    if max_calls:
        cursor = cursor.limit(max_calls)

    leads = []
    async for doc in cursor:
        leads.append(Lead(doc))

    print(f"📋 Found {len(leads)} leads to call\n")

    if len(leads) == 0:
        print("⚠️  No leads found matching criteria")
        CAMPAIGN_STATS["is_running"] = False
        return

    for i, lead in enumerate(leads, 1):
        # Check if still within calling hours
        if not is_calling_hours():
            print("\n⏰ Outside calling hours - pausing campaign")
            break

        # Make the call
        print(f"\n[{i}/{len(leads)}]")
        success = await make_outbound_call(lead)

        # Wait between calls (unless it's the last one)
        if i < len(leads):
            print(
                f"   ⏳ Waiting {SECONDS_BETWEEN_CALLS} seconds before next call...")
            await asyncio.sleep(SECONDS_BETWEEN_CALLS)

    CAMPAIGN_STATS["is_running"] = False
    CAMPAIGN_STATS["end_time"] = datetime.utcnow()

    # Print final stats
    duration = CAMPAIGN_STATS["end_time"] - CAMPAIGN_STATS["start_time"]

    print("\n" + "="*60)
    print("📊 CAMPAIGN COMPLETED")
    print("="*60)
    print(f"Total calls made: {CAMPAIGN_STATS['total_calls']}")
    print(f"Calls answered: {CAMPAIGN_STATS['answered']}")
    print(f"Interested leads: {CAMPAIGN_STATS['interested']}")
    print(f"Meetings booked: {CAMPAIGN_STATS['meetings_booked']}")
    print(f"Duration: {duration}")
    print("="*60 + "\n")

# --------------------------
# API Endpoints for Next.js

# --------------------------


@app.post("/test-auto-booking")
async def test_auto_booking():
    """Test endpoint to verify auto-booking works"""
    print("\n🧪 TESTING AUTO-BOOKING FUNCTION...\n")

    success = await auto_book_meeting_after_call(
        lead_id="test123",
        lead_name="Test Business",
        email="dfsturge@gmail.com",
        phone="555-1234"
    )

    return {
        "success": success,
        "message": "Check Python terminal for detailed logs and your email inbox"
    }


@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "message": "Jennifer AI Calling System",
        "status": "running",
        "version": "1.0",
        "mongodb_connected": mongo_client is not None,
        "twilio_configured": twilio_client is not None
    }


@app.post("/api/call-lead")
async def call_single_lead_endpoint(request: Request):
    """
    Call a single lead from Next.js CRM

    Body: {
        id: string,
        name: string,
        phone: string,
        business: string,
        email?: string,
        keyword?: string,
        zip_code?: string,
        address?: string
    }
    """
    try:
        data = await request.json()

        print("\n" + "="*60)
        print("📞 SINGLE LEAD CALL REQUEST")
        print("="*60)
        print(f"Lead: {data.get('name', 'Unknown')}")
        print(f"Phone: {data.get('phone', 'None')}")
        print("="*60 + "\n")

        # Get or create lead
        lead = await get_or_create_lead(data)

        # Normalize phone
        lead.phone = normalize_phone_number(lead.phone)

        if not lead.phone:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "No phone number provided"}
            )

        # Make the call
        success = await make_outbound_call(lead)

        if success:
            return JSONResponse(content={
                "success": True,
                "message": f"Calling {lead.name}",
                "call_sid": lead.call_sid
            })
        else:
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "Failed to initiate call"}
            )

    except Exception as e:
        print(f"❌ Error in call_single_lead_endpoint: {e}")
        import traceback
        traceback.print_exc()

        return JSONResponse(
            status_code=400,
            content={"success": False, "error": str(e)}
        )


@app.post("/api/campaign/start")
async def start_campaign_endpoint(request: Request, background_tasks: BackgroundTasks):
    """
    Start calling campaign from Next.js

    Body: {
        filters: {
            high_value_lead?: boolean,
            has_phone?: boolean,
            no_website?: boolean,
            keyword?: string,
            zip_code?: string
        },
        max_calls?: number
    }
    """
    try:
        data = await request.json()
        filters = data.get('filters', {})
        max_calls = data.get('max_calls', None)

        print("\n" + "="*60)
        print("🚀 CAMPAIGN START REQUEST")
        print("="*60)
        print(f"Filters: {filters}")
        print(f"Max calls: {max_calls or 'Unlimited'}")
        print("="*60 + "\n")

        # Build MongoDB query
        query = {}

        # Only call leads not already contacted
        query['$or'] = [
            {'contacted': {'$exists': False}},
            {'contacted': False}
        ]

        # Apply filters
        if filters.get('high_value_lead'):
            query['high_value_lead'] = True

        if filters.get('has_phone'):
            query['has_phone'] = True
            query['phone'] = {'$ne': '', '$exists': True}

        if filters.get('no_website'):
            query['no_website'] = True

        if filters.get('keyword'):
            query['keyword'] = filters['keyword']

        if filters.get('zip_code'):
            query['zip_code'] = filters['zip_code']

        # Count matching leads
        total_leads = await leads_collection.count_documents(query)

        if total_leads == 0:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": "No leads match the specified filters",
                    "total_leads": 0
                }
            )

        # Start campaign in background
        background_tasks.add_task(run_calling_campaign, query, max_calls)

        return JSONResponse(content={
            "success": True,
            "status": "Campaign started",
            "total_leads": total_leads,
            "calling_hours": f"{CAMPAIGN_START_HOUR}am-{CAMPAIGN_END_HOUR}pm"
        })

    except Exception as e:
        print(f"❌ Error in start_campaign_endpoint: {e}")
        import traceback
        traceback.print_exc()

        return JSONResponse(
            status_code=400,
            content={"success": False, "error": str(e)}
        )


@app.get("/api/campaign/status")
async def campaign_status():
    """Get current campaign status"""

    # Get real-time stats from MongoDB
    total_leads = await leads_collection.count_documents({})
    contacted = await leads_collection.count_documents({"contacted": True})
    answered = await leads_collection.count_documents({"answered": True})
    interested = await leads_collection.count_documents({"interested": True})
    meetings_booked = await leads_collection.count_documents({"meeting_booked": True})

    return {
        "is_running": CAMPAIGN_STATS["is_running"],
        "start_time": CAMPAIGN_STATS["start_time"].isoformat() if CAMPAIGN_STATS["start_time"] else None,
        "stats": {
            "total_leads": total_leads,
            "contacted": contacted,
            "answered": answered,
            "interested": interested,
            "meetings_booked": meetings_booked,
            "pending": total_leads - contacted
        },
        "campaign": {
            "total_calls": CAMPAIGN_STATS["total_calls"],
            "answered": CAMPAIGN_STATS["answered"],
            "interested": CAMPAIGN_STATS["interested"],
            "meetings_booked": CAMPAIGN_STATS["meetings_booked"]
        }
    }


@app.post("/api/campaign/stop")
async def stop_campaign():
    """Stop the campaign"""
    CAMPAIGN_STATS["is_running"] = False
    CAMPAIGN_STATS["end_time"] = datetime.utcnow()

    return {
        "success": True,
        "status": "Campaign stopped"
    }

# --------------------------
# Twilio Webhooks
# --------------------------


@app.post("/twilio-webhook")
async def twilio_webhook(request: Request):
    """Handle call connection"""

    params = dict(request.query_params)
    lead_id = params.get("lead_id")

    print(f"🔥 Call connected (lead_id: {lead_id})")

    media_ws_url = ws_url_from_public_base(PUBLIC_BASE_URL)

    vr = VoiceResponse()
    connect = Connect()
    connect.stream(url=media_ws_url)
    vr.append(connect)
    return Response(str(vr), media_type="text/xml")


@app.post("/call-status")
async def call_status(request: Request):
    """Track call status updates"""

    form_data = await request.form()
    call_sid = form_data.get("CallSid")
    call_status = form_data.get("CallStatus")
    call_duration = form_data.get("CallDuration", "0")
    answered_by = form_data.get("AnsweredBy", "")

    print(f"📊 Call {call_sid}: {call_status} (answered by: {answered_by})")

    # Update lead in MongoDB
    update_data = {
        "updated_at": datetime.utcnow()
    }

    if call_status == "completed":
        update_data["call_duration"] = int(call_duration)

        if answered_by == "human":
            update_data["answered"] = True
            CAMPAIGN_STATS["answered"] += 1
        elif answered_by in ["machine_start", "machine_end_beep", "machine_end_silence"]:
            update_data["notes"] = "Voicemail detected"

    await leads_collection.update_one(
        {"call_sid": call_sid},
        {"$set": update_data}
    )

    return Response("OK", media_type="text/plain")

# --------------------------
# WebSocket Media Handler
# --------------------------


@app.websocket("/stream")
async def media_socket(twilio_ws: WebSocket):
    await twilio_ws.accept()
    print("✅ Twilio WS connected to /stream")
    stream_sid = None
    session_ready = False
    current_lead = None

    oai_url = f"wss://api.openai.com/v1/realtime?model={OPENAI_MODEL}"
    oai_headers = [
        ("Authorization", f"Bearer {OPENAI_API_KEY}"),
        ("OpenAI-Beta", "realtime=v1")
    ]

    try:
        async with websockets.connect(oai_url, additional_headers=oai_headers) as oai_ws:
            print("🔗 Connected to OpenAI Realtime")

            async def twilio_to_openai():
                nonlocal stream_sid, session_ready, current_lead
                chunks_since_commit = 0
                greeted = False

                while True:
                    try:
                        msg = await twilio_ws.receive_text()
                        data = json.loads(msg)
                        evt = data.get("event")

                        if evt == "start":
                            stream_sid = data["start"]["streamSid"]
                            call_sid = data["start"].get("callSid")

                            custom_params = data["start"].get(
                                "customParameters", {})
                            lead_id = custom_params.get("leadId", "")
                            business_name = custom_params.get(
                                "businessName", "Unknown Business")
                            contact_name = custom_params.get(
                                "contactName", "there")

                            print(f"🎧 Stream started: {stream_sid}")
                            print(f"📋 Lead ID: {lead_id}")
                            print(f"🏢 Business: {business_name}")
                            print(f"👤 Contact: {contact_name}")

                            # Try to find the lead
                            current_lead = await get_lead_by_call_sid(call_sid)

                            # ✅ FIX: If we can't find by call_sid, try by lead_id
                            if not current_lead and lead_id:
                                try:
                                    from bson import ObjectId
                                    doc = await leads_collection.find_one({"_id": ObjectId(lead_id)})
                                    if doc:
                                        current_lead = Lead(doc)
                                        print(
                                            f"✅ Found lead by _id: {lead_id}")
                                    else:
                                        doc = await leads_collection.find_one({"id": lead_id})
                                        if doc:
                                            current_lead = Lead(doc)
                                            print(
                                                f"✅ Found lead by id: {lead_id}")
                                except Exception as e:
                                    print(f"⚠️ Could not find lead: {e}")

                            # ✅ FIX: Create CallState even if lead is not found
                            if current_lead:
                                print(
                                    f"✅ Creating CallState for {current_lead.business_name}")
                                CALLS[stream_sid] = CallState(current_lead)
                            else:
                                print(
                                    f"⚠️ Lead not found, creating minimal lead object")
                                # Create a minimal lead object
                                minimal_lead = Lead({
                                    "_id": lead_id or "unknown",
                                    "id": lead_id or "unknown",
                                    "business_name": business_name,
                                    "name": business_name,
                                    "phone": "",
                                    "call_sid": call_sid,
                                    "has_phone": False,
                                    "has_email": False,
                                    "contacted": True
                                })
                                CALLS[stream_sid] = CallState(minimal_lead)
                                print(f"✅ Created CallState with minimal lead data")

                            SYSTEM_PROMPT = f"""
You are Jennifer, the AI assistant for MediaDari, New York's hottest digital media and web agency.

You are speaking with {contact_name} who runs {business_name}.

Speak in very short, natural sentences (one sentence at a time).

Start by introducing yourself and confirming you're speaking with {contact_name}.

Mention you noticed {business_name} is listed online but may not have a proper website.

Explain that means lost trust and leads.

Offer our mobile-ready website designed to generate calls and leads.

If they're interested:
1. Ask for their email address
2. Repeat the email back to confirm it's correct
3. Ask: "What day and time works best for you this week? For example, Tuesday at 2 PM?"
4. Wait for them to suggest a specific day and time
5. Repeat it back: "Got it, so [DAY] at [TIME]. Perfect!"
6. Say: "I'm sending you a calendar invite right now with a Google Meet link. Check your email!"

Try to get them to say a specific day (like Monday, Tuesday, etc.) and a specific time (like 2 PM, 10 AM, etc.).

IMPORTANT: After asking a question, STOP and wait. Don't stack multiple questions.

Always speak in English. Be friendly and conversational.
"""

                            await oai_ws.send(json.dumps({
                                "type": "session.update",
                                "session": {
                                    "voice": OPENAI_VOICE,
                                    "instructions": SYSTEM_PROMPT,
                                    "modalities": ["text", "audio"],
                                    "input_audio_format": "pcm16",
                                    "output_audio_format": "pcm16",
                                    "input_audio_transcription": {"model": "whisper-1"},
                                    "turn_detection": {
                                        "type": "server_vad",
                                        "threshold": 0.5,
                                        "prefix_padding_ms": 300,
                                        "silence_duration_ms": 1500
                                    }
                                }
                            }))
                            await asyncio.sleep(0.2)
                            session_ready = True

                            if not greeted:
                                greeted = True
                                await oai_ws.send(json.dumps({
                                    "type": "conversation.item.create",
                                    "item": {
                                        "type": "message",
                                        "role": "user",
                                        "content": [{"type": "input_text", "text": "Hello"}]
                                    }
                                }))
                                await oai_ws.send(json.dumps({"type": "response.create"}))

                        elif evt == "media":
                            if not session_ready:
                                continue
                            payload_b64 = data["media"]["payload"]
                            ulaw8k = base64.b64decode(payload_b64)

                            pcm16_8k = audioop.ulaw2lin(ulaw8k, 2)
                            pcm16_24k, _ = audioop.ratecv(
                                pcm16_8k, 2, 1, 8000, 24000, None)

                            await oai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(pcm16_24k).decode("ascii")
                            }))
                            chunks_since_commit += 1

                            if chunks_since_commit >= 50:
                                chunks_since_commit = 0

                        elif evt == "stop":
                            print("🛑 Stream stopped")
                            break

                    except Exception as e:
                        print(f"Error in twilio_to_openai: {e}")
                        break

            async def openai_to_twilio():
                nonlocal session_ready, stream_sid, current_lead

                while True:
                    try:
                        raw = await oai_ws.recv()
                        try:
                            ev = json.loads(raw)
                        except Exception:
                            continue

                        et = ev.get("type")

                        if et in ("session.created", "session.updated"):
                            session_ready = True

                        if et == "error":
                            code = ev.get("error", {}).get("code", "")
                            if code != "input_audio_buffer_commit_empty":
                                print("❌ OpenAI error:", ev)
                            continue

                        if et == "response.audio.delta" and "delta" in ev:
                            if not stream_sid:
                                continue
                            pcm16_24k = base64.b64decode(ev["delta"])
                            pcm16_8k, _ = audioop.ratecv(
                                pcm16_24k, 2, 1, 24000, 8000, None)
                            ulaw8k = audioop.lin2ulaw(pcm16_8k, 2)

                            out = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": base64.b64encode(ulaw8k).decode("ascii")}
                            }
                            await twilio_ws.send_text(json.dumps(out))

                        if et == "response.audio_transcript.delta":
                            print(
                                f"🤖 Jennifer: {ev.get('delta','')}", end="", flush=True)
                        elif et == "response.audio_transcript.done":
                            print()

                        if et == "conversation.item.input_audio_transcription.completed":
                            transcript = ev.get("transcript", "") or ""
                            print(
                                f"👤 {current_lead.name if current_lead else 'Caller'}: {transcript}")

                            print(f"🔍 DEBUG: stream_sid = {stream_sid}")
                            print(
                                f"🔍 DEBUG: stream_sid in CALLS? {stream_sid in CALLS if stream_sid else 'No stream_sid'}")

                            if stream_sid and stream_sid in CALLS:
                                state = CALLS[stream_sid]
                                print(
                                    f"🔍 DEBUG: Found CallState for stream {stream_sid}")
                                print(
                                    f"🔍 DEBUG: state.email_captured = {state.email_captured}")
                                print(
                                    f"🔍 DEBUG: state.lead.interested = {state.lead.interested}")
                                print(
                                    f"🔍 DEBUG: state.link_sent = {state.link_sent}")

                                # Extract email
                                email_guess = maybe_extract_email(transcript)
                                print(
                                    f"🔍 DEBUG: email_guess from transcript = {email_guess}")

                                if email_guess:
                                    if state.email_captured and state.email_captured != email_guess:
                                        print(
                                            f"🔄 EMAIL UPDATED: {state.email_captured} → {email_guess}")
                                    elif not state.email_captured:
                                        print(
                                            f"✅ EMAIL DETECTED: {email_guess}")
                                    else:
                                        print(
                                            f"ℹ️ Same email repeated: {email_guess}")

                                    state.email_captured = email_guess
                                    state.lead.email = email_guess

                                    await leads_collection.update_one(
                                        {"call_sid": state.lead.call_sid},
                                        {"$set": {
                                            "collectedEmail": email_guess,
                                            "email": email_guess,
                                            "leadScore": "warm",
                                            "updated_at": datetime.utcnow()
                                        }}
                                    )

                                    print(
                                        f"   📧 Email saved to DB: {email_guess}")
                                else:
                                    print(f"🔍 No email detected in transcript")
                                 # ✅ ADD THIS: Extract preferred date/time
                                    datetime_guess = maybe_extract_datetime(
                                        transcript)
                                    print(
                                        f"🔍 DEBUG: datetime_guess from transcript = {datetime_guess}")

                                    if datetime_guess and not state.preferred_datetime:
                                        print(
                                            f"✅ DATETIME DETECTED: {datetime_guess}")
                                        state.preferred_datetime = datetime_guess
                                        print(
                                            f"   📅 Preferred: {datetime_guess['day']} at {datetime_guess['time']}")
                                # Check for interest keywords
                                interest_keywords = [
                                    "yes", "interested", "sure", "sounds good", "okay", "perfect", "great", "absolutely", "correct"]
                                print(
                                    f"🔍 DEBUG: Checking transcript for interest keywords...")
                                print(
                                    f"🔍 DEBUG: Transcript lowercase: '{transcript.lower()}'")

                                matched_keywords = [
                                    word for word in interest_keywords if word in transcript.lower()]
                                print(
                                    f"🔍 DEBUG: Matched keywords: {matched_keywords}")

                                if any(word in transcript.lower() for word in interest_keywords):
                                    print(
                                        f"✅ INTEREST KEYWORD DETECTED: {matched_keywords}")

                                    if not state.lead.interested:
                                        print(f"🔥 Marking lead as interested...")
                                        state.lead.interested = True
                                        CAMPAIGN_STATS["interested"] += 1

                                        await leads_collection.update_one(
                                            {"call_sid": state.lead.call_sid},
                                            {"$set": {
                                                "interested": True,
                                                "leadScore": "hot",
                                                "updated_at": datetime.utcnow()
                                            }}
                                        )

                                        print(
                                            f"   🔥 Lead marked as interested in DB!")
                                    else:
                                        print(f"ℹ️ Lead already interested")
                                else:
                                    print(f"🔍 No interest keywords detected")

                                # ✅ CHECK AUTO-BOOKING AFTER EVERY MESSAGE (not just when interest detected)
                                print(f"\n{'='*60}")
                                print(f"🎯 CHECKING AUTO-BOOKING CONDITIONS:")
                                print(
                                    f"   Email captured? {state.email_captured}")
                                print(
                                    f"   Lead interested? {state.lead.interested}")
                                print(
                                    f"   Link already sent? {state.link_sent}")
                                print(f"{'='*60}\n")

                                if state.email_captured and state.lead.interested and not state.link_sent:
                                    print(
                                        f"✅ ALL CONDITIONS MET! Triggering auto-booking...")

                                    booking_success = await auto_book_meeting_after_call(
                                        lead_id=state.lead.id or state.lead._id,
                                        lead_name=state.lead.business_name,
                                        email=state.email_captured,
                                        phone=state.lead.phone,
                                        preferred_datetime=state.preferred_datetime  # ✅ ADD THIS
                                    )

                                    if booking_success:
                                        state.link_sent = True
                                        state.lead.meeting_booked = True
                                        CAMPAIGN_STATS["meetings_booked"] += 1

                                        await leads_collection.update_one(
                                            {"call_sid": state.lead.call_sid},
                                            {"$set": {
                                                "meeting_booked": True,
                                                "hasAppointment": True,
                                                "appointmentBooked": True,
                                                "updated_at": datetime.utcnow()
                                            }}
                                        )

                                        print(f"   ✅ AUTO-BOOKING COMPLETE!")
                                    else:
                                        print(f"   ❌ Auto-booking failed")
                                else:
                                    if not state.email_captured:
                                        print(
                                            f"⚠️ Cannot auto-book: No email captured yet")
                                    if not state.lead.interested:
                                        print(
                                            f"⚠️ Cannot auto-book: Lead not interested yet")
                                    if state.link_sent:
                                        print(
                                            f"⚠️ Cannot auto-book: Link already sent")
                            else:
                                print(f"⚠️ WARNING: stream_sid not in CALLS dict!")
                                print(f"   stream_sid = {stream_sid}")
                                print(f"   CALLS keys = {list(CALLS.keys())}")

                    except Exception as e:
                        print(f"Error in openai_to_twilio: {e}")
                        import traceback
                        traceback.print_exc()
                        break

            await asyncio.gather(twilio_to_openai(), openai_to_twilio())

    except WebSocketDisconnect:
        print("☎️  WS disconnected")
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        try:
            await twilio_ws.close()
        except Exception:
            pass

# --------------------------
# Startup/Shutdown
# --------------------------


@app.on_event("startup")
async def startup():
    print("\n🚀 Jennifer AI Starting...")

    # Test MongoDB connection
    try:
        await mongo_client.admin.command('ping')
        print("✅ MongoDB connected")

        # Show some stats
        total_leads = await leads_collection.count_documents({})
        print(f"📊 Total leads in database: {total_leads}")

    except Exception as e:
        print(f"❌ MongoDB connection failed: {e}")
        print(f"   Make sure MongoDB is running and URI is correct:")
        print(f"   {MONGODB_URI}")


@app.on_event("shutdown")
async def shutdown():
    print("\n👋 Shutting down Jennifer AI...")
    mongo_client.close()


@app.post("/test-auto-booking")
async def test_auto_booking():
    """Test endpoint to verify auto-booking works"""
    print("\n" + "="*60)
    print("🧪 TEST ENDPOINT CALLED")
    print("="*60 + "\n")

    success = await auto_book_meeting_after_call(
        lead_id="test123",
        lead_name="Test Business",
        email="dfsturge@gmail.com",
        phone="555-1234"
    )

    print("\n" + "="*60)
    print(f"🧪 TEST RESULT: {'✅ SUCCESS' if success else '❌ FAILED'}")
    print("="*60 + "\n")

    return {
        "success": success,
        "message": "Check Python terminal for detailed logs"
    }
# --------------------------
# Run
# --------------------------
if __name__ == "__main__":
    import uvicorn
    print("\n🚀 Starting Jennifer AI with Next.js Integration")
    print(f"   API will be available at: http://localhost:5001")
    print(f"   Make sure your Next.js app points to this URL\n")
    uvicorn.run(app, host="0.0.0.0", port=5001)
