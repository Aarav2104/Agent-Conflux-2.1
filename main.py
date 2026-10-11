"""Agent Conflux 2.1 backend, Phases 1-2: auth, chats, uploads, RAG with citations.
Run: uvicorn main:app --reload   then open http://127.0.0.1:8000/docs"""
import os, re, time, uuid
from collections import defaultdict
from dotenv import load_dotenv
load_dotenv()
from fastapi import FastAPI, Request, Response, UploadFile, HTTPException, Depends
from pydantic import BaseModel
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import URLSafeTimedSerializer
import db, rag, agent, services
from fastapi.staticfiles import StaticFiles

ser = URLSafeTimedSerializer(os.environ["SESSION_SECRET"])
ph = PasswordHasher()
SESSION_AGE, MAX_BYTES, EXTS = 8 * 3600, 10 * 1024 * 1024, (".pdf", ".txt", ".docx")
app = FastAPI(title="Agent Conflux 2.1")
db.init()
print("Using database:", db.DB)
attempts = defaultdict(list)
sends = defaultdict(list)

@app.middleware("http")
async def headers(request, call_next):
    r = await call_next(request)
    if request.url.path.startswith("/api"): r.headers["Cache-Control"] = "no-store"
    r.headers["X-Content-Type-Options"] = "nosniff"
    return r

class Cred(BaseModel): username: str; password: str
class Text(BaseModel): text: str

def owner(scope: str, request: Request, response: Response) -> str:
    """Deterministic access control: private needs a valid session, public gets an anonymous id."""
    if scope == "private":
        try:
            return "u%s" % ser.loads(request.cookies.get("session", ""), max_age=SESSION_AGE)["u"]
        except Exception:
            raise HTTPException(401, "Login required")
    if scope != "public":
        raise HTTPException(404)
    vid = request.cookies.get("vid") or uuid.uuid4().hex
    response.set_cookie("vid", vid, httponly=True, samesite="lax", max_age=30 * 86400)
    return "a" + vid

def own_chat(cid, o, scope):
    rows, _ = db.q("SELECT * FROM chats WHERE id=? AND owner=? AND scope=?", (cid, o, scope))
    if not rows: raise HTTPException(404, "Chat not found")
    return rows[0]

# ---- auth (single-user private prototype) ----
@app.post("/api/auth/register")
def register(c: Cred):
    if db.q("SELECT id FROM users")[0]: raise HTTPException(403, "Already set up")
    if len(c.password) < 10: raise HTTPException(400, "Password must be 10+ characters")
    db.q("INSERT INTO users(username,pw_hash) VALUES(?,?)", (c.username, ph.hash(c.password)))
    return {"ok": True}

@app.post("/api/auth/login")
def login(c: Cred, request: Request, response: Response):
    ip, now = request.client.host, time.time()
    attempts[ip] = [t for t in attempts[ip] if now - t < 60]
    if len(attempts[ip]) >= 5: raise HTTPException(429, "Too many attempts, wait a minute")
    attempts[ip].append(now)
    rows, _ = db.q("SELECT * FROM users WHERE username=? COLLATE NOCASE", (c.username.strip(),))
    try:
        if not rows: raise VerifyMismatchError()
        ph.verify(rows[0]["pw_hash"], c.password)
    except VerifyMismatchError:
        raise HTTPException(401, "Invalid username or password")
    response.set_cookie("session", ser.dumps({"u": rows[0]["id"]}), httponly=True, samesite="lax", max_age=SESSION_AGE)
    return {"ok": True}

@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie("session"); return {"ok": True}

# ---- chats ----
@app.get("/api/{scope}/chats")
def chats(scope: str, o=Depends(owner)):
    return db.q("SELECT id,title,created FROM chats WHERE owner=? AND scope=? ORDER BY id DESC", (o, scope))[0]

@app.post("/api/{scope}/chats")
def new_chat(scope: str, o=Depends(owner)):
    return {"id": db.q("INSERT INTO chats(owner,scope,title) VALUES(?,?,'New chat')", (o, scope))[1]}

