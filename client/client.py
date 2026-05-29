import asyncio
import websockets
import json
import subprocess
import socket
import time
import os
import re
import psutil
import threading
import tkinter as tk

SERVER      = "ws://192.168.29.197:6001/ws"
CLIENT_NAME = socket.gethostname()

STEAM_EXE   = r"C:\Program Files (x86)\Steam\steam.exe"
AC_APP_ID   = "244210"
AC_ROOT     = r"C:\Program Files (x86)\Steam\steamapps\common\assettocorsa"

AC_DOCS     = os.path.join(os.path.expanduser("~"), "Documents", "Assetto Corsa")
CFG_DIR     = os.path.join(AC_DOCS, "cfg")
RACE_INI    = os.path.join(CFG_DIR, "race.ini")
ASSISTS_INI = os.path.join(CFG_DIR, "assists.ini")
RACE_OUT    = os.path.join(AC_DOCS, "out", "race_out.json")

CARS_DIR    = os.path.join(AC_ROOT, "content", "cars")
TRACKS_DIR  = os.path.join(AC_ROOT, "content", "tracks")

game_process    = None
start_time      = None
auto_stop_at    = None
last_preset     = {}          # holds the last launched preset for session-end reporting
last_race_out_mtime = None    # mtime of race_out.json at launch time


# SCANNING

