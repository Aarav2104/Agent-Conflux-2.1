"""Reset (or create) the Private Work login. Run on the computer that hosts the app:  python reset_password.py
Your contacts and chats are kept. Only the username and password change."""
import getpass, sys
from dotenv import load_dotenv
load_dotenv()
import db
from argon2 import PasswordHasher

db.init()
rows = db.q("SELECT id,username FROM users")[0]
print("Database file:", db.DB)
if rows: print("Current username:", rows[0]["username"])
user = input("Username%s: " % (" (press Enter to keep it)" if rows else "")).strip() or (rows[0]["username"] if rows else "")
pw = getpass.getpass("New password (10+ characters): ")
if not user or len(pw) < 10 or pw != getpass.getpass("Repeat password: "):
    sys.exit("Cancelled: you need a username and a matching password of 10+ characters.")
h = PasswordHasher().hash(pw)
if rows: db.q("UPDATE users SET username=?,pw_hash=? WHERE id=?", (user, h, rows[0]["id"]))
else: db.q("INSERT INTO users(username,pw_hash) VALUES(?,?)", (user, h))
print("Done. Sign in with the username and password you just set.")
