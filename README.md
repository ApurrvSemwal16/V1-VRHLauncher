# VRH Launcher

## Structure

```
vrh-launcher/
├── server/
│   ├── app.py                  ← FastAPI server
│   └── templates/
│       └── index.html          ← Dashboard
├── client/
│   └── client.py               ← Client (compile to .exe)
└── requirements.txt
```

## Run server

```bash
pip install -r requirements.txt
uvicorn server.app:app --host 0.0.0.0 --port 6001 --reload
```

Open http://localhost:6001 in your browser.

## Run / build client

```bash
# Dev
pip install psutil websockets
python client/client.py

# Build .exe
pip install pyinstaller
pyinstaller --onefile client/client.py
```

Edit `CLIENT_NAME` and `SERVER` at the top of `client.py` before building for each PC.

## What's new vs V1

- Pod-style dashboard per client (matches screenshot)
- Per-pod: Driver, Mode, Session Type, Server, Car, Track, Session counter, Auto-Stop, Auto-Grid
- Start / Stop buttons per pod
- Launch All / Stop All in header
- Client pushes live status (idle / running / stopped) every 3s
- Client auto-reconnects if server drops
- Preset JSON sent to client on launch (ready to extend into AC cfg writing)
- `/api/clients` endpoint returns live state of all clients