def _read_ui_json(path):
    """Robustly read AC ui_*.json - handles BOM and trailing commas."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8-sig", errors="ignore")   # strips BOM
        text = re.sub(r",\s*([}\]])", r"\1", text)          # remove trailing commas
        return json.loads(text)
    except Exception:
        return None


def scan_cars():
    cars = []
    if not os.path.isdir(CARS_DIR):
        return cars
    for folder in os.listdir(CARS_DIR):
        full = os.path.join(CARS_DIR, folder)
        if not os.path.isdir(full) or folder.startswith("__") or folder.startswith("."):
            continue
        name = folder
        ui_path = os.path.join(full, "ui", "ui_car.json")
        if os.path.isfile(ui_path):
            data = _read_ui_json(ui_path)
            if data and data.get("name"):
                name = str(data["name"]).strip()
        cars.append({"id": folder, "name": name})
    cars.sort(key=lambda c: c["name"].lower())
    return cars


def scan_tracks():
    tracks = []
    if not os.path.isdir(TRACKS_DIR):
        return tracks
    for folder in os.listdir(TRACKS_DIR):
        full = os.path.join(TRACKS_DIR, folder)
        if not os.path.isdir(full) or folder.startswith("__") or folder.startswith("."):
            continue

        # FIRST: check for layout subfolders (each with their own ui/ui_track.json).
        # This must take priority over a top-level ui_track.json because AC requires
        # CONFIG_TRACK to be set to the exact layout folder name. If we used the
        # top-level file and set CONFIG_TRACK= (blank) for a track that actually has
        # layout subfolders, AC throws "track mod damaged or layout changed".
        layout_entries = []
        try:
            for layout in sorted(os.listdir(full)):
                layout_full = os.path.join(full, layout)
                if not os.path.isdir(layout_full):
                    continue
                layout_ui = os.path.join(layout_full, "ui", "ui_track.json")
                if os.path.isfile(layout_ui):
                    data = _read_ui_json(layout_ui)
                    name = str(data["name"]).strip() if (data and data.get("name")) else (folder + " - " + layout)
                    if layout.lower() not in name.lower():
                        name = name + " (" + layout + ")"
                    layout_entries.append({"id": folder, "layout": layout, "name": name})
        except Exception:
            pass

        if layout_entries:
            tracks.extend(layout_entries)
            continue

        # FALLBACK: no layout subfolders found — use top-level ui/ui_track.json
        # (CONFIG_TRACK will be blank, which is correct for single-layout tracks)
        ui_path = os.path.join(full, "ui", "ui_track.json")
        if os.path.isfile(ui_path):
            data = _read_ui_json(ui_path)
            name = str(data["name"]).strip() if (data and data.get("name")) else folder
            tracks.append({"id": folder, "layout": "", "name": name})
            continue

        # Last resort: list the folder if it has real AC content
        try:
            has_content = os.path.isdir(os.path.join(full, "data")) or \
                any(fn.endswith(".kn5") for fn in os.listdir(full))
            if has_content:
                tracks.append({"id": folder, "layout": "", "name": folder})
        except Exception:
            pass

    tracks.sort(key=lambda t: t["name"].lower())
    return tracks


# INI WRITERS

SESSION_NUM = {
    "Practice":    0,
    "Qualifying":  1,
    "Race":        2,
    "Hot Lap":     3,
    "Time Attack": 4,
    "Drift":       5,
    "Drag":        6,
}

ASSIST_VAL = {"off": 0, "factory": 1, "on": 2}


def write_race_ini(preset):
    car_id        = preset.get("car_id", "")
    track_id      = preset.get("track_id", "")
    layout        = preset.get("track_layout", "") or ""
    session_label = preset.get("session_type", "Practice")
    duration      = int(preset.get("session_minutes", 0))
    driver        = preset.get("driver", CLIENT_NAME)
    assist        = preset.get("assist", {}) or {}

    session_type = SESSION_NUM.get(session_label, 0)
    damage       = int(assist.get("damage", 100))
    fuel_rate    = float(assist.get("fuel_rate", 1.0))
    tyre_wear    = float(assist.get("tyre_wear", 1.0))
    blankets     = "1" if assist.get("tyre_blankets", True) else "0"

    is_lap_based = session_label == "Race"
    is_hotlap    = session_label in ("Hot Lap", "Time Attack")
    is_drift     = session_label == "Drift"

    lines = []
    lines.append("[HEADER]")
    lines.append("VERSION=2")
    lines.append("")

    lines.append("[RACE]")
    lines.append("MODEL=" + car_id)
    lines.append("MODEL_CONFIG=")
    lines.append("SKIN=")
    lines.append("TRACK=" + track_id)
    lines.append("CONFIG_TRACK=" + layout)
    lines.append("AI_LEVEL=95")
    lines.append("CARS=1")
    lines.append("DRIFT_MODE=" + ("1" if is_drift else "0"))
    lines.append("FIXED_SETUP=0")
    lines.append("PENALTIES=1")
    lines.append("JUMP_START_PENALTY=0")
    lines.append("RACE_LAPS=" + ("5" if is_lap_based else "0"))
    lines.append("DAMAGE_MULTIPLIER=" + str(damage))
    lines.append("FUEL_RATE=" + str(int(fuel_rate * 100)))
    lines.append("TYRE_WEAR_RATE=" + str(int(tyre_wear * 100)))
    lines.append("TYRE_BLANKETS=" + blankets)
    lines.append("ALLOWED_TYRES_OUT=4")
    lines.append("")

    lines.append("[REMOTE]")
    lines.append("ACTIVE=0")
    lines.append("SERVER_IP=")
    lines.append("SERVER_PORT=0")
    lines.append("NAME=")
    lines.append("TEAM=")
    lines.append("GUID=")
    lines.append("REQUESTED_CAR=")
    lines.append("")

    lines.append("[CAR_0]")
    lines.append("MODEL=" + car_id)
    lines.append("MODEL_CONFIG=")
    lines.append("SETUP=")
    lines.append("SKIN=")
    lines.append("DRIVER_NAME=" + driver)
    lines.append("NATION_CODE=")
    lines.append("NATIONALITY=")
    lines.append("AI_LEVEL=95")
    lines.append("AI_AGGRESSION=0")
    lines.append("")

    lines.append("[SESSION_0]")
    lines.append("NAME=" + session_label)
    lines.append("TYPE=" + str(session_type))
    if is_lap_based:
        lines.append("LAPS=5")
        lines.append("DURATION_MINUTES=" + str(duration))
        lines.append("SPAWN_SET=PIT")
        lines.append("STARTING_POSITION=1")
    elif is_hotlap:
        lines.append("LAPS=0")
        lines.append("DURATION_MINUTES=" + str(duration))
        lines.append("SPAWN_SET=HOTLAP_START")
        lines.append("STARTING_POSITION=0")
    else:
        lines.append("LAPS=0")
        lines.append("DURATION_MINUTES=" + str(duration))
        lines.append("SPAWN_SET=START")
        lines.append("STARTING_POSITION=0")
    lines.append("")

    lines.append("[TEMPERATURE]")
    lines.append("AMBIENT=26")
    lines.append("ROAD=32")
    lines.append("")

    lines.append("[LIGHTING]")
    lines.append("SUN_ANGLE=0")
    lines.append("TIME_MULT=1")
    lines.append("CLOUD_SPEED=0.2")
    lines.append("")

    lines.append("[WEATHER]")
    lines.append("NAME=3_clear")
    lines.append("")

    lines.append("[DYNAMIC_TRACK]")
    lines.append("PRESET=0")
    lines.append("")

    lines.append("[GHOST_CAR]")
    lines.append("RECORDING=" + ("1" if is_hotlap else "0"))
    lines.append("PLAYING=" + ("1" if is_hotlap else "0"))
    lines.append("SECONDS_ADVANTAGE=0")
    lines.append("LOAD=1")
    lines.append("FILE=")
    lines.append("ENABLED=" + ("1" if is_hotlap else "0"))
    lines.append("")

    lines.append("[REPLAY]")
    lines.append("FILENAME=")
    lines.append("ACTIVE=0")
    lines.append("")

    lines.append("[BENCHMARK]")
    lines.append("ACTIVE=0")
    lines.append("")

    lines.append("[OPTIONS]")
    lines.append("USE_MPH=0")
    lines.append("")

    os.makedirs(CFG_DIR, exist_ok=True)
    with open(RACE_INI, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines))

    print("race.ini written: car=" + repr(car_id) + " track=" + repr(track_id) +
          " layout=" + repr(layout) + " session=" + session_label +
          "(TYPE=" + str(session_type) + ") laps=" + ("5" if is_lap_based else "0") +
          " duration=" + str(duration) + "min damage=" + str(damage) + "%")


def write_assists_ini(assist):
    abs_v       = ASSIST_VAL.get(assist.get("abs", "factory"), 1)
    tc_v        = ASSIST_VAL.get(assist.get("traction_control", "factory"), 1)
    stability   = int(assist.get("stability_control", 0))
    ideal_line  = "1" if assist.get("ideal_line", False)   else "0"
    auto_shift  = "1" if assist.get("auto_shifter", False) else "0"
    auto_clutch = "1" if assist.get("auto_clutch", False)  else "0"
    blankets    = "1" if assist.get("tyre_blankets", True) else "0"
    damage      = int(assist.get("damage", 100))
    fuel_rate   = float(assist.get("fuel_rate", 1.0))
    tyre_wear   = float(assist.get("tyre_wear", 1.0))

    lines = []
    lines.append("[ASSISTS]")
    lines.append("ABS=" + str(abs_v))
    lines.append("TRACTION_CONTROL=" + str(tc_v))
    lines.append("STABILITY_CONTROL=" + str(stability))
    lines.append("AUTO_BLIP=0")
    lines.append("IDEAL_LINE=" + ideal_line)
    lines.append("AUTO_CLUTCH=" + auto_clutch)
    lines.append("AUTO_SHIFTER=" + auto_shift)
    lines.append("AUTOMATIC_GEARBOX=0")
    lines.append("GEARBOX=" + auto_shift)
    lines.append("TYRE_BLANKETS=" + blankets)
    lines.append("DAMAGE_MULTIPLIER=" + str(damage))
    lines.append("FUEL_RATE=" + str(int(fuel_rate * 100)))
    lines.append("TYRE_RATE=" + str(int(tyre_wear * 100)))
    lines.append("")

    os.makedirs(CFG_DIR, exist_ok=True)
    with open(ASSISTS_INI, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines))

    print("assists.ini written: abs=" + str(abs_v) + " tc=" + str(tc_v) +
          " stability=" + str(stability) + "% line=" + ideal_line +
          " shifter=" + auto_shift + " clutch=" + auto_clutch)


# PROCESS CONTROL

def is_ac_running():
    global game_process
    if game_process is None:
        return False
    if game_process.poll() is not None:
        game_process = None
        return False
    for proc in psutil.process_iter(["name"]):
        if proc.info["name"] and "acs" in proc.info["name"].lower():
            return True
    return False


def launch_game(preset):
    global game_process, start_time, auto_stop_at, last_preset, last_race_out_mtime

    if is_ac_running():
        print("AC already running - ignoring launch")
        return

    write_race_ini(preset)
    write_assists_ini(preset.get("assist", {}) or {})

    minutes   = int(preset.get("session_minutes", 0))
    auto_stop = preset.get("auto_stop", False)
    auto_stop_at = (time.time() + minutes * 60) if (auto_stop and minutes > 0) else None

    acs_path = os.path.join(AC_ROOT, "acs.exe")
    if not os.path.isfile(acs_path):
        print("ERROR: acs.exe not found at " + acs_path)
        return

    try:
        last_preset = dict(preset)
        # Record mtime of race_out.json so we can detect a new file after the session
        try:
            last_race_out_mtime = os.path.getmtime(RACE_OUT) if os.path.isfile(RACE_OUT) else None
        except Exception:
            last_race_out_mtime = None

        game_process = subprocess.Popen([acs_path], cwd=AC_ROOT)
        start_time = time.time()
        print("AC launched - auto_stop=" + ("yes, " + str(minutes) + "min" if auto_stop_at else "no"))
    except Exception as e:
        print("Launch failed: " + str(e))


def stop_game():
    global game_process, start_time, auto_stop_at
    auto_stop_at = None
    start_time   = None

    killed = False
    for proc in psutil.process_iter(["name", "pid"]):
        if proc.info["name"] and "acs" in proc.info["name"].lower():
            try:
                proc.kill()
                killed = True
                print("Killed " + proc.info["name"] + " (pid " + str(proc.info["pid"]) + ")")
            except Exception as e:
                print("Kill failed: " + str(e))

    if game_process:
        try:
            game_process.terminate()
        except Exception:
            pass
        game_process = None

    if not killed:
        print("No AC process found to stop")


def get_uptime():
    if start_time is None:
        return "stopped"
    elapsed = int(time.time() - start_time)
    h = elapsed // 3600
    m = (elapsed % 3600) // 60
    s = elapsed % 60
    return "%02d:%02d:%02d" % (h, m, s)


def get_cpu():
    try:
        return round(psutil.cpu_percent(interval=None))
    except Exception:
        return 0


def get_ram():
    try:
        return round(psutil.virtual_memory().used / (1024 ** 3), 1)
    except Exception:
        return 0


# WEBSOCKET LOOPS

def parse_race_out() -> dict:
    """Parse AC's race_out.json and return a summary dict (best times, valid/invalid counts)."""
    result = {
        "valid_laps":   0,
        "invalid_laps": 0,
        "total_laps":   0,
        "best_valid_ms":   None,
        "best_invalid_ms": None,
        "laps": [],
    }
    try:
        if not os.path.isfile(RACE_OUT):
            return result
        with open(RACE_OUT, "r", encoding="utf-8-sig", errors="ignore") as f:
            data = json.load(f)
        # AC race_out.json: top-level "Cars" list, each with "BestLap", "Laps" list
        cars = data.get("Cars", [])
        if not cars:
            return result
        car = cars[0]  # single-player: only one car
        laps = car.get("Laps", [])
        for lap in laps:
            ms        = lap.get("LapTime", 0)
            has_cuts  = lap.get("Cuts", 0) > 0
            in_pit    = lap.get("InPit", False)
            valid     = (not has_cuts) and (not in_pit) and ms > 0
            result["laps"].append({"ms": ms, "valid": valid, "cuts": lap.get("Cuts", 0)})
            if valid:
                result["valid_laps"] += 1
                if result["best_valid_ms"] is None or ms < result["best_valid_ms"]:
                    result["best_valid_ms"] = ms
            else:
                result["invalid_laps"] += 1
                if ms > 0 and (result["best_invalid_ms"] is None or ms < result["best_invalid_ms"]):
                    result["best_invalid_ms"] = ms
        result["total_laps"] = len(laps)
    except Exception as e:
        print("parse_race_out error: " + str(e))
    return result


