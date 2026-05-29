from fastapi import FastAPI, WebSocket, Request, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional
import json
import os
import csv
import io
from datetime import datetime

app = FastAPI()
templates = Jinja2Templates(directory="server/templates")

# ── State ────────────────────────────────────────────────────────────────────
clients: dict[str, dict]          = {}
connections: dict[str, WebSocket] = {}
session_ended_events: dict[str, dict] = {}  # latest session-end event per client name

# Persistent assist profiles
DATA_DIR      = "server/data"
ASSISTS_FILE  = os.path.join(DATA_DIR, "assists.json")
DRIVERS_FILE  = os.path.join(DATA_DIR, "drivers.json")
REG_SETTINGS_FILE = os.path.join(DATA_DIR, "registration_settings.json")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
os.makedirs(DATA_DIR, exist_ok=True)


def default_assist(name: str) -> dict:
    return {
        "name":             name,
        "abs":              "factory",      # off | factory | on (Car specific = factory)
        "traction_control": "factory",      # off | factory | on
        "stability_control": 0,             # 0-100
        "damage":            100,           # 0-100
        "ideal_line":        False,
        "auto_shifter":      False,
        "fuel_rate":         1.0,           # multiplier
        "tyre_wear":         1.0,           # multiplier
        "auto_clutch":       False,
        "tyre_blankets":     True,
    }


