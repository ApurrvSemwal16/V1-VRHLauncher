from fastapi import FastAPI, WebSocket, Request
from fastapi.templating import Jinja2Templates

app = FastAPI()

templates = Jinja2Templates(
    directory="server/templates"
)

clients = {}
connections = {}

@app.get("/")
async def home(request: Request):

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "clients": clients.keys()
        }
    )

@app.post("/launch")
async def launch():

    print("LAUNCH SENT")

    for name, ws in connections.items():

        try:

            await ws.send_text(
                "launch"
            )

            print(
                "sent to",
                name
            )

        except:

            pass

    return {
        "ok":True
    }

@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket
):

    await websocket.accept()

    data = await websocket.receive_json()

    name = data["name"]

    clients[name] = True

    connections[name] = websocket

    print(
        name,
        "connected"
    )

    try:

        while True:

            await websocket.receive_text()

    except:

        print(
            name,
            "disconnected"
        )

        clients.pop(
            name,
            None
        )

        connections.pop(
            name,
            None
        )