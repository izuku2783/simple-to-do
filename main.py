from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import uvicorn, os
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId

app = FastAPI()

# ✅ Connect to MongoDB Atlas
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
client = AsyncIOMotorClient(MONGO_URI)
db = client.todo_db
collection = db.tasks

# HTML Renderer
def render_html(tasks):
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>To-Do List</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; }
            h1 { color: #333; }
            ul { list-style-type: none; padding: 0; }
            li { margin: 8px 0; }
            form { display: inline; }
            input[type="text"] { padding: 6px; }
            button { padding: 6px 10px; margin-left: 5px; }
            .done { text-decoration: line-through; color: gray; }
        </style>
    </head>
    <body>
        <h1>My To-Do List</h1>
        <form action="/add" method="post">
            <input type="text" name="task" placeholder="Enter a task" required>
            <button type="submit">Add</button>
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


@app.get("/", response_class=HTMLResponse)
async def home():
    tasks_cursor = collection.find({})
    tasks = []
    async for task in tasks_cursor:
        tasks.append({
            "id": str(task["_id"]),
            "text": task["text"],
            "done": task.get("done", False)
        })
    return render_html(tasks)


@app.post("/add")
async def add_task(task: str = Form(...)):
    await collection.insert_one({"text": task, "done": False})
    return RedirectResponse(url="/", status_code=303)


@app.post("/delete/{task_id}")
async def delete_task(task_id: str):
    await collection.delete_one({"_id": ObjectId(task_id)})
    return RedirectResponse(url="/", status_code=303)


@app.post("/toggle/{task_id}")
async def toggle_task(task_id: str):
    task = await collection.find_one({"_id": ObjectId(task_id)})
    if task:
        new_status = not task.get("done", False)
        await collection.update_one({"_id": ObjectId(task_id)}, {"$set": {"done": new_status}})
    return RedirectResponse(url="/", status_code=303)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
