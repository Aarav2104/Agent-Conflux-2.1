"""Tests your Gemini key and connection. Run:  python check_gemini.py   (never prints your key)"""
import os, time, httpx
from dotenv import load_dotenv
load_dotenv()
key = os.getenv("LLM_API_KEY", "").strip().strip("\"'")
if not key: raise SystemExit("LLM_API_KEY is empty in .env")
BASE = "https://generativelanguage.googleapis.com/v1beta/"
H = {"x-goog-api-key": key}
t = time.time()
try:
    r = httpx.get(BASE + "models", headers=H, params={"pageSize": 100}, timeout=20)
except Exception as e:
    raise SystemExit("Cannot reach Google (%s). Check internet, VPN, proxy or firewall." % type(e).__name__)
print("List models: HTTP %s in %.1fs" % (r.status_code, time.time() - t))
if r.status_code != 200: raise SystemExit("Google rejected the key: " + r.text[:200])
names = [m["name"].split("/", 1)[1] for m in r.json().get("models", []) if "generateContent" in m.get("supportedGenerationMethods", []) and "flash" in m["name"]]
first = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
for m in [first] + [n for n in names if n != first][:4]:
    t = time.time()
    try:
        r = httpx.post(BASE + "models/%s:generateContent" % m, headers=H, timeout=40,
                       json={"contents": [{"role": "user", "parts": [{"text": "Say OK"}]}]})
        print("%-42s HTTP %s in %.1fs" % (m, r.status_code, time.time() - t))
    except Exception as e:
        print("%-42s %s after %.1fs" % (m, type(e).__name__, time.time() - t))