@app.patch("/api/{scope}/chats/{cid}")
def rename(scope: str, cid: int, b: Text, o=Depends(owner)):
    own_chat(cid, o, scope); db.q("UPDATE chats SET title=? WHERE id=?", (b.text[:80], cid)); return {"ok": True}

@app.delete("/api/{scope}/chats/{cid}")
def del_chat(scope: str, cid: int, o=Depends(owner)):
    own_chat(cid, o, scope)
    db.q("DELETE FROM messages WHERE chat_id=?", (cid,)); db.q("DELETE FROM chats WHERE id=?", (cid,)); return {"ok": True}

@app.get("/api/{scope}/chats/{cid}/messages")
def messages(scope: str, cid: int, o=Depends(owner)):
    own_chat(cid, o, scope)
    rows, out, now = db.q("SELECT m.role,m.text,m.source,m.created,a.id AS aid,a.kind,a.to_label,a.subject,a.body,a.status,a.result,a.expires "
                          "FROM messages m LEFT JOIN actions a ON a.id=m.action_id WHERE m.chat_id=? ORDER BY m.id", (cid,))[0], [], time.time()
    for r in rows:
        act = None
        if r["aid"]:
            st = "expired" if r["status"] == "pending" and r["expires"] < now else r["status"]
            act = {"id": r["aid"], "kind": r["kind"], "to": r["to_label"], "subject": r["subject"], "body": r["body"], "status": st, "result": r["result"]}
        out.append({"role": r["role"], "text": r["text"], "source": r["source"], "created": r["created"], "action": act})
    return out

@app.post("/api/{scope}/chats/{cid}/send")
def send(scope: str, cid: int, b: Text, o=Depends(owner)):
    chat = own_chat(cid, o, scope)
    now = time.time(); sends[o] = [t for t in sends[o] if now - t < 60]
    if len(sends[o]) >= 20: raise HTTPException(429, "Slow down: 20 messages per minute")
    sends[o].append(now)
    msg = b.text.strip()[:4000]
    if not msg: raise HTTPException(400, "Empty message")
    hist = db.q("SELECT role,text FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT 6", (cid,))[0][::-1]
    db.q("INSERT INTO messages(chat_id,role,text) VALUES(?,?,?)", (cid, "user", msg))
    if chat["title"] == "New chat": db.q("UPDATE chats SET title=? WHERE id=?", (msg[:36], cid))
    ctx = {"actions": []}
    text, used = agent.run(o, scope, hist, msg, ctx)
    source = ",".join(used) or "none"
    db.q("INSERT INTO messages(chat_id,role,text,source,action_id) VALUES(?,?,?,?,?)", (cid, "bot", text, source, ctx["actions"][-1] if ctx["actions"] else None))
    return {"text": text, "source": source}

# ---- documents ----
@app.post("/api/{scope}/docs")
async def upload(scope: str, file: UploadFile, o=Depends(owner)):
    name = os.path.basename(file.filename or "")
    if not name.lower().endswith(EXTS): raise HTTPException(400, "Allowed: PDF, TXT, DOCX")
    data = await file.read()
    if len(data) > MAX_BYTES: raise HTTPException(413, "File over 10 MB")
    doc_id = db.q("INSERT INTO docs(owner,scope,filename) VALUES(?,?,?)", (o, scope, name))[1]
    try:
        n = rag.ingest(o, scope, doc_id, name, data)
    except Exception:
        db.q("DELETE FROM docs WHERE id=?", (doc_id,)); raise HTTPException(422, "Could not read this file")
    if n == 0:
        rag.delete_doc(doc_id); db.q("DELETE FROM docs WHERE id=?", (doc_id,))
        raise HTTPException(422, "No readable text found in this file (scanned or image-only files are not supported yet)")
    return {"id": doc_id, "filename": name, "chunks": n}

@app.get("/api/{scope}/docs")
def docs(scope: str, o=Depends(owner)):
    return db.q("SELECT id,filename,created FROM docs WHERE owner=? AND scope=?", (o, scope))[0]

@app.delete("/api/{scope}/docs/{did}")
def del_doc(scope: str, did: int, o=Depends(owner)):
    if not db.q("SELECT id FROM docs WHERE id=? AND owner=? AND scope=?", (did, o, scope))[0]: raise HTTPException(404)
    rag.delete_doc(did); db.q("DELETE FROM docs WHERE id=?", (did,)); return {"ok": True}

