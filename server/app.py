from fastapi import FastAPI, WebSocket, Request
from fastapi.templating import Jinja2Templates

app = FastAPI()

templates = Jinja2Templates(
    directory="server/templates"
)

clients = []
connections = []

@app.get("/")
async def home(request: Request):

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "clients":clients
        }
    )

@app.post("/launch")
async def launch():

    for ws in connections:

        await ws.send_text(
            "launch"
        )

    return {
        "ok":True
    }

@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket
):

    await websocket.accept()

    connections.append(
        websocket
    )

    data = await websocket.receive_json()

    clients.append(
        data["name"]
    )

    while True:

        await websocket.receive_text()