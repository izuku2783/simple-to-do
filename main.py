from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import uvicorn, os, hashlib
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from datetime import datetime, timedelta
import pandas as pd
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ✅ MongoDB Connection
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
client = AsyncIOMotorClient(MONGO_URI)
db = client.todo_db
users_collection = db.users
tasks_collection = db.tasks

# ✅ Password hashing helper
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def get_current_user(request: Request):
    return request.cookies.get("username")

# ---------------- ROUTES ----------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, category: str = None, search: str = None):
    username = get_current_user(request)
    if not username:
        return templates.TemplateResponse("index.html", {"request": request, "page": "login"})

    query = {"owner": username}
    if category:
        query["category"] = category
    if search:
        query["text"] = {"$regex": search, "$options": "i"}

    tasks_cursor = tasks_collection.find(query).sort("priority", -1)
    tasks = []
    async for task in tasks_cursor:
        tasks.append({
            "id": str(task["_id"]),
            "text": task["text"],
            "done": task.get("done", False),
            "priority": task.get("priority", "Medium"),
            "due_date": task.get("due_date"),
            "category": task.get("category", "General"),
            "recurring": task.get("recurring", "None"),
            "subtasks": task.get("subtasks", [])
        })

    total = await tasks_collection.count_documents({"owner": username})
    completed = await tasks_collection.count_documents({"owner": username, "done": True})
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

# ---------- AUTH ----------

@app.post("/register")
async def register(username: str = Form(...), password: str = Form(...)):
    existing = await users_collection.find_one({"username": username})
    if existing:
        return RedirectResponse("/?page=register&error=exists", status_code=303)
    await users_collection.insert_one({"username": username, "password": hash_password(password)})
    return RedirectResponse("/", status_code=303)

@app.post("/login")
async def login(response: Response, username: str = Form(...), password: str = Form(...)):
    user = await users_collection.find_one({"username": username})
    if not user or user["password"] != hash_password(password):
        return RedirectResponse("/?page=login&error=invalid", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(key="username", value=username, httponly=True)
    return resp

@app.post("/logout")
async def logout(response: Response):
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("username")
    return resp

# ---------- TASKS ----------

@app.post("/add")
async def add_task(request: Request, task: str = Form(...), priority: str = Form("Medium"),
                   due_date: str = Form(None), category: str = Form("General"),
                   recurring: str = Form("None")):
    username = get_current_user(request)
    if not username:
        return RedirectResponse("/", status_code=303)

    due = datetime.strptime(due_date, "%Y-%m-%d") if due_date else None
    await tasks_collection.insert_one({
        "text": task,
        "done": False,
        "priority": priority,
        "due_date": due.strftime("%Y-%m-%d") if due else None,
        "category": category,
        "recurring": recurring,
        "subtasks": [],
        "owner": username
    })
    return RedirectResponse("/", status_code=303)

@app.post("/delete/{task_id}")
async def delete_task(request: Request, task_id: str):
    username = get_current_user(request)
    await tasks_collection.delete_one({"_id": ObjectId(task_id), "owner": username})
    return RedirectResponse("/", status_code=303)

@app.post("/toggle/{task_id}")
async def toggle_task(request: Request, task_id: str):
    username = get_current_user(request)
    task = await tasks_collection.find_one({"_id": ObjectId(task_id), "owner": username})
    if task:
        new_status = not task.get("done", False)
        await tasks_collection.update_one({"_id": ObjectId(task_id)}, {"$set": {"done": new_status}})
        # If recurring and done → schedule next occurrence
        if new_status and task.get("recurring") != "None" and task.get("due_date"):
            due = datetime.strptime(task["due_date"], "%Y-%m-%d")
            if task["recurring"] == "Daily":
                due += timedelta(days=1)
            elif task["recurring"] == "Weekly":
                due += timedelta(weeks=1)
            elif task["recurring"] == "Monthly":
                due += timedelta(days=30)
            await tasks_collection.insert_one({
                "text": task["text"],
                "done": False,
                "priority": task["priority"],
                "due_date": due.strftime("%Y-%m-%d"),
                "category": task["category"],
                "recurring": task["recurring"],
                "subtasks": [],
                "owner": username
            })
    return RedirectResponse("/", status_code=303)

# ---------- SUBTASKS ----------

@app.post("/add_subtask/{task_id}")
async def add_subtask(request: Request, task_id: str, subtask: str = Form(...)):
    username = get_current_user(request)
    await tasks_collection.update_one(
        {"_id": ObjectId(task_id), "owner": username},
        {"$push": {"subtasks": {"text": subtask, "done": False}}}
    )
    return RedirectResponse("/", status_code=303)

@app.post("/toggle_subtask/{task_id}/{index}")
async def toggle_subtask(request: Request, task_id: str, index: int):
    username = get_current_user(request)
    task = await tasks_collection.find_one({"_id": ObjectId(task_id), "owner": username})
    if task:
        subtasks = task.get("subtasks", [])
        if 0 <= index < len(subtasks):
            subtasks[index]["done"] = not subtasks[index]["done"]
            await tasks_collection.update_one(
                {"_id": ObjectId(task_id), "owner": username},
                {"$set": {"subtasks": subtasks}}
            )
    return RedirectResponse("/", status_code=303)

# ---------- EXPORT ----------

@app.get("/export_excel")
async def export_excel(request: Request):
    username = get_current_user(request)
    tasks_cursor = tasks_collection.find({"owner": username})
    tasks = []
    async for task in tasks_cursor:
        tasks.append(task)
    df = pd.DataFrame(tasks)
    filename = "tasks.xlsx"
    df.to_excel(filename, index=False)
    return FileResponse(filename, filename=filename)

@app.get("/export_pdf")
async def export_pdf(request: Request):
    username = get_current_user(request)
    tasks_cursor = tasks_collection.find({"owner": username})
    tasks = []
    async for task in tasks_cursor:
        tasks.append(task)

    filename = "tasks.pdf"
    doc = SimpleDocTemplate(filename)
    styles = getSampleStyleSheet()
    story = [Paragraph(f"{username}'s Tasks", styles["Title"]), Spacer(1, 12)]
    for t in tasks:
        line = f"{t['text']} | Priority: {t.get('priority','')} | Category: {t.get('category','')} | Due: {t.get('due_date','')}"
        story.append(Paragraph(line, styles["Normal"]))
        story.append(Spacer(1, 12))
    doc.build(story)
    return FileResponse(filename, filename=filename)

# ---------- CALENDAR ----------

@app.get("/calendar", response_class=JSONResponse)
async def calendar_events(request: Request):
    username = get_current_user(request)
    if not username:
        return []
    tasks_cursor = tasks_collection.find({"owner": username, "due_date": {"$ne": None}})
    events = []
    async for task in tasks_cursor:
        events.append({
            "id": str(task["_id"]),
            "title": task["text"],
            "start": task["due_date"],
            "color": "#10B981" if task.get("done") else "#EF4444"
        })
    return events

@app.get("/task/{task_id}", response_class=JSONResponse)
async def get_task_details(task_id: str, request: Request):
    username = get_current_user(request)
    task = await tasks_collection.find_one({"_id": ObjectId(task_id), "owner": username})
    if task:
        return {
            "text": task["text"],
            "priority": task.get("priority"),
            "category": task.get("category"),
            "due_date": task.get("due_date"),
            "recurring": task.get("recurring"),
            "done": task.get("done"),
            "subtasks": task.get("subtasks", [])
        }
    return {"error": "Task not found"}

# --------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