# ---- Private Work: contacts + confirmed actions ----
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")
acts = defaultdict(list)

class ContactIn(BaseModel): name: str; email: str = ""; phone: str = ""

def priv(request: Request) -> str:
    return owner("private", request, Response())

def clean(c: ContactIn):
    name, email, phone = c.name.strip()[:60], c.email.strip()[:120], re.sub(r"[\s\-()]", "", c.phone.strip())
    if not name: raise HTTPException(400, "Name is required")
    if not email and not phone: raise HTTPException(400, "Add an email address or a phone number")
    if email and not EMAIL_RE.match(email): raise HTTPException(400, "That email address looks invalid")
    if phone and not PHONE_RE.match(phone): raise HTTPException(400, "Phone needs the country code, e.g. +919876543210")
    return name, email, phone

@app.get("/api/private/contacts")
def contacts(o=Depends(priv)):
    return db.q("SELECT id,name,email,phone FROM contacts WHERE owner=? ORDER BY name COLLATE NOCASE", (o,))[0]

@app.post("/api/private/contacts")
def add_contact(c: ContactIn, o=Depends(priv)):
    name, email, phone = clean(c)
    return {"id": db.q("INSERT INTO contacts(owner,name,email,phone) VALUES(?,?,?,?)", (o, name, email, phone))[1]}

@app.patch("/api/private/contacts/{cid}")
def edit_contact(cid: int, c: ContactIn, o=Depends(priv)):
    name, email, phone = clean(c)
    if db.ex("UPDATE contacts SET name=?,email=?,phone=? WHERE id=? AND owner=?", (name, email, phone, cid, o)) != 1: raise HTTPException(404)
    return {"ok": True}

@app.delete("/api/private/contacts/{cid}")
def del_contact(cid: int, o=Depends(priv)):
    if db.ex("DELETE FROM contacts WHERE id=? AND owner=?", (cid, o)) != 1: raise HTTPException(404)
    return {"ok": True}

@app.post("/api/private/actions/{aid}/confirm")
def confirm(aid: str, o=Depends(priv)):
    """The only code path that sends anything. Checks owner, pending state and expiry atomically, so it cannot run twice."""
    now = time.time(); acts[o] = [t for t in acts[o] if now - t < 3600]
    if len(acts[o]) >= 20: raise HTTPException(429, "Limit reached: 20 confirmed actions per hour")
    if db.ex("UPDATE actions SET status='sending' WHERE id=? AND owner=? AND status='pending' AND expires>?", (aid, o, now)) != 1:
        raise HTTPException(409, "This draft is no longer pending (already handled, cancelled, or expired)")
    acts[o].append(now)
    a = db.q("SELECT * FROM actions WHERE id=?", (aid,))[0][0]
    ok, msg = services.send_email(a["to_addr"], a["subject"], a["body"]) if a["kind"] == "email" else services.place_call(a["to_addr"], a["body"])
    st = "sent" if ok else "failed"
    db.q("UPDATE actions SET status=?,result=? WHERE id=?", (st, msg, aid))
    db.q("INSERT INTO audit(owner,kind,status,to_label) VALUES(?,?,?,?)", (o, a["kind"], st, a["to_label"]))
    return {"status": st, "result": msg}

@app.post("/api/private/actions/{aid}/cancel")
def cancel(aid: str, o=Depends(priv)):
    if db.ex("UPDATE actions SET status='cancelled' WHERE id=? AND owner=? AND status='pending'", (aid, o)) != 1:
        raise HTTPException(409, "This draft is no longer pending")
    return {"status": "cancelled"}

# ---- status (booleans only, never keys) ----
@app.get("/api/config")
def config(): return {"llm": bool(os.getenv("LLM_API_KEY")), "web": agent.web_enabled()}

@app.get("/api/auth/me")
def me(request: Request):
    try: ser.loads(request.cookies.get("session", ""), max_age=SESSION_AGE); return {"authed": True}
    except Exception: return {"authed": False}

app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), html=True), name="ui")