def load_assists() -> list:
    if not os.path.isfile(ASSISTS_FILE):
        # seed with default profiles
        defaults = [
            {**default_assist("Gamer"),
             "abs": "on", "traction_control": "on", "stability_control": 50,
             "damage": 0, "ideal_line": True, "auto_shifter": True,
             "fuel_rate": 0.0, "tyre_wear": 0.0, "auto_clutch": True},

            {**default_assist("Simulation NO DAMAGE"),
             "abs": "factory", "traction_control": "factory", "stability_control": 0,
             "damage": 0, "ideal_line": False, "auto_shifter": False,
             "fuel_rate": 1.0, "tyre_wear": 1.0, "auto_clutch": False},

            {**default_assist("Simulation"),
             "abs": "factory", "traction_control": "factory", "stability_control": 0,
             "damage": 100, "ideal_line": False, "auto_shifter": False,
             "fuel_rate": 1.0, "tyre_wear": 1.0, "auto_clutch": False},
        ]
        save_assists(defaults)
        return defaults

    try:
        with open(ASSISTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_assists(profiles: list) -> None:
    with open(ASSISTS_FILE, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2)


def get_assist_by_name(name: str) -> Optional[dict]:
    for p in load_assists():
        if p["name"] == name:
            return p
    return None


# ── Driver persistence ───────────────────────────────────────────────────────

def load_drivers() -> list:
    if not os.path.isfile(DRIVERS_FILE):
        save_drivers([])
        return []
    try:
        with open(DRIVERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_drivers(drivers: list) -> None:
    with open(DRIVERS_FILE, "w", encoding="utf-8") as f:
        json.dump(drivers, f, indent=2)


def next_driver_id() -> int:
    drivers = load_drivers()
    if not drivers:
        return 1001
    return max(d.get("id", 1000) for d in drivers) + 1


# ── Session persistence ─────────────────────────────────────────────────────

def load_sessions() -> list:
    if not os.path.isfile(SESSIONS_FILE):
        save_sessions([])
        return []
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_sessions(sessions: list) -> None:
    with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(sessions, f, indent=2)


def next_session_id() -> int:
    sessions = load_sessions()
    if not sessions:
        return 1
    return max(s.get("id", 0) for s in sessions) + 1


def log_session(driver_name: str, driver_id: Optional[int], car_id: str, car_name: str,
               track_id: str, track_layout: str, track_name: str,
               session_type: str, session_minutes: int) -> None:
    sessions = load_sessions()
    sessions.append({
        "id":             next_session_id(),
        "driver_id":      driver_id,
        "driver_name":    driver_name,
        "car_id":         car_id,
        "car_name":       car_name,
        "track_id":       track_id,
        "track_layout":   track_layout,
        "track_name":     track_name,
        "session_type":   session_type,
        "session_minutes": session_minutes,
        "created":        datetime.now().strftime("%m/%d/%Y %I:%M:%S %p"),
    })
    save_sessions(sessions)


def compute_dpi(driver_id: int) -> dict:
    """Aggregate session stats for a driver into a DPI summary."""
    sessions = [s for s in load_sessions() if s.get("driver_id") == driver_id]
    if not sessions:
        return {"dpi": 0, "session_count": 0, "tracks": [], "cars": []}

    # Per-track aggregation
    track_map: dict = {}
    for s in sessions:
        key = s["track_id"] + ("__" + s["track_layout"] if s.get("track_layout") else "")
        if key not in track_map:
            track_map[key] = {
                "track_id":     s["track_id"],
                "track_layout": s.get("track_layout", ""),
                "track_name":   s.get("track_name", s["track_id"]),
                "sessions":     0,
                "total_minutes": 0,
                "cars":         {},
            }
        t = track_map[key]
        t["sessions"] += 1
        t["total_minutes"] += s.get("session_minutes", 0)
        c_key = s["car_id"]
        if c_key not in t["cars"]:
            t["cars"][c_key] = {"car_id": c_key, "car_name": s.get("car_name", c_key), "sessions": 0, "minutes": 0}
        t["cars"][c_key]["sessions"] += 1
        t["cars"][c_key]["minutes"]  += s.get("session_minutes", 0)

    # Per-car aggregation
    car_map: dict = {}
    for s in sessions:
        c_key = s["car_id"]
        if c_key not in car_map:
            car_map[c_key] = {
                "car_id":   c_key,
                "car_name": s.get("car_name", c_key),
                "sessions": 0,
                "total_minutes": 0,
                "tracks":   {},
            }
        c = car_map[c_key]
        c["sessions"]      += 1
        c["total_minutes"] += s.get("session_minutes", 0)
        t_key = s["track_id"]
        if t_key not in c["tracks"]:
            c["tracks"][t_key] = {"track_name": s.get("track_name", t_key), "sessions": 0}
        c["tracks"][t_key]["sessions"] += 1

    track_list = sorted(track_map.values(), key=lambda x: x["sessions"], reverse=True)
    for t in track_list:
        t["cars"] = sorted(t["cars"].values(), key=lambda x: x["sessions"], reverse=True)

    car_list = sorted(car_map.values(), key=lambda x: x["sessions"], reverse=True)
    for c in car_list:
        c["tracks"] = sorted(c["tracks"].values(), key=lambda x: x["sessions"], reverse=True)

    # DPI = sessions * 10 + total_minutes * 0.5
    total_minutes = sum(s.get("session_minutes", 0) for s in sessions)
    dpi = int(len(sessions) * 10 + total_minutes * 0.5)

    return {
        "dpi":           dpi,
        "session_count": len(sessions),
        "total_minutes": total_minutes,
        "tracks":        track_list,
        "cars":          car_list,
        "first_session": sessions[0]["created"] if sessions else None,
        "last_session":  sessions[-1]["created"] if sessions else None,
    }


# ── Registration settings persistence ────────────────────────────────────────

def default_reg_settings() -> dict:
    return {
        "title": "Driver Sign-In",
        "name_placeholder": "First and Lastname",
        "birthday_placeholder": "Birthday",
        "email_placeholder": "Email",
        "phone_placeholder": "Phone",
        "show_phone": True,
        "text_block": "<b>VRH IS NOT LIABLE FOR ANY INJURY CASUED DUE TO INCORRECT USAGE OF THE SIMULATOR </b>",
        "accept_text": "Accept",
        "send_button": "Register",
        "clear_button": "Clear",
    }


def load_reg_settings() -> dict:
    if not os.path.isfile(REG_SETTINGS_FILE):
        settings = default_reg_settings()
        save_reg_settings(settings)
        return settings
    try:
        with open(REG_SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default_reg_settings()


def save_reg_settings(settings: dict) -> None:
    with open(REG_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


# ── Models ───────────────────────────────────────────────────────────────────

class AssistProfile(BaseModel):
    name:              str
    abs:               str   = "factory"
    traction_control:  str   = "factory"
    stability_control: int   = 0
    damage:            int   = 100
    ideal_line:        bool  = False
    auto_shifter:      bool  = False
    fuel_rate:         float = 1.0
    tyre_wear:         float = 1.0
    auto_clutch:       bool  = False
    tyre_blankets:     bool  = True


class PresetPayload(BaseModel):
    target:          str
    driver:          str  = ""
    mode:            str  = "Simulation NO DAMAGE"  # assist profile name
    session_type:    str  = "Practice"
    server:          str  = "None"
    car_id:          str  = ""
    track_id:        str  = ""
    track_layout:    str  = ""
    session_minutes: int  = 0
    auto_stop:       bool = False
    auto_grid:       bool = True


class StopPayload(BaseModel):
    target: str


class DriverCreate(BaseModel):
    name:     str
    birthday: str = ""
    email:    str = ""
    phone:    str = ""
    signed:   bool = True
    vip:      bool = False


class DriverUpdate(BaseModel):
    name:     str
    birthday: str = ""
    email:    str = ""
    phone:    str = ""
    signed:   bool = True
    vip:      bool = False


class RegSettingsPayload(BaseModel):
    title:                str = "Driver Sign-In"
    name_placeholder:     str = "First and Lastname"
    birthday_placeholder: str = "Birthday"
    email_placeholder:    str = "Email"
    phone_placeholder:    str = "Phone"
    show_phone:           bool = True
    text_block:           str = ""
    accept_text:          str = "Accept"
    send_button:          str = "Register"
    clear_button:         str = "Clear"


# ── Pages ────────────────────────────────────────────────────────────────────

@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse(
        request=request, name="index.html", context={}
    )


@app.get("/assists")
async def assists_page(request: Request):
    return templates.TemplateResponse(
        request=request, name="assists.html", context={}
    )


@app.get("/drivers")
async def drivers_page(request: Request):
    return templates.TemplateResponse(
        request=request, name="drivers.html", context={}
    )


@app.get("/registration-settings")
async def registration_settings_page(request: Request):
    return templates.TemplateResponse(
        request=request, name="registration_settings.html", context={}
    )


@app.get("/register")
async def register_page(request: Request):
    return templates.TemplateResponse(
        request=request, name="register.html", context={}
    )


@app.get("/dpi/{driver_id}")
async def dpi_page(request: Request, driver_id: int):
    return templates.TemplateResponse(
        request=request, name="dpi.html", context={}
    )


# ── REST API ─────────────────────────────────────────────────────────────────

@app.get("/api/clients")
async def get_clients():
    return clients


@app.get("/api/assists")
async def get_assists():
    return load_assists()


@app.post("/api/assists")
async def create_assist(profile: AssistProfile):
    profiles = load_assists()
    if any(p["name"] == profile.name for p in profiles):
        raise HTTPException(400, "Profile name already exists")
    profiles.append(profile.model_dump())
    save_assists(profiles)
    return {"ok": True, "profile": profile.model_dump()}


@app.put("/api/assists/{old_name}")
async def update_assist(old_name: str, profile: AssistProfile):
    profiles = load_assists()
    found = False
    for i, p in enumerate(profiles):
        if p["name"] == old_name:
            profiles[i] = profile.model_dump()
            found = True
            break
    if not found:
        raise HTTPException(404, "Profile not found")
    save_assists(profiles)
    return {"ok": True, "profile": profile.model_dump()}


@app.delete("/api/assists/{name}")
async def delete_assist(name: str):
    profiles = load_assists()
    new_profiles = [p for p in profiles if p["name"] != name]
    if len(new_profiles) == len(profiles):
        raise HTTPException(404, "Profile not found")
    if len(new_profiles) == 0:
        raise HTTPException(400, "Cannot delete the last profile")
    save_assists(new_profiles)
    return {"ok": True}



# ── Driver API ───────────────────────────────────────────────────────────────

@app.get("/api/drivers")
async def get_drivers():
    return load_drivers()


@app.get("/api/drivers/suggest")
async def suggest_drivers(q: str = ""):
    """Return drivers whose name starts with q, newest-registered first."""
    drivers = load_drivers()
    q_low = q.strip().lower()
    if not q_low:
        return []
    matches = [d for d in drivers if d.get("name", "").lower().startswith(q_low)]
    # Sort by id descending (most recently registered first)
    matches.sort(key=lambda d: d.get("id", 0), reverse=True)
    return [{"id": d["id"], "name": d["name"], "created": d.get("created", "")} for d in matches[:10]]


@app.get("/api/drivers/{driver_id}/dpi")
async def get_driver_dpi(driver_id: int):
    drivers = load_drivers()
    driver = next((d for d in drivers if d.get("id") == driver_id), None)
    if not driver:
        raise HTTPException(404, "Driver not found")
    stats = compute_dpi(driver_id)
    return {"driver": driver, **stats}


@app.post("/api/drivers")
async def create_driver(driver: DriverCreate):
    drivers = load_drivers()
    new_driver = {
        "id": next_driver_id(),
        "name": driver.name,
        "birthday": driver.birthday,
        "email": driver.email,
        "phone": driver.phone,
        "signed": driver.signed,
        "vip": driver.vip,
        "created": datetime.now().strftime("%m/%d/%Y %I:%M:%S %p"),
    }
    drivers.append(new_driver)
    save_drivers(drivers)
    return {"ok": True, "driver": new_driver}


@app.put("/api/drivers/{driver_id}")
async def update_driver(driver_id: int, driver: DriverUpdate):
    drivers = load_drivers()
    found = False
    for i, d in enumerate(drivers):
        if d.get("id") == driver_id:
            drivers[i].update({
                "name": driver.name,
                "birthday": driver.birthday,
                "email": driver.email,
                "phone": driver.phone,
                "signed": driver.signed,
                "vip": driver.vip,
            })
            found = True
            break
    if not found:
        raise HTTPException(404, "Driver not found")
    save_drivers(drivers)
    return {"ok": True}


@app.delete("/api/drivers/{driver_id}")
async def delete_driver(driver_id: int):
    drivers = load_drivers()
    new_drivers = [d for d in drivers if d.get("id") != driver_id]
    if len(new_drivers) == len(drivers):
        raise HTTPException(404, "Driver not found")
    save_drivers(new_drivers)
    return {"ok": True}


@app.get("/api/drivers/export/csv")
async def export_drivers_csv():
    drivers = load_drivers()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Id", "Name", "Birthday", "Email", "Phone", "Signed", "VIP", "Created"])
    for d in drivers:
        writer.writerow([
            d.get("id", ""), d.get("name", ""), d.get("birthday", ""),
            d.get("email", ""), d.get("phone", ""),
            "Yes" if d.get("signed") else "No",
            "Yes" if d.get("vip") else "No",
            d.get("created", ""),
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=drivers.csv"}
    )


@app.get("/api/drivers/export/json")
async def export_drivers_json():
    drivers = load_drivers()
    content = json.dumps(drivers, indent=2)
    return StreamingResponse(
        iter([content]),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=drivers.json"}
    )


# ── Registration Settings API ────────────────────────────────────────────────

@app.get("/api/registration-settings")
async def get_reg_settings():
    return load_reg_settings()


@app.put("/api/registration-settings")
async def update_reg_settings(payload: RegSettingsPayload):
    settings = payload.model_dump()
    save_reg_settings(settings)
    return {"ok": True, "settings": settings}


@app.post("/api/launch")
async def launch(payload: PresetPayload):
    driver_name = payload.driver.strip()
    if not driver_name:
        raise HTTPException(400, "Driver name is required")

    # Look up or auto-create driver record
    drivers = load_drivers()
    matched = next((d for d in drivers if d["name"].lower() == driver_name.lower()), None)
    if matched is None:
        # Auto-create a minimal profile
        new_id = next_driver_id()
        matched = {
            "id":      new_id,
            "name":    driver_name,
            "birthday": "",
            "email":   "",
            "phone":   "",
            "signed":  False,
            "vip":     False,
            "created": datetime.now().strftime("%m/%d/%Y %I:%M:%S %p"),
        }
        drivers.append(matched)
        save_drivers(drivers)

    driver_id = matched["id"]

    # Resolve human-readable car/track names from connected client
    car_name   = payload.car_id
    track_name = payload.track_id
    targets    = list(connections.keys()) if payload.target == "ALL" else [payload.target]
    for t in targets:
        cl = clients.get(t, {})
        c = next((x for x in cl.get("cars", []) if x["id"] == payload.car_id), None)
        if c:
            car_name = c["name"]
        tr_key = payload.track_id + ("__" + payload.track_layout if payload.track_layout else "")
        trk = next((x for x in cl.get("tracks", []) if
                    x["id"] == payload.track_id and (x.get("layout") or "") == (payload.track_layout or "")), None)
        if trk:
            track_name = trk["name"]
        break

    assist = get_assist_by_name(payload.mode) or default_assist(payload.mode)

    msg = json.dumps({
        "cmd": "launch",
        "preset": {
            "driver":          driver_name,
            "mode":            payload.mode,
            "session_type":    payload.session_type,
            "server":          payload.server,
            "car_id":          payload.car_id,
            "car_name":        car_name,
            "track_id":        payload.track_id,
            "track_layout":    payload.track_layout,
            "track_name":      track_name,
            "session_minutes": payload.session_minutes,
            "auto_stop":       payload.auto_stop,
            "auto_grid":       payload.auto_grid,
            "assist":          assist,
        }
    })

    sent = []
    for name in targets:
        ws = connections.get(name)
        if ws:
            try:
                await ws.send_text(msg)
                clients[name]["status"] = "launching"
                sent.append(name)
            except Exception:
                pass

    # Log one session entry per target sent
    for _ in sent:
        log_session(
            driver_name=driver_name,
            driver_id=driver_id,
            car_id=payload.car_id,
            car_name=car_name,
            track_id=payload.track_id,
            track_layout=payload.track_layout or "",
            track_name=track_name,
            session_type=payload.session_type,
            session_minutes=payload.session_minutes,
        )

    return {"ok": True, "sent_to": sent, "driver_id": driver_id}


@app.post("/api/stop")
async def stop(payload: StopPayload):
    msg = json.dumps({"cmd": "stop"})
    targets = list(connections.keys()) if payload.target == "ALL" else [payload.target]
    sent = []
    for name in targets:
        ws = connections.get(name)
        if ws:
            try:
                await ws.send_text(msg)
                clients[name]["status"] = "stopping"
                sent.append(name)
            except Exception:
                pass
    return {"ok": True, "sent_to": sent}


# ── Session-Ended API ─────────────────────────────────────────────────────────

@app.get("/api/session-ended")
async def get_session_ended():
    """Return all pending session-ended events (one per client). Consumed by the dashboard popup."""
    return session_ended_events


@app.delete("/api/session-ended/{client_name}")
async def clear_session_ended(client_name: str):
    """Mark a session-ended event as consumed (dismissed by the operator)."""
    session_ended_events.pop(client_name, None)
    return {"ok": True}


# ── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    data = await websocket.receive_json()
    name = data.get("name", "unknown")

    clients[name] = {
        "name":           name,
        "status":         "idle",
        "game":           data.get("game", "AC"),
        "uptime":         "stopped",
        "cpu":            0,
        "ram":            0,
        "time_remaining": None,
        "cars":           data.get("cars", []),
        "tracks":         data.get("tracks", []),
    }
    connections[name] = websocket
    print(f"{name} connected — {len(clients[name]['cars'])} cars, {len(clients[name]['tracks'])} tracks")

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                update = json.loads(raw)
                # Handle special session_ended event from client
                if update.get("type") == "session_ended":
                    update["client_name"] = name
                    session_ended_events[name] = update
                    print(f"{name} session ended: {update}")
                    continue
                cars   = clients[name].get("cars", [])
                tracks = clients[name].get("tracks", [])
                clients[name].update(update)
                if not clients[name].get("cars"):
                    clients[name]["cars"]   = cars
                if not clients[name].get("tracks"):
                    clients[name]["tracks"] = tracks
            except Exception:
                pass
    except Exception:
        print(f"{name} disconnected")
        clients.pop(name, None)
        connections.pop(name, None)