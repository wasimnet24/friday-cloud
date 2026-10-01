"""Friday Cloud -- 24/7 relay taaki PC off ho tab bhi mobile se AI chat chale. ☁️

Deploy: free hosting (guide: CLOUD_SETUP.md). Env vars:
    CLOUD_TOKEN=...        # lamba random string (phone + PC dono me same)
    NVIDIA_API_KEY=...     # wasim ki NVIDIA key
    NVIDIA_MODEL=z-ai/glm-5.3
    PC_ID=wasim-pc         # (optional)

Kaam:
- POST /api/chat, /api/chat_stream -> cloud brain (hamesha online)
- PC actions: ghar ka PC (pc_link.py) har ~25s me /api/pc/poll karta hai;
  chat me PC action aaye to yahan queue hota hai, PC chalake /api/pc/result
  bhejta hai. PC offline ho to saaf jawab: "PC offline hai".
"""
import json
import secrets
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

import config
from brain import FridayBrain, extract_action

VERSION = "1.9-cloud"
PC_TIMEOUT = 50          # itne sec me poll na aaye to PC offline
POLL_WAIT = 25           # long-poll kitni der command ka wait kare
DISPATCH_TIMEOUT = 35    # chat request PC result ka kitna wait kare

app = FastAPI(title="Friday Cloud")

_brain = FridayBrain()

_lock = threading.Lock()
_pending = {}            # cmd_id -> {"name","args","event","result","ts"}
_pc_last_poll = 0.0


# -- auth --------------------------------------------------------------------
def _auth(authorization: str = ""):
    want = (config.CLOUD_TOKEN or "").strip()
    if not want:
        raise HTTPException(status_code=500,
                            detail="CLOUD_TOKEN set nahi hai (server env check karo)")
    if authorization != f"Bearer {want}":
        raise HTTPException(status_code=401, detail="galat token")


def _pc_auth(authorization: str = ""):
    _auth(authorization)  # same token; PC_ID header optional check skip


# -- pc link -------------------------------------------------------------------
def pc_online() -> bool:
    return (time.time() - _pc_last_poll) < PC_TIMEOUT


def dispatch_to_pc(name: str, args: dict, timeout: int = DISPATCH_TIMEOUT) -> str:
    """PC action bhejo, result ka wait karo. Hamesha string return karta hai."""
    if not pc_online():
        return "PC offline hai wasim -- on hote hi ye kaam kar dungi."
    cmd_id = secrets.token_hex(8)
    ev = threading.Event()
    with _lock:
        _pending[cmd_id] = {"name": name, "args": args or {},
                            "event": ev, "result": None, "ts": time.time()}
    ev.wait(timeout)
    with _lock:
        item = _pending.pop(cmd_id, None)
    if not item or not item["result"]:
        return "PC se jawab nahi aaya (timeout) -- PC online hai?"
    return item["result"]


def cloud_action_runner(name, args):
    try:
        return dispatch_to_pc(name, args or {})
    except Exception as e:
        return f"cloud link me dikkat: {e}"


def _fix_reply(reply: str, result: str) -> str:
    """Agar PC action fail/offline hua to jhootha 'ho gaya' mat bolo."""
    low = (result or "").lower()
    if any(k in low for k in ("offline", "timeout", "jawab nahi", "dikkat")):
        return (reply or "").rstrip() + f" (lekin {result})"
    return reply


# -- chat endpoints --------------------------------------------------------------
class ChatBody(BaseModel):
    text: str


@app.post("/api/chat")
def api_chat(body: ChatBody, authorization: str = Header(default="")):
    _auth(authorization)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text khaali hai")
    result = _brain.handle_user_text(text, action_runner=cloud_action_runner)
    reply = result.get("reply") or ""
    if result.get("action"):
        reply = _fix_reply(reply, result.get("action_result") or "")
    return {"ok": True, "reply": reply, "action": result.get("action"),
            "action_result": result.get("action_result"),
            "pc_online": pc_online()}


