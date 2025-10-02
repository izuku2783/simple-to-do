import logging
import os
import json
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from fastapi import FastAPI, Form, Request, Response, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from passlib.context import CryptContext

# --- Step 1: Configure Logging ---
# This sets up a logger that will print detailed messages to the console.
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

logger.info("Application startup sequence initiated.")

# --- Step 2: Environment Variable Loading & Validation ---
try:
    logger.info("Loading SECRET_KEY...")
    SECRET_KEY = os.environ["SECRET_KEY"]
    logger.info("SECRET_KEY loaded successfully.")

    logger.info("Loading MONGO_URI...")
    MONGO_URI = os.environ["MONGO_URI"]
    logger.info("MONGO_URI loaded successfully.")

    logger.info("Loading GOOGLE_CREDENTIALS...")
    GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]
    # Validate that GOOGLE_CREDENTIALS is valid JSON
    json.loads(GOOGLE_CREDENTIALS)
    logger.info("GOOGLE_CREDENTIALS loaded and validated as JSON successfully.")

except KeyError as e:
    logger.critical(f"CRITICAL ERROR: Environment variable {e} is not set. Application cannot start.")
    raise
except json.JSONDecodeError as e:
    logger.critical(f"CRITICAL ERROR: GOOGLE_CREDENTIALS is not valid JSON. Error: {e}. Application cannot start.")
    raise

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
templates = Jinja2Templates(directory="templates")

# Password Hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# --- Step 3: MongoDB Connection ---
db = None
try:
    logger.info("Attempting to connect to MongoDB...")
    client = AsyncIOMotorClient(MONGO_URI)
    # The ismaster command is cheap and does not require auth. It's a quick way to check server availability.
    client.admin.command('ismaster')
    db = client["todo_app"]
    users_collection = db["users"]
    tasks_collection = db["tasks"]
    projects_collection = db["projects"]
    logger.info("MongoDB connection successful. Collections are ready.")
except Exception as e:
    logger.critical(f"CRITICAL ERROR: Failed to connect to MongoDB. Error: {e}")
    # We raise the exception to ensure the app stops if the DB is unavailable.
    raise

# (The rest of your application code remains the same)
# ✅ Google OAuth
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "openid", "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile"
]
REDIRECT_PATH = "/auth/callback"

def get_google_flow(request: Request):
    creds_data = json.loads(GOOGLE_CREDENTIALS)
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

# Dependency for getting the current user, for protected routes
async def current_user(request: Request):
    username = get_current_user(request)
    if not username:
        return None
    return await users_collection.find_one({"_id": username})

# ✅ Google Login
@app.get("/login-google")
async def login_google(request: Request):
    flow = get_google_flow(request)
    auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true")
    request.session['state'] = state
    return RedirectResponse(auth_url)

@app.get(REDIRECT_PATH)
async def auth_callback(request: Request):
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
    # Check if user exists, if not create with default settings
    user = await users_collection.find_one({"_id": email})
    if not user:
        await users_collection.insert_one({
            "_id": email,
            "settings": {"default_priority": "Medium", "theme": "light"},
        })

    response = RedirectResponse("/")
    response.set_cookie("username", email)
    return response

# ✅ Username/Password Login
@app.post("/login")
async def login(response: Response, username: str = Form(...), password: str = Form(...)):
    user = await users_collection.find_one({"_id": username})

    if not user:
        hashed_pw = pwd_context.hash(password)
        await users_collection.insert_one({
            "_id": username,
            "password": hashed_pw,
            "settings": {"default_priority": "Medium", "theme": "light"},
        })
    elif not user.get("password") or not pwd_context.verify(password, user["password"]):
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
async def home(request: Request, project_id: str = None, user: dict = Depends(current_user)):
    if not user:
        return templates.TemplateResponse("index.html", {"request": request, "page": "login"})

    query = {"owner": user["_id"]}
    if project_id:
        query["project_id"] = ObjectId(project_id)

    tasks_cursor = tasks_collection.find(query)
    tasks = []
    async for t in tasks_cursor:
        tasks.append({**t, "id": str(t["_id"])})

    projects_cursor = projects_collection.find({"owner": user["_id"]})
    projects = []
    async for p in projects_cursor:
        projects.append({**p, "id": str(p["_id"])})

    total = len(tasks)
    completed = len([t for t in tasks if t["done"]])
    pending = total - completed

    return templates.TemplateResponse("index.html", {
        "request": request,
        "page": "home",
        "username": user["_id"],
        "tasks": tasks,
        "projects": projects,
        "current_project_id": project_id,
        "total": total,
        "completed": completed,
        "pending": pending,
        "user_settings": user.get("settings", {"default_priority": "Medium", "theme": "light"})
    })