def ms_to_laptime(ms) -> str:
    """Convert milliseconds to m:ss.mmm string."""
    if ms is None or ms <= 0:
        return "--:--.---"
    ms = int(ms)
    minutes = ms // 60000
    seconds = (ms % 60000) // 1000
    millis  = ms % 1000
    return f"{minutes}:{seconds:02d}.{millis:03d}"


def show_session_popup(data: dict):
    """Show a styled fullscreen-ish popup on the client PC for 10 seconds.
    Runs in its own thread so it doesn't block the asyncio loop.
    """
    try:
        root = tk.Tk()
        root.title("Session Over")
        root.configure(bg="#0d1b2a")
        root.resizable(False, False)
        root.attributes("-topmost", True)   # always on top
        root.attributes("-alpha", 0.97)

        # Center on screen
        W, H = 580, 480
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        root.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")

        # ── Close button (top-right X) ────────────────────────────────
        tk.Button(
            root, text="✕", command=root.destroy,
            bg="#1a2a3a", fg="#aaaaaa", relief="flat",
            font=("Segoe UI", 12, "bold"), bd=0,
            activebackground="#2a3a4a", activeforeground="#ffffff",
            cursor="hand2", padx=8, pady=2,
        ).place(x=W-44, y=10)

        # ── Flag + Title ──────────────────────────────────────────────
        tk.Label(root, text="🏁", bg="#0d1b2a", font=("Segoe UI", 32)).pack(pady=(28, 0))
        tk.Label(root, text="Session Over", bg="#0d1b2a", fg="#c8dcf8",
                 font=("Segoe UI", 26, "bold")).pack(pady=(2, 0))

        # ── Driver / Meta ─────────────────────────────────────────────
        driver = data.get("driver", "")
        tk.Label(root, text=driver, bg="#0d1b2a", fg="#ffffff",
                 font=("Segoe UI", 16, "bold")).pack(pady=(10, 0))

        meta_parts = [p for p in [
            data.get("track_name", ""),
            data.get("car_name",   ""),
            data.get("session_type", ""),
        ] if p]
        tk.Label(root, text="  ·  ".join(meta_parts), bg="#0d1b2a", fg="#7a8ba0",
                 font=("Segoe UI", 11)).pack(pady=(4, 10))

        # ── Divider ───────────────────────────────────────────────────
        tk.Frame(root, bg="#1e3a5f", height=1, width=480).pack(pady=(0, 14))

        total   = data.get("total_laps", 0)
        valid   = data.get("valid_laps", 0)
        invalid = data.get("invalid_laps", 0)
        v_pct   = data.get("valid_pct", 0)
        i_pct   = data.get("invalid_pct", 0)

        if total == 0:
            tk.Label(root, text="No laps recorded", bg="#0d1b2a", fg="#ff5252",
                     font=("Segoe UI", 20, "bold")).pack(pady=12)
        else:
            # ── Lap stat row ──────────────────────────────────────────
            row = tk.Frame(root, bg="#0d1b2a")
            row.pack(fill="x", padx=50, pady=(0, 10))

            # Valid box
            vbox = tk.Frame(row, bg="#0a2a0a", bd=1, relief="solid")
            vbox.pack(side="left", expand=True, fill="both", padx=(0, 8))
            tk.Label(vbox, text="✔ VALID LAPS", bg="#0a2a0a", fg="#4caf50",
                     font=("Segoe UI", 9, "bold")).pack(pady=(10, 0))
            tk.Label(vbox, text=str(valid), bg="#0a2a0a", fg="#69f0ae",
                     font=("Segoe UI", 40, "bold")).pack()
            tk.Label(vbox, text=f"{v_pct}%", bg="#0a2a0a", fg="#4caf50",
                     font=("Segoe UI", 12, "bold")).pack(pady=(0, 10))

            # Invalid box
            ibox = tk.Frame(row, bg="#2a0a0a", bd=1, relief="solid")
            ibox.pack(side="left", expand=True, fill="both", padx=(8, 0))
            tk.Label(ibox, text="✘ INVALID LAPS", bg="#2a0a0a", fg="#ff5252",
                     font=("Segoe UI", 9, "bold")).pack(pady=(10, 0))
            tk.Label(ibox, text=str(invalid), bg="#2a0a0a", fg="#ff5252",
                     font=("Segoe UI", 40, "bold")).pack()
            tk.Label(ibox, text=f"{i_pct}%", bg="#2a0a0a", fg="#ff5252",
                     font=("Segoe UI", 12, "bold")).pack(pady=(0, 10))

            # ── Best lap times ────────────────────────────────────────
            tk.Frame(root, bg="#1e3a5f", height=1, width=480).pack(pady=(4, 10))
            trow = tk.Frame(root, bg="#0d1b2a")
            trow.pack(fill="x", padx=50)

            bv = data.get("best_valid",   "--:--.---")
            bi = data.get("best_invalid", "--:--.---")

            vt = tk.Frame(trow, bg="#0d2a1a")
            vt.pack(side="left", expand=True, fill="both", padx=(0, 6))
            tk.Label(vt, text="🏆 Best Valid", bg="#0d2a1a", fg="#7a8ba0",
                     font=("Segoe UI", 9, "bold")).pack(pady=(8, 0))
            tk.Label(vt, text=bv, bg="#0d2a1a", fg="#69f0ae",
                     font=("Courier New", 16, "bold")).pack(pady=(0, 8))

            it = tk.Frame(trow, bg="#2a0d0d")
            it.pack(side="left", expand=True, fill="both", padx=(6, 0))
            tk.Label(it, text="🚧 Best Invalid", bg="#2a0d0d", fg="#7a8ba0",
                     font=("Segoe UI", 9, "bold")).pack(pady=(8, 0))
            tk.Label(it, text=bi, bg="#2a0d0d", fg="#ff7070",
                     font=("Courier New", 16, "bold")).pack(pady=(0, 8))

        # ── Auto-close countdown ──────────────────────────────────────
        countdown_var = tk.StringVar(value="Closes in 10s")
        tk.Label(root, textvariable=countdown_var, bg="#0d1b2a", fg="#445566",
                 font=("Segoe UI", 10)).pack(pady=(12, 0))

        def _tick(secs=10):
            if secs <= 0:
                root.destroy()
                return
            countdown_var.set(f"Closes in {secs}s")
            root.after(1000, _tick, secs - 1)

        root.after(1000, _tick, 9)
        root.mainloop()
    except Exception as ex:
        print("Popup error: " + str(ex))


