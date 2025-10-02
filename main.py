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
from passlib.context import CryptContext

# Basic Logging Configuration
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Environment Variable Loading
SECRET_KEY = os.environ.get("SECRET_KEY")
MONGO_URI = os.environ.get("MONGO_URI")

if not SECRET_KEY or not MONGO_URI:
    raise RuntimeError("Missing SECRET_KEY or MONGO_URI env vars.")

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
templates = Jinja2Templates(directory="templates")

# Password Hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# MongoDB Connection
try:
    client = AsyncIOMotorClient(MONGO_URI)
    db = client["todo_app"]
    users_collection = db["users"]
    tasks_collection = db["tasks"]
    projects_collection = db["projects"]
    logger.info("MongoDB connection successful.")
except Exception as e:
    logger.critical(f"CRITICAL ERROR: Failed to connect to MongoDB. Error: {e}")
    raise

def get_current_user(request: Request):
    return request.cookies.get("username")

# Dependency for getting the current user, for protected routes
async def current_user(request: Request):
    username = get_current_user(request)
    if not username:
        return None
    return await users_collection.find_one({"_id": username})

# Health Check Endpoint for Render
@app.get("/health")
async def health_check():
    return {"status": "ok"}

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
        return HTMLResponse("<h3>Invalid username or password</h3><a href='/'>Try again</a>", status_code=401)

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
    await tasks_collection.insert_one(new_task)
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
