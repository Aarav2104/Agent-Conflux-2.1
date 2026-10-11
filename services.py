"""Outbound providers. Mock mode (default) prints instead of contacting anyone. Credentials are read server-side only."""
import os, smtplib, ssl
from email.message import EmailMessage
from xml.sax.saxutils import escape
import httpx

def _mock(flag, *needed):
    return os.getenv(flag, "true").lower() == "true" or not all(os.getenv(n) for n in needed)

def send_email(to, subject, body):
    """Gmail SMTP with an App Password (free). Returns (ok, message); ok only when the server accepted the mail."""
    if _mock("MOCK_EMAIL", "EMAIL_ADDRESS", "EMAIL_APP_PASSWORD"):
        print("[MOCK EMAIL] to=%s subject=%r" % (to, subject))
        return True, "Mock mode: email was NOT actually sent"
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = os.environ["EMAIL_ADDRESS"].strip(), to, subject
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=20) as s:
            s.login(os.environ["EMAIL_ADDRESS"].strip(), os.environ["EMAIL_APP_PASSWORD"].replace(" ", ""))
            s.send_message(msg)
        return True, "Email sent"
    except Exception as e:
        return False, "Email was not sent: email service error (%s)" % type(e).__name__

def _twilio_call(to, message):
    """Twilio voice call that speaks the message once and hangs up. Needs an UPGRADED Twilio account: trial accounts reject custom TwiML."""
    clean = lambda k: os.environ[k].strip().strip("\"'")
    sid, token, frm = clean("TWILIO_SID"), clean("TWILIO_TOKEN"), clean("TWILIO_FROM")
    if not sid.startswith("AC"):
        return False, "Call was not placed: TWILIO_SID must start with AC (copy it from the Twilio Console home page, not an API key)"
    twiml = '<Response><Pause length="1"/><Say>%s</Say></Response>' % escape(message[:500])
    try:
        r = httpx.post("https://api.twilio.com/2010-04-01/Accounts/%s/Calls.json" % sid, auth=(sid, token),
                       data={"To": to, "From": frm, "Twiml": twiml}, timeout=20)
        if r.status_code == 201: return True, "Call placed; it hangs up after the message"
        try: detail = r.json().get("message", "")
        except Exception: detail = ""
        if r.status_code == 400 and "trial" in detail.lower():
            return False, ("Call was not placed: a Twilio trial account cannot speak custom messages through the API "
                           "(trials only allow Twilio's fixed sample calls). Upgrade the Twilio account to enable it, or set MOCK_PHONE=true.")
        return False, "Call was not placed: phone service error %s %s" % (r.status_code, detail[:150])
    except Exception as e:
        return False, "Call was not placed: phone service error (%s)" % type(e).__name__

def _sinch_call(to, message):
    """Sinch Voice 'ttsCallout': one API call, no public webhook. Speaks the text, then the call ends.
    Trial accounts can only dial numbers verified in the Sinch dashboard."""
    clean = lambda k: os.environ[k].strip().strip("\"'")
    key, secret, cli = clean("SINCH_APPLICATION_KEY"), clean("SINCH_APPLICATION_SECRET"), clean("SINCH_CLI")
    if not cli.startswith("+"):
        return False, "Call was not placed: SINCH_CLI must be your verified or Sinch dashboard number in +E.164 format"
    text = "".join(ch for ch in message if ch.isprintable() or ch == " ")[:600]  # Sinch default TTS limit is 600 characters
    host = os.getenv("SINCH_VOICE_HOST", "https://calling.api.sinch.com").strip().rstrip("/")
    body = {"method": "ttsCallout", "ttsCallout": {"cli": cli, "destination": {"type": "number", "endpoint": to},
                                                  "locale": os.getenv("SINCH_LOCALE", "en-US").strip(), "text": text}}
    try:
        r = httpx.post(host + "/calling/v1/callouts", auth=(key, secret), json=body, timeout=20)
        if r.status_code == 200: return True, "Call started; it speaks your message and hangs up"
        try: j = r.json(); detail = "%s %s" % (j.get("errorCode") or j.get("code") or "", j.get("message", ""))
        except Exception: detail = ""
        return False, "Call was not placed: phone service error %s %s" % (r.status_code, detail.strip()[:200])
    except Exception as e:
        return False, "Call was not placed: phone service error (%s)" % type(e).__name__