# ✅ Events API (for FullCalendar)
@app.get("/events")
async def get_events(request: Request, user: dict = Depends(current_user)):
    if not user:
        return JSONResponse([])

    events = []

    tasks_cursor = tasks_collection.find({"owner": user["_id"]})
    async for t in tasks_cursor:
        if t.get("due_date"):
            events.append({
                "title": t["text"],
                "start": t["due_date"],
                "color": "#2563eb" if not t["done"] else "#9ca3af"
            })

    creds = await load_token(user["_id"])
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
            logger.error(f"Error fetching Google Calendar events: {e}")

    return JSONResponse(events)

# ✅ Add Task
@app.post("/add")
async def add_task(request: Request, task: str = Form(...), priority: str = Form("Medium"),
                   due_date: str = Form(None), category: str = Form("General"), recurring: str = Form("None"),
                   subtasks: str = Form(""), tags: str = Form(""), project_id: str = Form(None),
                   user: dict = Depends(current_user)):
    if not user:
        return RedirectResponse("/", status_code=302)

    subtasks_list = [{"text": s.strip(), "done": False} for s in subtasks.split(",") if s.strip()]
    tags_list = [s.strip() for s in tags.split(",") if s.strip()]

    new_task = {
        "text": task,
        "done": False,
        "priority": priority,
        "category": category,
        "due_date": due_date if due_date else None,
        "recurring": recurring,
        "subtasks": subtasks_list,
        "tags": tags_list,
        "project_id": ObjectId(project_id) if project_id else None,
        "owner": user["_id"],
        "notes": [],
        "history": [{
            "timestamp": datetime.now(timezone.utc),
            "action": "Task created"
        }]
    }
    result = await tasks_collection.insert_one(new_task)

    creds = await load_token(user["_id"])
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
            logger.error(f"Error creating Google Calendar event: {e}")

    return RedirectResponse(f"/?project_id={project_id}" if project_id else "/", status_code=302)

# ✅ Toggle Task Completion
@app.post("/toggle/{task_id}")
async def toggle_task(task_id: str, user: dict = Depends(current_user)):
    if not user:
        return RedirectResponse("/", status_code=302)

    task = await tasks_collection.find_one({"_id": ObjectId(task_id), "owner": user["_id"]})
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    new_status = not task["done"]
    action = "Task marked as complete" if new_status else "Task marked as incomplete"
    await tasks_collection.update_one(
        {"_id": ObjectId(task_id)},
        {
            "$set": {"done": new_status},
            "$push": {"history": {"timestamp": datetime.now(timezone.utc), "action": action}}
        }
    )
    project_id = task.get("project_id")
    return RedirectResponse(f"/?project_id={project_id}" if project_id else "/", status_code=302)


# ✅ Add Note to Task
@app.post("/add_note/{task_id}")
async def add_note(task_id: str, note: str = Form(...), user: dict = Depends(current_user)):
    if not user:
        return RedirectResponse("/", status_code=302)

    task = await tasks_collection.find_one({"_id": ObjectId(task_id), "owner": user["_id"]})
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    new_note = {"timestamp": datetime.now(timezone.utc), "text": note}
    await tasks_collection.update_one(
        {"_id": ObjectId(task_id)},
        {"$push": {"notes": new_note, "history": {"timestamp": datetime.now(timezone.utc), "action": "Note added"}}}
    )
    project_id = task.get("project_id")
    return RedirectResponse(f"/?project_id={project_id}" if project_id else "/", status_code=302)


# ✅ Project Management
@app.post("/add_project")
async def add_project(name: str = Form(...), user: dict = Depends(current_user)):
    if not user:
        return RedirectResponse("/", status_code=302)

    new_project = {"name": name, "owner": user["_id"]}
    await projects_collection.insert_one(new_project)
    return RedirectResponse("/", status_code=302)

# ✅ Settings
@app.post("/settings")
async def update_settings(default_priority: str = Form(...), theme: str = Form(...), user: dict = Depends(current_user)):
    if not user:
        return RedirectResponse("/", status_code=302)

    await users_collection.update_one(
        {"_id": user["_id"]},
        {"$set": {"settings": {"default_priority": default_priority, "theme": theme}}}
    )
    return RedirectResponse("/", status_code=302)


logger.info("Application startup sequence completed. Uvicorn is now running the app.")