async def status_loop(ws):
    global auto_stop_at, last_preset, last_race_out_mtime
    was_running = False
    while True:
        try:
            running = is_ac_running()

            if auto_stop_at and time.time() >= auto_stop_at:
                print("Auto-stop triggered")
                stop_game()
                running = False

            time_remaining = None
            if auto_stop_at and running:
                time_remaining = max(0, int(auto_stop_at - time.time()))

            await ws.send(json.dumps({
                "status":         "running" if running else "idle",
                "game":           "AC",
                "uptime":         get_uptime(),
                "cpu":            get_cpu(),
                "ram":            get_ram(),
                "time_remaining": time_remaining,
            }))

            # Detect session end (running -> idle transition)
            if was_running and not running and last_preset:
                # Wait for AC to finish writing race_out.json (4 s is safer than 2 s)
                await asyncio.sleep(4)

                # Always parse – don't gate on mtime, as NTFS timestamps can have
                # low enough granularity to make new_mtime == last_race_out_mtime
                # even when the file was actually re-written this session.
                stats = {"valid_laps": 0, "invalid_laps": 0, "total_laps": 0,
                         "best_valid_ms": None, "best_invalid_ms": None}
                try:
                    if os.path.isfile(RACE_OUT):
                        stats = parse_race_out()
                        last_race_out_mtime = os.path.getmtime(RACE_OUT)
                        print(f"race_out parsed: total={stats['total_laps']} "
                              f"valid={stats['valid_laps']} invalid={stats['invalid_laps']}")
                    else:
                        print("race_out.json not found – no lap data available")
                except Exception as ex:
                    print("Failed to read race_out: " + str(ex))

                total = stats["valid_laps"] + stats["invalid_laps"]
                valid_pct   = round(stats["valid_laps"]   / total * 100) if total else 0
                invalid_pct = round(stats["invalid_laps"] / total * 100) if total else 0

                session_end_msg = {
                    "type":            "session_ended",
                    "driver":          last_preset.get("driver", CLIENT_NAME),
                    "car_name":        last_preset.get("car_name", last_preset.get("car_id", "")),
                    "track_name":      last_preset.get("track_name", last_preset.get("track_id", "")),
                    "session_type":    last_preset.get("session_type", "Practice"),
                    "session_minutes": last_preset.get("session_minutes", 0),
                    "valid_laps":      stats["valid_laps"],
                    "invalid_laps":    stats["invalid_laps"],
                    "total_laps":      stats["total_laps"],
                    "valid_pct":       valid_pct,
                    "invalid_pct":     invalid_pct,
                    "best_valid":      ms_to_laptime(stats["best_valid_ms"]),
                    "best_invalid":    ms_to_laptime(stats["best_invalid_ms"]),
                    "best_valid_ms":   stats["best_valid_ms"],
                    "best_invalid_ms": stats["best_invalid_ms"],
                }
                try:
                    await ws.send(json.dumps(session_end_msg))
                    print("Session-end event sent: " + str(session_end_msg))
                    # Show popup on the client PC in a background thread
                    popup_data = dict(session_end_msg)
                    t = threading.Thread(target=show_session_popup, args=(popup_data,), daemon=True)
                    t.start()
                except Exception as e:
                    print("Failed to send session_ended: " + str(e))

            was_running = running
        except Exception:
            break
        await asyncio.sleep(3)


async def receive_loop(ws):
    while True:
        try:
            raw = await ws.recv()
            msg = json.loads(raw)
            cmd = msg.get("cmd")
            if cmd == "launch":
                preset = msg.get("preset", {})
                # Carry over resolved human-readable names passed from server
                launch_game(preset)
            elif cmd == "stop":
                stop_game()
            else:
                print("Unknown command: " + str(cmd))
        except Exception as e:
            print("Receive error: " + str(e))
            break


async def connect():
    print("Connecting to " + SERVER + " as '" + CLIENT_NAME + "'...")
    cars   = scan_cars()
    tracks = scan_tracks()
    print("Found " + str(len(cars)) + " cars, " + str(len(tracks)) + " tracks")

    while True:
        try:
            async with websockets.connect(SERVER, ping_interval=20) as ws:
                print("Connected")
                await ws.send(json.dumps({
                    "name":   CLIENT_NAME,
                    "game":   "AC",
                    "cars":   cars,
                    "tracks": tracks,
                }))
                await asyncio.gather(status_loop(ws), receive_loop(ws))
        except Exception as e:
            print("Disconnected: " + str(e) + " - retrying in 5s")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(connect())