def _infobip_call(to, message):
    """Infobip Voice text-to-speech: POST /tts/3/single with {from,to,text,language}; falls back to /tts/3/advanced.
    Trial: 15 outgoing calls, verified recipient numbers only, 5 minutes max per call."""
    clean = lambda k: os.environ[k].strip().strip("\"'")
    key, frm = clean("INFOBIP_API_KEY"), clean("INFOBIP_FROM").lstrip("+")
    base = clean("INFOBIP_BASE_URL").rstrip("/")
    if not base.startswith("http"): base = "https://" + base
    host = base.split("//", 1)[1].split("/", 1)[0].lower()
    if not (host == "infobip.com" or host.endswith(".infobip.com")):
        return False, "Call was not placed: INFOBIP_BASE_URL must be your Infobip API address (like xxxxx.api.infobip.com)"
    if not frm.isdigit():
        return False, "Call was not placed: INFOBIP_FROM must be digits only (copy the 'from' value from the ready-made API request in your Infobip portal)"
    text = "".join(ch for ch in message if ch.isprintable() or ch == " ")[:600]
    lang = os.getenv("INFOBIP_LANGUAGE", "en").strip() or "en"
    headers = {"Authorization": "App " + key, "Content-Type": "application/json", "Accept": "application/json"}
    dest = to.lstrip("+")
    attempts = [("/tts/3/single", {"from": frm, "to": dest, "text": text, "language": lang}),
                ("/tts/3/advanced", {"messages": [{"from": frm, "destinations": [{"to": dest}], "text": text, "language": lang}]})]
    try:
        for n, (path, body) in enumerate(attempts):
            r = httpx.post(base + path, headers=headers, json=body, timeout=20)
            if r.status_code in (404, 405, 410) and n == 0: continue  # endpoint not available for this account, try the other
            try: j = r.json()
            except Exception: j = {}
            if r.status_code in (200, 201):
                msgs = j.get("messages") or []
                st = (msgs[0].get("status") if msgs else j.get("status")) or {}
                group = str(st.get("groupName", "")).upper()
                if group in ("REJECTED", "UNDELIVERABLE", "FAILED", "EXPIRED"):
                    return False, "Call was not placed: Infobip rejected it (%s %s)" % (st.get("name", group), st.get("description", ""))[:220]
                return True, "Infobip accepted the call request (%s). Delivery is not confirmed yet; it speaks your message and ends." % (st.get("name") or group or "accepted")
            ex = (j.get("requestError") or {}).get("serviceException") or {}
            return False, ("Call was not placed: Infobip error %s %s %s" % (r.status_code, ex.get("messageId", ""), ex.get("text", "")))[:240].strip()
        return False, "Call was not placed: Infobip voice endpoint not found for this account"
    except Exception as e:
        return False, "Call was not placed: phone service error (%s)" % type(e).__name__

def place_call(to, message):
    """PHONE_PROVIDER=infobip (default), sinch or twilio. Mock mode only logs."""
    provider = os.getenv("PHONE_PROVIDER", "infobip").strip().lower()
    need = {"infobip": ("INFOBIP_API_KEY", "INFOBIP_BASE_URL", "INFOBIP_FROM"),
            "sinch": ("SINCH_APPLICATION_KEY", "SINCH_APPLICATION_SECRET", "SINCH_CLI"),
            "twilio": ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM")}.get(provider)
    if need is None:
        return False, "Call was not placed: PHONE_PROVIDER must be infobip, sinch or twilio"
    if _mock("MOCK_PHONE", *need):
        print("[MOCK CALL] to=%s" % to)
        return True, "Mock mode: call was NOT actually placed"
    return {"infobip": _infobip_call, "sinch": _sinch_call, "twilio": _twilio_call}[provider](to, message)