@app.post("/api/chat_stream")
def api_chat_stream(body: ChatBody, authorization: str = Header(default="")):
    _auth(authorization)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text khaali hai")

    def gen():
        action = None
        try:
            for kind, payload in _brain.chat_stream(text):
                if kind == "token":
                    yield "data: " + json.dumps({"t": payload},
                                                ensure_ascii=False) + "\n\n"
                elif kind == "done":
                    action, spoken = extract_action(payload)
                    result = None
                    if action:
                        result = cloud_action_runner(action.get("action"),
                                                     action.get("args") or {})
                    reply = spoken or payload
                    if action:
                        reply = _fix_reply(reply, result or "")
                    yield "data: " + json.dumps(
                        {"done": True, "reply": reply, "action": action,
                         "action_result": result, "pc_online": pc_online()},
                        ensure_ascii=False) + "\n\n"
        except Exception as e:
            yield "data: " + json.dumps(
                {"done": True, "reply": f"arey wasim, dikkat aayi: {e}",
                 "action": None, "action_result": None,
                 "pc_online": pc_online()},
                ensure_ascii=False) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


class PowerBody(BaseModel):
    op: str  # shutdown | restart


@app.post("/api/power")
def api_power(body: PowerBody, authorization: str = Header(default="")):
    _auth(authorization)
    op = (body.op or "").strip().lower()
    if op not in ("shutdown", "restart"):
        raise HTTPException(status_code=400, detail="op shutdown/restart hona chahiye")
    result = dispatch_to_pc(op + "_pc", {"confirm": True})
    return {"ok": True, "op": op, "result": result, "pc_online": pc_online()}


@app.get("/api/status")
def api_status(authorization: str = Header(default="")):
    _auth(authorization)
    return {"ok": True, "cloud": True, "version": VERSION,
            "pc_online": pc_online(),
            "brain_offline": _brain.offline}


# -- pc link endpoints -------------------------------------------------------------
class PCResult(BaseModel):
    cmd_id: str
    result: str = ""


@app.get("/api/pc/poll")
def api_pc_poll(authorization: str = Header(default="")):
    """PC ka long-poll: pending commands mile to turant, warna ~25s wait."""
    _pc_auth(authorization)
    global _pc_last_poll
    _pc_last_poll = time.time()
    deadline = time.time() + POLL_WAIT
    while time.time() < deadline:
        with _lock:
            cmds = [{"cmd_id": cid, "name": c["name"], "args": c["args"]}
                    for cid, c in _pending.items()]
        if cmds:
            return {"commands": cmds}
        time.sleep(0.5)
    return {"commands": []}


@app.post("/api/pc/result")
def api_pc_result(body: PCResult, authorization: str = Header(default="")):
    _pc_auth(authorization)
    with _lock:
        item = _pending.get(body.cmd_id)
        if item:
            item["result"] = body.result or ""
            item["event"].set()
            return {"ok": True}
    return {"ok": False, "detail": "unknown cmd_id"}


# -- mobile UI ----------------------------------------------------------------------
def _mobile_html():
    try:
        return (Path(__file__).parent / "static" / "mobile.html").read_text(
            encoding="utf-8")
    except Exception:
        return "<h1>mobile.html missing</h1>"


@app.get("/", response_class=HTMLResponse)
def mobile_ui():
    return _mobile_html()


@app.get("/manifest.json")
def manifest():
    return JSONResponse({
        "name": "Friday Cloud", "short_name": "Friday",
        "start_url": "/", "display": "standalone",
        "background_color": "#05060f", "theme_color": "#05060f",
        "icons": [{"src": "/icon.svg", "sizes": "512x512",
                   "type": "image/svg+xml"}],
    })


@app.get("/icon.svg")
def icon():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
           '<rect width="512" height="512" rx="110" fill="#05060f"/>'
           '<circle cx="256" cy="256" r="150" fill="none" stroke="#35e0ff" stroke-width="22"/>'
           '<circle cx="256" cy="256" r="86" fill="#d8f7ff"/>'
           '<circle cx="256" cy="256" r="86" fill="none" stroke="#ff7ad9" stroke-width="10" '
           'stroke-dasharray="20 26" opacity="0.8"/></svg>')
    return Response(svg, media_type="image/svg+xml")


if __name__ == "__main__":
    import uvicorn
    port = int((__import__("os").environ.get("PORT") or "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
