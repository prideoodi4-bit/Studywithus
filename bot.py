import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

CHANNEL = "https://t.me/iwillsleep"
IRAQ = timezone(timedelta(hours=3))


class APIError(Exception):
    pass


def api(method, **args):
    req = Request("https://api.telegram.org/bot" + os.environ["BOT_TOKEN"] + "/" + method,
                  data=json.dumps(args).encode(), headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=35) as response:
            return json.load(response)["result"]
    except HTTPError as exc:
        body = json.load(exc)
        if exc.code == 429:
            time.sleep(body.get("parameters", {}).get("retry_after", 3))
            raise ConnectionError("rate limit") from None
        if exc.code >= 500:
            raise ConnectionError("Telegram unavailable") from None
        raise APIError(body.get("description", "Telegram error")) from None


def clock(stamp):
    return datetime.fromtimestamp(stamp, IRAQ).strftime("%H:%M")


def setup_db(path):
    db = sqlite3.connect(path, isolation_level=None)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS boards(chat INTEGER PRIMARY KEY, msg INTEGER, dirty INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS sessions(chat INTEGER, uid INTEGER, name TEXT, start INTEGER, finish INTEGER,
        last_msg INTEGER DEFAULT 0, strikes INTEGER DEFAULT 0, PRIMARY KEY(chat,uid));
    CREATE TABLE IF NOT EXISTS cursor_state(id INTEGER PRIMARY KEY, next_id INTEGER);
    """)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS drafts(chat INTEGER, uid INTEGER, prompt INTEGER, minutes INTEGER, PRIMARY KEY(chat,uid));
    CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY, chat INTEGER, uid INTEGER, name TEXT, topic TEXT,
        finish INTEGER, prompt INTEGER, answer TEXT, UNIQUE(chat,uid,finish));
    """)
    if "topic" not in {row[1] for row in db.execute("PRAGMA table_info(sessions)")}:
        db.execute("ALTER TABLE sessions ADD COLUMN topic TEXT DEFAULT 'دراسة'")
    db.execute("UPDATE boards SET dirty=1")
    return db


def tick(db):
    now = int(time.time())
    db.execute("""INSERT OR IGNORE INTO reviews(chat,uid,name,topic,finish)
        SELECT chat,uid,name,topic,finish FROM sessions WHERE finish<=?""", (now,))
    with db:
        db.execute("UPDATE boards SET dirty=1 WHERE chat IN (SELECT chat FROM sessions WHERE finish<=?)", (now,))
        db.execute("DELETE FROM sessions WHERE finish<=?", (now,))
    for rid, chat, name, topic in db.execute("SELECT id,chat,name,topic FROM reviews WHERE prompt IS NULL").fetchall():
        buttons = [[{"text": label, "callback_data": f"review:{rid}:{value}"}
                    for label, value in (("✅ خلصت", "yes"), ("بعدني", "no"))]]
        try:
            sent = api("sendMessage", chat_id=chat, text=f"{name}، انتهى وقت «{topic}». خلصت لو لا؟",
                       reply_markup={"inline_keyboard": buttons})
        except APIError as exc:
            print(str(exc), flush=True)
            db.execute("UPDATE reviews SET prompt=-1 WHERE id=?", (rid,))
            continue
        db.execute("UPDATE reviews SET prompt=? WHERE id=?", (sent["message_id"], rid))
    for chat, msg in db.execute("SELECT chat,msg FROM boards WHERE dirty=1").fetchall():
        rows = db.execute("SELECT name,finish,topic FROM sessions WHERE chat=? ORDER BY finish", (chat,)).fetchall()
        lines = ["📚 ادرس ويانا", f"هسه يدرسون {len(rows)} · التوقيت بغداد", ""]
        lines.extend(f"🟢 {name} · {topic[:32]} · يخلّص {clock(end)}" for name, end, topic in rows[:30])
        if len(rows) > 30:
            lines.append(f"وياهم {len(rows)-30} طالب إضافي")
        if not rows:
            lines.append("منو يبدي أول ساعة؟")
        lines.append("\nاكتب /session واختار مدتك وموضوعك. أول رسالة تنحذف ويا تنبيه، والثانية كتم لباقي السشن. للمشرف تنبيه بالكلام فقط.")
        data = dict(chat_id=chat, text="\n".join(lines), reply_markup={"inline_keyboard": []})
        try:
            if msg:
                api("editMessageText", message_id=msg, **data)
            else:
                msg = api("sendMessage", **data)["message_id"]
                db.execute("UPDATE boards SET msg=? WHERE chat=?", (msg, chat))
                api("pinChatMessage", chat_id=chat, message_id=msg, disable_notification=True)
        except APIError as exc:
            if "message to edit not found" in str(exc).lower():
                db.execute("UPDATE boards SET msg=NULL WHERE chat=?", (chat,))
                continue
            if "not modified" not in str(exc):
                print(str(exc), flush=True)
        db.execute("UPDATE boards SET dirty=0 WHERE chat=?", (chat,))


