from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import uvicorn, os, hashlib
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# -------------------------
# Configuration / MongoDB
# -------------------------
MONGO_URI = os.environ.get("MONGO_URI", "your-mongodb-atlas-uri")
client = AsyncIOMotorClient(MONGO_URI)
db = client["todo_app"]
users_collection = db["users"]
tasks_collection = db["tasks"]

# -------------------------
# Helpers
# -------------------------
def get_current_user(request: Request):
    return request.cookies.get("username")

# -------------------------
# Auth (username/password only)
# - Auto signup if username not found.
# -------------------------
@app.post("/login")
async def login(response: Response, username: str = Form(...), password: str = Form(...)):
    hashed_pw = hashlib.sha256(password.encode()).hexdigest()
    user = await users_collection.find_one({"_id": username})

    if not user:
        await users_collection.insert_one({"_id": username, "password": hashed_pw})
    elif user["password"] != hashed_pw:
        return HTMLResponse("<h3>Invalid password</h3>", status_code=401)

    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie("username", username)
    return resp

@app.post("/logout")
async def logout(response: Response):
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie("username")
    return response

# -------------------------
# Dashboard (list view, tag filtering)
# -------------------------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request, tag: str = None):
    username = get_current_user(request)
    if not username:
        return templates.TemplateResponse("index.html", {"request": request, "page": "login"})

    query = {"owner": username}
    if tag:
        query["tags"] = tag

    tasks = []
    cursor = tasks_collection.find(query)
    async for t in cursor:
        tasks.append({
            "id": str(t["_id"]),
            "text": t["text"],
            "done": t["done"],
            "priority": t["priority"],
            "category": t["category"],
            "due_date": t.get("due_date", ""),
            "recurring": t.get("recurring", "None"),
            "subtasks": t.get("subtasks", []),
            "tags": t.get("tags", [])
        })

    # Collect all tags (for tag filters)
    all_tags = set()
    cursor2 = tasks_collection.find({"owner": username})
    async for t in cursor2:
        for tg in t.get("tags", []):
            all_tags.add(tg)

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
        "pending": pending,
        "all_tags": sorted(list(all_tags)),
        "active_tag": tag or ""
    })

# -------------------------
# Events endpoint for calendar (returns id,title,start,color)
# Accepts optional ?tag=... to filter events by tag
# -------------------------
@app.get("/events")
async def get_events(request: Request):
    username = get_current_user(request)
    if not username:
        return JSONResponse([])

    tag = request.query_params.get("tag")
    query = {"owner": username}
    if tag:
        query["tags"] = tag

    events = []
    cursor = tasks_collection.find(query)
    async for t in cursor:
        if t.get("due_date"):
            events.append({
                "id": str(t["_id"]),
                "title": t["text"],
                "start": t["due_date"],
                "color": "#2563eb" if not t.get("done") else "#9ca3af"
            })

    return JSONResponse(events)

# -------------------------
# Get single task (used by calendar modal)
# -------------------------
@app.get("/task/{task_id}")
async def get_task(task_id: str, request: Request):
    username = get_current_user(request)
    if not username:
        return JSONResponse({})

    task = await tasks_collection.find_one({"_id": ObjectId(task_id), "owner": username})
    if not task:
        return JSONResponse({})

    return JSONResponse({
        "id": str(task["_id"]),
        "text": task["text"],
        "done": task["done"],
        "priority": task["priority"],
        "category": task["category"],
        "due_date": task.get("due_date", ""),
        "recurring": task.get("recurring", "None"),
        "subtasks": task.get("subtasks", []),
        "tags": task.get("tags", [])
    })

# -------------------------
# Add Task (tags + subtasks supported)
# -------------------------
@app.post("/add")
async def add_task(request: Request,
                   task: str = Form(...),
                   priority: str = Form("Medium"),
                   due_date: str = Form(None),
                   category: str = Form("General"),
                   recurring: str = Form("None"),
                   subtasks: str = Form(""),
                   tags: str = Form("")):
    username = get_current_user(request)
    if not username:
        return RedirectResponse("/", status_code=302)

    subtasks_list = [{"text": s.strip(), "done": False} for s in subtasks.split(",") if s.strip()]
    tags_list = [t.strip() for t in tags.split(",") if t.strip()]

    new_task = {
        "text": task,
        "done": False,
        "priority": priority,
        "category": category,
        "due_date": due_date if due_date else None,
        "recurring": recurring,
        "subtasks": subtasks_list,
        "tags": tags_list,
        "owner": username
    }
    await tasks_collection.insert_one(new_task)
    return RedirectResponse("/", status_code=302)

# -------------------------
# Add subtask AFTER creation
# -------------------------
@app.post("/add-subtask/{task_id}")
async def add_subtask(task_id: str, subtask: str = Form(...)):
    task = await tasks_collection.find_one({"_id": ObjectId(task_id)})
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    subtasks = task.get("subtasks", [])
    subtasks.append({"text": subtask, "done": False})
    await tasks_collection.update_one({"_id": ObjectId(task_id)}, {"$set": {"subtasks": subtasks}})
    return RedirectResponse("/", status_code=302)

# -------------------------
# Toggle task completion
# -------------------------
@app.post("/toggle/{task_id}")
async def toggle_task(task_id: str):
    task = await tasks_collection.find_one({"_id": ObjectId(task_id)})
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    new_status = not task["done"]
    await tasks_collection.update_one({"_id": ObjectId(task_id)}, {"$set": {"done": new_status}})
    return RedirectResponse("/", status_code=302)

# -------------------------
# Toggle subtask completion
# -------------------------
@app.post("/toggle-sub/{task_id}/{sub_index}")
async def toggle_subtask(task_id: str, sub_index: int):
    task = await tasks_collection.find_one({"_id": ObjectId(task_id)})
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    subtasks = task.get("subtasks", [])
    if 0 <= sub_index < len(subtasks):
        subtasks[sub_index]["done"] = not subtasks[sub_index]["done"]
        await tasks_collection.update_one({"_id": ObjectId(task_id)}, {"$set": {"subtasks": subtasks}})

    return RedirectResponse("/", status_code=302)

# -------------------------
# Delete a task
# -------------------------
@app.post("/delete/{task_id}")
async def delete_task(task_id: str):
    await tasks_collection.delete_one({"_id": ObjectId(task_id)})
    return RedirectResponse("/", status_code=302)

# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
