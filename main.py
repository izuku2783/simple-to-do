from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
import uvicorn, os, hashlib
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId

app = FastAPI()

# ✅ MongoDB Atlas connection
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
client = AsyncIOMotorClient(MONGO_URI)
db = client.todo_db
users_collection = db.users
tasks_collection = db.tasks

# ✅ Password hashing helper
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

# ✅ Render login/register page
def render_login(message=""):
    return f"""
    <html>
    <head><title>Login</title></head>
    <body>
        <h2>Login</h2>
        <form action="/login" method="post">
            <input type="text" name="username" placeholder="Username" required>
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">Login</button>
        </form>
        <h2>Register</h2>
        <form action="/register" method="post">
            <input type="text" name="username" placeholder="Username" required>
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">Register</button>
        </form>
        <p style="color:red;">{message}</p>
    </body>
    </html>
    """

# ✅ Render todo list
def render_todo(username, tasks):
    html = f"""
    <html>
    <head>
        <title>{username}'s To-Do List</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 40px; }}
            h1 {{ color: #333; }}
            ul {{ list-style-type: none; padding: 0; }}
            li {{ margin: 8px 0; }}
            form {{ display: inline; }}
            .done {{ text-decoration: line-through; color: gray; }}
        </style>
    </head>
    <body>
        <h1>{username}'s To-Do List</h1>
        <form action="/add" method="post">
            <input type="text" name="task" placeholder="Enter a task" required>
            <button type="submit">Add</button>
        </form>
        <form action="/logout" method="post" style="margin-left:20px;">
            <button type="submit">Logout</button>
        </form>
        <ul>
    """
    for task in tasks:
        html += f"""
        <li>
            <span class="{'done' if task['done'] else ''}">{task['text']}</span>
            <form action="/toggle/{task['id']}" method="post">
                <button type="submit">{'Undo' if task['done'] else 'Done'}</button>
            </form>
            <form action="/delete/{task['id']}" method="post">
                <button type="submit">Delete</button>
            </form>
        </li>
        """
    html += """
        </ul>
    </body>
    </html>
    """
    return html

# ✅ Middleware-like helper to get logged-in user
async def get_current_user(request: Request):
    username = request.cookies.get("username")
    if username:
        return username
    return None

# ------------------ ROUTES ------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    username = await get_current_user(request)
    if not username:
        return render_login()

    # Load only this user's tasks
    tasks_cursor = tasks_collection.find({"owner": username})
    tasks = []
    async for task in tasks_cursor:
        tasks.append({
            "id": str(task["_id"]),
            "text": task["text"],
            "done": task.get("done", False)
        })
    return render_todo(username, tasks)

@app.post("/register")
async def register(username: str = Form(...), password: str = Form(...)):
    existing = await users_collection.find_one({"username": username})
    if existing:
        return HTMLResponse(render_login("❌ Username already exists"), status_code=400)

    await users_collection.insert_one({
        "username": username,
        "password": hash_password(password)
    })
    return RedirectResponse(url="/", status_code=303)

@app.post("/login")
async def login(response: Response, username: str = Form(...), password: str = Form(...)):
    user = await users_collection.find_one({"username": username})
    if not user or user["password"] != hash_password(password):
        return HTMLResponse(render_login("❌ Invalid username or password"), status_code=400)

    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(key="username", value=username)  # ✅ store session
    return resp

@app.post("/logout")
async def logout(response: Response):
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie("username")
    return resp

@app.post("/add")
async def add_task(request: Request, task: str = Form(...)):
    username = await get_current_user(request)
    if not username:
        return RedirectResponse(url="/", status_code=303)

    await tasks_collection.insert_one({"text": task, "done": False, "owner": username})
    return RedirectResponse(url="/", status_code=303)

@app.post("/delete/{task_id}")
async def delete_task(request: Request, task_id: str):
    username = await get_current_user(request)
    if not username:
        return RedirectResponse(url="/", status_code=303)

    await tasks_collection.delete_one({"_id": ObjectId(task_id), "owner": username})
    return RedirectResponse(url="/", status_code=303)

@app.post("/toggle/{task_id}")
async def toggle_task(request: Request, task_id: str):
    username = await get_current_user(request)
    if not username:
        return RedirectResponse(url="/", status_code=303)

    task = await tasks_collection.find_one({"_id": ObjectId(task_id), "owner": username})
    if task:
        new_status = not task.get("done", False)
        await tasks_collection.update_one(
            {"_id": ObjectId(task_id)},
            {"$set": {"done": new_status}}
        )
    return RedirectResponse(url="/", status_code=303)

# ------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