def handle(db, update, bot):
    now = int(time.time())
    q = update.get("callback_query")
    m = q.get("message", {}) if q else update.get("message", {})
    chat = m.get("chat", {})
    if not chat:
        return
    cid = chat["id"]
    user = (q or m).get("from", {})
    uid = user.get("id")
    if chat["type"] == "private":
        api("sendMessage", chat_id=cid, text="هلا بيك بـ «ادرس ويانا» 📚\nتفضل، هاي قناتنا 👇",
            reply_markup={"inline_keyboard": [[{"text": "📢 افتح القناة", "url": CHANNEL}]]})
        return
    if q:
        action = q.get("data", "")
        answer = "هذا الزر مو إلك أو انتهى استخدامه."
        mid = m["message_id"]
        if action in ("duration:60", "duration:90", "duration:120"):
            draft = db.execute("SELECT prompt,minutes FROM drafts WHERE chat=? AND uid=?", (cid, uid)).fetchone()
            if draft == (mid, 0):
                minutes = int(action.split(":")[1])
                sent = api("sendMessage", chat_id=cid, text=f"{user['first_name']}، شنو موضوع السشن؟ ردّ على هاي الرسالة.",
                           reply_markup={"force_reply": True})
                db.execute("UPDATE drafts SET prompt=?,minutes=? WHERE chat=? AND uid=?",
                           (sent["message_id"], minutes, cid, uid))
                answer = "اكتب موضوعك حتى نبدي."
        elif action.startswith("review:"):
            parts = action.split(":")
            if len(parts) == 3 and parts[2] in ("yes", "no"):
                saved = db.execute("""UPDATE reviews SET answer=? WHERE id=? AND chat=? AND uid=?
                    AND prompt=? AND answer IS NULL""", (parts[2], parts[1], cid, uid, mid))
                if saved.rowcount:
                    answer = "عاشت إيدك، سجلنا إنك خلصت!" if parts[2] == "yes" else "سجلنا إنك بعدك. تكدر تبدي سشن جديد."
        elif action == "hour":
            answer = "اكتب /session حتى تختار المدة والموضوع."
        api("answerCallbackQuery", callback_query_id=q["id"], text=answer)
        return

    if not uid or user.get("is_bot") or m.get("sender_chat"):
        return
    content = any(k in m for k in ("text photo video sticker animation audio voice video_note document poll "
                                    "contact location venue dice game paid_media story checklist").split())
    if not content:
        return
    mid = m["message_id"]
    session = db.execute("SELECT finish,strikes,last_msg FROM sessions WHERE chat=? AND uid=? AND start<=? AND finish>?",
                         (cid, uid, m["date"], now)).fetchone()
    cmd = (m.get("text") or "").strip().lower()
    if cmd in ("/session", "/session@" + bot["username"].lower()):
        if session:
            api("sendMessage", chat_id=cid, text=f"عندك سشن فعّال يخلص {clock(session[0])}.")
            return
        if not db.execute("SELECT 1 FROM boards WHERE chat=?", (cid,)).fetchone():
            api("sendMessage", chat_id=cid, text="خلّي المشرف يكتب /setup أول شي.")
            return
        choices = [[{"text": label, "callback_data": f"duration:{minutes}"}
                    for label, minutes in (("ساعة", 60), ("ساعة ونص", 90), ("ساعتين", 120))]]
        sent = api("sendMessage", chat_id=cid, text=f"{user['first_name']}، شكد تريد تدرس؟",
                   reply_markup={"inline_keyboard": choices})
        db.execute("INSERT OR REPLACE INTO drafts VALUES(?,?,?,0)", (cid, uid, sent["message_id"]))
        return
    reply = m.get("reply_to_message", {}).get("message_id")
    draft = db.execute("SELECT minutes FROM drafts WHERE chat=? AND uid=? AND prompt=? AND minutes>0",
                       (cid, uid, reply)).fetchone()
    if draft and not session:
        topic = " ".join(m.get("text", "").split())
        if not 1 <= len(topic) <= 100:
            api("sendMessage", chat_id=cid, text="ردّ على سؤال الموضوع بنص من حرف إلى 100 حرف.")
            return
        end = now + draft[0] * 60
        name = " ".join(user["first_name"].split())[:30]
        with db:
            db.execute("BEGIN")
            db.execute("""INSERT OR REPLACE INTO sessions(chat,uid,name,start,finish,topic)
                       VALUES(?,?,?,?,?,?)""", (cid, uid, name, now, end, topic))
            db.execute("DELETE FROM drafts WHERE chat=? AND uid=?", (cid, uid))
            db.execute("UPDATE boards SET dirty=1 WHERE chat=?", (cid,))
        api("sendMessage", chat_id=cid, text=f"{name}، بدا سشن «{topic}». تخلّص {clock(end)}.")
        return
    if session:
        end, strikes, last = session
        if mid < last:
            return
        member = api("getChatMember", chat_id=cid, user_id=uid)
        if member["status"] in ("administrator", "creator"):
            if mid != last:
                api("sendMessage", chat_id=cid,
                    text=f"{user.get('first_name', 'مشرف')}، تذكير 🤫 بعدك بالسشن لحد {clock(end)}. خلّينا نأجّل السوالف لبعدها.")
                db.execute("UPDATE sessions SET last_msg=? WHERE chat=? AND uid=?", (mid, cid, uid))
            return
        if mid != last:
            strikes += 1
            db.execute("UPDATE sessions SET strikes=?,last_msg=? WHERE chat=? AND uid=?", (strikes, mid, cid, uid))
        try:
            api("deleteMessage", chat_id=cid, message_id=mid)
        except APIError as exc:
            if "not found" not in str(exc).lower():
                print(str(exc), flush=True)
        if strikes == 1:
            api("sendMessage", chat_id=cid, text=f"{user.get('first_name','طالب')}، ساعتك تخلص {clock(end)}. الرسالة الجاية تفعّل الكتم 🤫")
        elif end-time.time() > 65:
            if member["status"] == "member":
                api("restrictChatMember", chat_id=cid, user_id=uid, until_date=end,
                    permissions={"can_send_messages": False})
        return
    cmd = (m.get("text") or "").strip().lower()
    if cmd not in ("/setup", "/setup@" + bot["username"].lower()):
        return
    if chat["type"] != "supergroup":
        api("sendMessage", chat_id=cid, text="فعّلني بسوبركروب حتى يشتغل الكتم المؤقت.")
        return
    if api("getChatMember", chat_id=cid, user_id=uid)["status"] not in ("creator", "administrator"):
        return
    rights = api("getChatMember", chat_id=cid, user_id=bot["id"])
    if not all(rights.get(k) for k in ("can_delete_messages", "can_restrict_members", "can_pin_messages")):
        api("sendMessage", chat_id=cid, text="أحتاج صلاحيات حذف الرسائل وتقييد الأعضاء وتثبيت الرسائل. بعدها اكتب /setup.")
        return
    db.execute("INSERT INTO boards(chat) VALUES(?) ON CONFLICT(chat) DO UPDATE SET dirty=1", (cid,))
    msg = db.execute("SELECT msg FROM boards WHERE chat=?", (cid,)).fetchone()[0]
    if msg:
        try:
            api("pinChatMessage", chat_id=cid, message_id=msg, disable_notification=True)
        except APIError as exc:
            if "not found" not in str(exc).lower():
                raise
            db.execute("UPDATE boards SET msg=NULL WHERE chat=?", (cid,))


def main():
    if not os.getenv("BOT_TOKEN"):
        raise SystemExit("Set BOT_TOKEN first.")
    db = setup_db(os.getenv("DB_PATH", "study.sqlite3"))
    bot = api("getMe")
    row = db.execute("SELECT next_id FROM cursor_state WHERE id=1").fetchone()
    offset = row[0] if row else 0
    while True:
        try:
            tick(db)
            for update in api("getUpdates", offset=offset, timeout=5, allowed_updates=["message","callback_query"]):
                try:
                    handle(db, update, bot)
                except APIError as exc:
                    print(str(exc), flush=True)
                offset = update["update_id"] + 1
                db.execute("INSERT OR REPLACE INTO cursor_state VALUES(1,?)", (offset,))
        except (OSError, URLError, APIError) as exc:
            print(type(exc).__name__, flush=True)
            time.sleep(4)


if __name__ == "__main__":
    main()
