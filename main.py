from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import uvicorn, os, pickle, json
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime

from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ✅ MongoDB Connection
MONGO_URI = os.environ.get("MONGO_URI", "your-mongodb-atlas-uri")
client = AsyncIOMotorClient(MONGO_URI)
db = client["todo_app"]
users_collection = db["users"]
tasks_collection = db["tasks"]

# ✅ Google OAuth (load from env instead of file)
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "openid", "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile"
]
REDIRECT_PATH = "/auth/callback"

def get_google_flow(request: Request):
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_json:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS env var")
    creds_data = json.loads(creds_json)
    flow = Flow.from_client_config(
        creds_data,
        scopes=SCOPES,
        redirect_uri=f"{request.url.scheme}://{request.url.hostname}{REDIRECT_PATH}"
    )
    return flow

# 🔑 Helpers for token storage
async def save_token(user_id, creds):
    await users_collection.update_one(
        {"_id": user_id},
        {"$set": {"google_creds": pickle.dumps(creds)}},
        upsert=True
    )

async def load_token(user_id):
    user = await users_collection.find_one({"_id": user_id})
    if user and "google_creds" in user:
        return pickle.loads(user["google_creds"])
    return None

def get_current_user(request: Request):
    return request.cookies.get("username")

# ✅ Google Login
@app.get("/login-google")
async def login_google(request: Request):
    flow = get_google_flow(request)
    auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true")
    request.session = {"state": state}
    return RedirectResponse(auth_url)

@app.get(REDIRECT_PATH)
async def auth_callback(request: Request, response: Response):
    flow = get_google_flow(request)
    flow.fetch_token(authorization_response=str(request.url))
    creds = flow.credentials

    # Get user profile
    service = build("oauth2", "v2", credentials=creds)
    user_info = service.userinfo().get().execute()
    email = user_info["email"]

    await save_token(email, creds)

    response = RedirectResponse("/")
    response.set_cookie("username", email)
    return response

@app.post("/logout")
async def logout(response: Response):
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie("username")
    return response

# ✅ Dashboard
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    username = get_current_user(request)
    if not username:
        return templates.TemplateResponse("index.html", {"request": request, "page": "login"})

    tasks = []
    tasks_cursor = tasks_collection.find({"owner": username})
    async for t in tasks_cursor:
        tasks.append({
            "id": str(t["_id"]),
            "text": t["text"],
            "done": t["done"],
            "priority": t["priority"],
            "category": t["category"],
            "due_date": t.get("due_date", ""),
            "recurring": t.get("recurring", "None"),
            "subtasks": t.get("subtasks", [])
        })

    total = len(tasks)
    completed = len([t for t in tasks if t["done"]])
    pending = total - completed

    return templates.TemplateResponse("index.html", {
        "request": request,
        "page": "home",
        "username": username,
        "tasks": tasks,
        "total": total,
        "completed": completed,
        "pending": pending
    })

# ✅ API for FullCalendar → combine MongoDB + Google Calendar
@app.get("/events")
async def get_events(request: Request):
    username = get_current_user(request)
    if not username:
        return JSONResponse([])

    events = []

    # Local tasks
    tasks_cursor = tasks_collection.find({"owner": username})
    async for t in tasks_cursor:
        if t.get("due_date"):
            events.append({
                "title": t["text"],
                "start": t["due_date"],
                "color": "#2563eb"  # Tailwind blue
            })

    # Google Calendar events
    creds = await load_token(username)
    if creds:
        service = build("calendar", "v3", credentials=creds)
        events_result = service.events().list(
            calendarId="primary",
            timeMin=datetime.utcnow().isoformat() + "Z",
            maxResults=50,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        for e in events_result.get("items", []):
            start = e["start"].get("date", e["start"].get("dateTime"))
            if start:
                events.append({
                    "title": e.get("summary", "Google Event"),
                    "start": start,
                    "color": "#16a34a"  # Tailwind green
                })

    return JSONResponse(events)

# ✅ Add Task (also push to Google Calendar)
@app.post("/add")
async def add_task(request: Request, task: str = Form(...), priority: str = Form("Medium"),
                   due_date: str = Form(None), category: str = Form("General"), recurring: str = Form("None"),
                   subtasks: str = Form("")):
    username = get_current_user(request)
    if not username:
        return RedirectResponse("/", status_code=302)

    subtasks_list = [{"text": s.strip(), "done": False} for s in subtasks.split(",") if s.strip()]

    new_task = {
        "text": task,
        "done": False,
        "priority": priority,
        "category": category,
        "due_date": due_date if due_date else None,
        "recurring": recurring,
        "subtasks": subtasks_list,
        "owner": username
    }
    await tasks_collection.insert_one(new_task)

    creds = await load_token(username)
    if creds and due_date:
        service = build("calendar", "v3", credentials=creds)
        event = {
            "summary": task,
            "description": f"Priority: {priority}\nCategory: {category}\nSubtasks: {', '.join([s['text'] for s in subtasks_list])}",
            "start": {"date": due_date},
            "end": {"date": due_date},
        }
        if recurring != "None":
            freq = recurring.upper()
            event["recurrence"] = [f"RRULE:FREQ={freq}"]
        service.events().insert(calendarId="primary", body=event).execute()

    return RedirectResponse("/", status_code=302)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
