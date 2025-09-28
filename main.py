from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import uvicorn, os
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from passlib.hash import bcrypt
from itsdangerous import URLSafeSerializer

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# --- MongoDB Setup ---
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
client = AsyncIOMotorClient(MONGO_URI)
db = client.todo_db
users_collection = db.users
tasks_collection = db.tasks

# --- Session Setup ---
SECRET_KEY = os.environ.get("SECRET_KEY", "supersecret")
serializer = URLSafeSerializer(SECRET_KEY)

def get_current_user(request: Request):
    session = request.cookies.get("session")
    if not session:
        return None
    try:
        data = serializer.loads(session)
        return data.get("username")
    except Exception:
        return None


# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    username = get_current_user(request)
    if not username:
        return templates.TemplateResponse("index.html", {"request": request, "page": "login"})

    tasks_cursor = tasks_collection.find({"username": username})
    tasks = []
    async for task in tasks_cursor:
        tasks.append({"id": str(task["_id"]), "text": task["text"], "done": task.get("done", False)})

    return templates.TemplateResponse("index.html", {"request": request, "page": "home", "username": username, "tasks": tasks})


@app.post("/register")
async def register(username: str = Form(...), password: str = Form(...)):
    existing = await users_collection.find_one({"username": username})
    if existing:
        return RedirectResponse("/?page=register&error=exists", status_code=303)
    hashed_pw = bcrypt.hash(password)
    await users_collection.insert_one({"username": username, "password": hashed_pw})
    return RedirectResponse("/", status_code=303)


@app.post("/login")
async def login(response: Response, username: str = Form(...), password: str = Form(...)):
    user = await users_collection.find_one({"username": username})
    if not user or not bcrypt.verify(password, user["password"]):
        return RedirectResponse("/?page=login&error=invalid", status_code=303)
    session = serializer.dumps({"username": username})
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("session", session, httponly=True)
    return response


@app.get("/logout")
async def logout(response: Response):
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("session")
    return response


@app.post("/add")
async def add_task(request: Request, task: str = Form(...)):
    username = get_current_user(request)
    if not username:
        return RedirectResponse("/")
    await tasks_collection.insert_one({"username": username, "text": task, "done": False})
    return RedirectResponse("/", status_code=303)


@app.post("/delete/{task_id}")
async def delete_task(request: Request, task_id: str):
    username = get_current_user(request)
    if not username:
        return RedirectResponse("/")
    await tasks_collection.delete_one({"_id": ObjectId(task_id), "username": username})
    return RedirectResponse("/", status_code=303)


@app.post("/toggle/{task_id}")
async def toggle_task(request: Request, task_id: str):
    username = get_current_user(request)
    if not username:
        return RedirectResponse("/")
    task = await tasks_collection.find_one({"_id": ObjectId(task_id), "username": username})
    if task:
        new_status = not task.get("done", False)
        await tasks_collection.update_one({"_id": ObjectId(task_id)}, {"$set": {"done": new_status}})
    return RedirectResponse("/", status_code=303)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
