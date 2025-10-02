from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import os
import json
import hashlib
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from datetime import datetime

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from passlib.context import CryptContext

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Password Hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ✅ MongoDB
MONGO_URI = os.environ.get("MONGO_URI", "your-mongodb-atlas-uri")
client = AsyncIOMotorClient(MONGO_URI)
db = client["todo_app"]
users_collection = db["users"]
tasks_collection = db["tasks"]

# ✅ Google OAuth
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
    
    # Correctly determine the redirect URI scheme
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    redirect_uri = f"{scheme}://{request.url.netloc}{REDIRECT_PATH}"

    return Flow.from_client_config(
        creds_data,
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )

# 🔑 Token helpers
async def save_token(user_id, creds):
    await users_collection.update_one(
        {"_id": user_id},
        {"$set": {"google_creds": json.loads(creds.to_json())}},
        upsert=True
    )

async def load_token(user_id):
    user = await users_collection.find_one({"_id": user_id})
    if user and "google_creds" in user:
        return Credentials.from_authorized_user_info(user["google_creds"])
    return None

def get_current_user(request: Request):
    return request.cookies.get("username")

# ✅ Google Login
@app.get("/login-google")
async def login_google(request: Request):
    flow = get_google_flow(request)
    auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true")
    # Store the state in the session to verify on callback
    request.session['state'] = state
    return RedirectResponse(auth_url)

@app.get(REDIRECT_PATH)
async def auth_callback(request: Request):
    # Verify the state to prevent CSRF attacks
    state = request.session.pop('state', '')
    if state != request.query_params.get('state'):
        return HTMLResponse("<h3>State mismatch. Please try again.</h3>", status_code=400)

    flow = get_google_flow(request)
    flow.fetch_token(code=request.query_params.get('code'))
    creds = flow.credentials

    service = build("oauth2", "v2", credentials=creds)
    user_info = service.userinfo().get().execute()
    email = user_info["email"]

    await save_token(email, creds)

    response = RedirectResponse("/")
    response.set_cookie("username", email)
    return response

# ✅ Username/Password Login
@app.post("/login")
async def login(response: Response, username: str = Form(...), password: str = Form(...)):
    user = await users_collection.find_one({"_id": username})

    if not user:
        # Hash the password before storing
        hashed_pw = pwd_context.hash(password)
        await users_collection.insert_one({"_id": username, "password": hashed_pw})
    elif not pwd_context.verify(password, user["password"]):
        return HTMLResponse("<h3>Invalid password</h3>", status_code=401)

    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie("username", username)
    return resp

@app.post("/logout")
async def logout():
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

# ✅ Events API (for FullCalendar)
@app.get("/events")
async def get_events(request: Request):
    username = get_current_user(request)
    if not username:
        return JSONResponse([])

    events = []

    tasks_cursor = tasks_collection.find({"owner": username})
    async for t in tasks_cursor:
        if t.get("due_date"):
            events.append({
                "title": t["text"],
                "start": t["due_date"],
                "color": "#2563eb" if not t["done"] else "#9ca3af"
            })

    creds = await load_token(username)
    if creds:
        try:
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
                        "color": "#16a34a"
                    })
        except Exception as e:
            print(f"Error fetching Google Calendar events: {e}")

    return JSONResponse(events)

# ✅ Add Task
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
    result = await tasks_collection.insert_one(new_task)

    creds = await load_token(username)
    if creds and due_date:
        try:
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
        except Exception as e:
            print(f"Error creating Google Calendar event: {e}")

    return RedirectResponse("/", status_code=302)

# ✅ Toggle Task Completion
@app.post("/toggle/{task_id}")
async def toggle_task(task_id: str):
    task = await tasks_collection.find_one({"_id": ObjectId(task_id)})
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    new_status = not task["done"]
    await tasks_collection.update_one({"_id": ObjectId(task_id)}, {"$set": {"done": new_status}})
    return RedirectResponse("/", status_code=302)

# ✅ Toggle Subtask Completion
@app.post("/toggle-sub/{task_id}/{sub_index}")
async def toggle_subtask(task_id: str, sub_index: int):
    task = await tasks_collection.find_one({"_id": ObjectId(task_id)})
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    subtasks = task.get("subtasks", [])
    if sub_index < len(subtasks):
        subtasks[sub_index]["done"] = not subtasks[sub_index]["done"]
        await tasks_collection.update_one({"_id": ObjectId(task_id)}, {"$set": {"subtasks": subtasks}})

    return RedirectResponse("/", status_code=302)