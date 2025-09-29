from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import uvicorn, os, hashlib
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from datetime import datetime

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ✅ MongoDB Atlas connection
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
client = AsyncIOMotorClient(MONGO_URI)
db = client.todo_db
users_collection = db.users
tasks_collection = db.tasks

# ✅ Password hashing helper
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

# ✅ Helper to get current user from cookie
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
            "category": task.get("category", "General")
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
                   due_date: str = Form(None), category: str = Form("General")):
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
        "owner": username
    })
    return RedirectResponse("/", status_code=303)

@app.post("/delete/{task_id}")
async def delete_task(request: Request, task_id: str):
    username = get_current_user(request)
    if not username:
        return RedirectResponse("/", status_code=303)

    await tasks_collection.delete_one({"_id": ObjectId(task_id), "owner": username})
    return RedirectResponse("/", status_code=303)

@app.post("/toggle/{task_id}")
async def toggle_task(request: Request, task_id: str):
    username = get_current_user(request)
    if not username:
        return RedirectResponse("/", status_code=303)

    task = await tasks_collection.find_one({"_id": ObjectId(task_id), "owner": username})
    if task:
        new_status = not task.get("done", False)
        await tasks_collection.update_one({"_id": ObjectId(task_id)}, {"$set": {"done": new_status}})
    return RedirectResponse("/", status_code=303)

# --------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
