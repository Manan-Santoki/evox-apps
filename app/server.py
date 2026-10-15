"""Dashboard and scheduler for the app pipeline.

Anyone can view the dashboard and the read-only API. Approving, rejecting,
starting a check, downloading a pending APK and reading logs need the admin
login (HTTP basic auth, ADMIN_USER / ADMIN_PASSWORD).
"""

import collections
import logging
import os
import secrets
import sys
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import pipeline

log = logging.getLogger("evox-apps")

INTERVAL = int(float(os.environ.get("CHECK_INTERVAL_HOURS", "6")) * 3600)
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
DASHBOARD = Path(__file__).with_name("dashboard.html")


class RingHandler(logging.Handler):
    def __init__(self, size=600):
        super().__init__()
        self.lines = collections.deque(maxlen=size)

    def emit(self, record):
        msg = self.format(record)
        if "region localizations" not in msg:
            self.lines.append(msg)


ring = RingHandler()
fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
ring.setFormatter(fmt)
stdout = logging.StreamHandler(sys.stdout)
stdout.setFormatter(fmt)
logging.basicConfig(level=logging.INFO, handlers=[stdout, ring])
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

pipeline.WORK.mkdir(parents=True, exist_ok=True)
state = pipeline.State()
store = pipeline.Store()
wake = threading.Event()
sched = {"running": False, "next": None}


def scheduler():
    while True:
        sched["running"] = True
        try:
            pipeline.run_once(state, store)
        except Exception:
            log.exception("check failed")
        sched["running"] = False
        sched["next"] = time.time() + INTERVAL
        wake.wait(INTERVAL)
        wake.clear()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
basic = HTTPBasic(auto_error=False)


def admin(request: Request, creds: HTTPBasicCredentials | None = Depends(basic)):
    ok = (ADMIN_PASSWORD and creds
          and secrets.compare_digest(creds.username.encode(), ADMIN_USER.encode())
          and secrets.compare_digest(creds.password.encode(), ADMIN_PASSWORD.encode()))
    if not ok:
        raise HTTPException(401, "Admin login required", {"WWW-Authenticate": 'Basic realm="evox-apps"'})
    # A plain form on another site can't add this header, which blocks CSRF
    # with the browser's cached basic-auth login.
    if request.method == "POST" and request.headers.get("x-requested-with") != "evox":
        raise HTTPException(403, "Missing X-Requested-With header")
    return True


@app.on_event("startup")
def start():
    threading.Thread(target=scheduler, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD.read_text()


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/login")
def login(_=Depends(admin)):
    return RedirectResponse("/", 303)


def public_pending(entry):
    return {k: v for k, v in entry.items() if k != "file"}


@app.get("/api/state")
def api_state(request: Request, creds: HTTPBasicCredentials | None = Depends(basic)):
    is_admin = bool(ADMIN_PASSWORD and creds
                    and secrets.compare_digest(creds.username.encode(), ADMIN_USER.encode())
                    and secrets.compare_digest(creds.password.encode(), ADMIN_PASSWORD.encode()))
    index = store.get_json(store.key("index.json")) or {"apps": []}
    data = state.data
    return {
        "admin": is_admin,
        "adminEnabled": bool(ADMIN_PASSWORD),
        "running": sched["running"],
        "nextCheck": sched["next"],
        "intervalHours": INTERVAL / 3600,
        "autoApprove": pipeline.AUTO_APPROVE,
        "indexUrl": store.url(store.key("index.json")),
        "published": index.get("apps", []),
        "pending": {k: public_pending(v) for k, v in data["pending"].items()},
        "history": data["history"][:30],
        "lastRun": data.get("lastRun"),
        "order": pipeline.app_order(),
    }


@app.post("/api/check")
def api_check(_=Depends(admin)):
    if sched["running"]:
        return {"started": False, "message": "A check is already running"}
    wake.set()
    return {"started": True}


@app.post("/api/approve/{app_id}")
def api_approve(app_id: str, _=Depends(admin)):
    try:
        return pipeline.approve(state, store, app_id)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/api/reject/{app_id}")
def api_reject(app_id: str, _=Depends(admin)):
    try:
        pipeline.reject(state, app_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.get("/api/pending/{app_id}/apk")
def api_pending_apk(app_id: str, _=Depends(admin)):
    entry = state.data["pending"].get(app_id)
    if not entry or not Path(entry["file"]).exists():
        raise HTTPException(404, "No pending build")
    return FileResponse(entry["file"], media_type="application/vnd.android.package-archive",
                        filename=Path(entry["file"]).name)


@app.get("/api/logs")
def api_logs(_=Depends(admin)):
    return {"lines": list(ring.lines)}
