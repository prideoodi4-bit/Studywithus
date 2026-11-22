from pathlib import Path
import re
from html import escape
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


def finished_sticker(chat):
    # A static Telegram sticker uploaded as multipart form data.
    boundary = "studywayana-static-image"
    parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat}\r\n",
             f"--{boundary}\r\nContent-Disposition: form-data; name=\"sticker\"; filename=\"finished.webp\"\r\nContent-Type: image/webp\r\n\r\n"]
    try:
        data = "".join(parts).encode() + Path(__file__).with_name("finished.webp").read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = Request("https://api.telegram.org/bot"+os.environ["BOT_TOKEN"]+"/sendSticker", data=data,
                      headers={"Content-Type":"multipart/form-data; boundary="+boundary})
        with urlopen(req, timeout=10) as result:
            result.read()
    except OSError:
        print("Completion sticker unavailable", flush=True)


def tag(uid, name):
    return f'<a href="tg://user?id={int(uid)}">{escape(name)}</a>'


def say(chat, text, **kwargs):
    return api("sendMessage", chat_id=chat, text=text, parse_mode="HTML", **kwargs)


def button(label, action, style="success"):
    return dict(text=label, callback_data=action, style=style)


def next_hours(now):
    first = ((now + 3600 + 3599) // 3600) * 3600
    return first, first + 3600


def custom_time(text, now):
    value = text.strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    if re.fullmatch(r"[0-9]{1,5}(?:\s*(?:دقيقة|دقائق|د))?", value):
        minutes = int(re.match(r"[0-9]+", value)[0])
        if 60 <= minutes <= 1440:
            return minutes, 0
    if re.fullmatch(r"(?:[01]?[0-9]|2[0-3]):[0-5][0-9]", value):
        hour, minute = map(int, value.split(":"))
        end = int(datetime.fromtimestamp(now, IRAQ).replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp())
        if end <= now:
            end += 86400
        if end-now >= 3600:
            return 0, end
    return None


def start_session(db, cid, user, topic, now, end, partner=0):
    uid = user["id"]
    name = " ".join(user["first_name"].split())[:30]
    with db:
        db.execute("BEGIN")
        db.execute("""INSERT OR REPLACE INTO sessions(chat,uid,name,start,finish,topic,partner)
            VALUES(?,?,?,?,?,?,?)""", (cid, uid, name, now, end, topic, partner))
        db.execute("DELETE FROM drafts WHERE chat=? AND uid=?", (cid, uid))
        db.execute("UPDATE boards SET dirty=1 WHERE chat=?", (cid,))
    say(cid, f"🟢 <b>بدا السشن</b>\n{tag(uid,name)} · {escape(topic)}\nتخلّص الساعة <b>{clock(end)}</b> بتوقيت بغداد.")


def ask(db, cid, user, message, minutes=0, deadline=0, stage="topic"):
    prompt = say(cid, tag(user["id"], user["first_name"]) + "، " + message,
                 reply_markup={"force_reply": True, "selective": True})
    db.execute("UPDATE drafts SET prompt=?,minutes=?,deadline=?,stage=? WHERE chat=? AND uid=?",
               (prompt["message_id"], minutes, deadline, stage, cid, user["id"]))


def session_menu(db, cid, user, now):
    uid = user["id"]
    active = db.execute("SELECT finish FROM sessions WHERE chat=? AND uid=? AND finish>?", (cid, uid, now)).fetchone()
    if active:
        say(cid, f"⏱ سشنك مستمر لحد {clock(active[0])}.")
        return
    if not db.execute("SELECT 1 FROM boards WHERE chat=?", (cid,)).fetchone():
        say(cid, "خلّي المشرف يكتب /setup أول شي.")
        return
    choices = [[button(label, f"duration:{n}") for n,label in ((60,"ساعة"),(90,"ساعة ونص"),(120,"ساعتين"))],
               [button("لحد " + clock(end), f"until:{end}") for end in next_hours(now)],
               [button("✍️ أكتب وقتي", "custom")]]
    peers = db.execute("SELECT uid,name,start FROM sessions WHERE chat=? AND finish>? AND partner=0 ORDER BY finish LIMIT 5",
                       (cid,now)).fetchall()
    for peer,name,start in peers:
        choices.append([button(f"🤝 أنضم ويا {name}", f"join:{peer}:{start}")])
    sent = say(cid, f"📚 {tag(uid,user['first_name'])}، شكد تريد تدرس؟\nاختار مدة أو ساعة نهاية بتوقيت بغداد."
               + ("\nبالانضمام تدرس نفس الموضوع، وسشنك ساعة على الأقل." if peers else ""),
               reply_markup={"inline_keyboard": choices})
    db.execute("INSERT OR REPLACE INTO drafts(chat,uid,prompt,minutes,stage) VALUES(?,?,?,0,'menu')",
               (cid,uid,sent["message_id"]))


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
    for table, col, definition in (("sessions","partner","INTEGER DEFAULT 0"),
                                   ("drafts","deadline","INTEGER DEFAULT 0"),
                                   ("drafts","stage","TEXT DEFAULT 'menu'")):
        if col not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
    db.execute("UPDATE drafts SET stage='topic' WHERE minutes>0 AND stage='menu'")
    db.execute("UPDATE boards SET dirty=1")
    return db


def tick(db):
    now = int(time.time())
    db.execute("""INSERT OR IGNORE INTO reviews(chat,uid,name,topic,finish)
        SELECT chat,uid,name,topic,finish FROM sessions WHERE finish<=?""", (now,))
    with db:
        db.execute("UPDATE boards SET dirty=1 WHERE chat IN (SELECT chat FROM sessions WHERE finish<=?)", (now,))
        db.execute("DELETE FROM sessions WHERE finish<=?", (now,))
    for rid, chat, uid, name, topic in db.execute("SELECT id,chat,uid,name,topic FROM reviews WHERE prompt IS NULL").fetchall():
        buttons = [[button(label, f"review:{rid}:{value}", "success" if value=="yes" else "danger")
                    for label, value in (("✅ خلصت", "yes"), ("🔴 بعدني", "no"))]]
        try:
            sent = say(chat, f"🏁 <b>انتهى السشن</b>\n{tag(uid,name)}، خلّصت «{escape(topic)}» لو بعدك؟",
                       reply_markup={"inline_keyboard": buttons})
        except APIError as exc:
            print(str(exc), flush=True)
            db.execute("UPDATE reviews SET prompt=-1 WHERE id=?", (rid,))
            continue
        db.execute("UPDATE reviews SET prompt=? WHERE id=?", (sent["message_id"], rid))
    for chat, msg in db.execute("SELECT chat,msg FROM boards WHERE dirty=1").fetchall():
        rows = db.execute("""SELECT s.uid,s.name,s.finish,s.topic,p.uid,p.name FROM sessions s
            LEFT JOIN sessions p ON p.chat=s.chat AND p.uid=s.partner
            WHERE s.chat=? ORDER BY s.finish,s.name""", (chat,)).fetchall()
        lines = ["📚 <b>ادرس ويانا | لوحة السشنات</b>", f"🟢 يدرسون هسه: <b>{len(rows)}</b>   ·   توقيت بغداد", ""]
        for uid,name,end,topic,peer,pname in rows[:15]:
            partner = f" · ويا {tag(peer,pname)}" if peer else ""
            lines.append(f"{tag(uid,name)}{partner} · <b>{clock(end)}</b>\n   📖 {escape(topic[:42])}")
        if len(rows)>15:
            lines.append(f"وياهم {len(rows)-15} طالب إضافي")
        if not rows:
            lines.append("الكروب هادئ. منو يفتح أول سشن؟")
        lines.append("\nاكتب «انشاء سشن» أو /session حتى تبدي أو تنضم لزميل.\n🔕 أول مخالفة تنبيه، والثانية كتم. للمشرف تذكير بالكلام.")
        data = dict(chat_id=chat, text="\n".join(lines), parse_mode="HTML", reply_markup={"inline_keyboard":[]})
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
        draft = db.execute("SELECT prompt,stage FROM drafts WHERE chat=? AND uid=?", (cid,uid)).fetchone()
        if draft == (mid,"menu") and not action.startswith("review:"):
            if action == "custom":
                ask(db,cid,user,"اكتب المدة بالدقائق (٦٠ إلى ١٤٤٠)، أو وقت النهاية مثل ١٩:٣٠ بنظام ٢٤ ساعة.",stage="custom")
                answer = "ردّ على الرسالة بوقتك."
            elif action in ("duration:60","duration:90","duration:120"):
                ask(db,cid,user,"شنو موضوع السشن؟",minutes=int(action.split(":")[1]))
                answer = "اكتب موضوعك."
            elif re.fullmatch(r"until:[0-9]{1,12}", action):
                end = int(action.split(":")[1])
                if end-now >= 3600:
                    ask(db,cid,user,"شنو موضوع السشن؟",deadline=end)
                    answer = "اكتب موضوعك."
                else:
                    answer = "باقي أقل من ساعة. اكتب /session واختار وقت جديد."
            elif re.fullmatch(r"join:[0-9]+:[0-9]+",action):
                _,peer,start = action.split(":")
                host = db.execute("SELECT topic,finish FROM sessions WHERE chat=? AND uid=? AND start=? AND finish>?",
                                  (cid,peer,start,now)).fetchone()
                if host:
                    start_session(db,cid,user,host[0],now,max(host[1],now+3600),int(peer))
                    answer = "انضمّيت للسشن!"
                else:
                    answer = "خلص سشن زميلك. اختار وقت من القائمة."
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
        if action.startswith("review:") and answer.startswith("عاشت إيدك"):
            finished_sticker(cid)
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
    if " ".join(cmd.split()) in ("انشاء سشن", "إنشاء سشن", "/session", "/session@" + bot["username"].lower()):
        session_menu(db,cid,user,now)
        return
    reply = m.get("reply_to_message", {}).get("message_id")
    draft = db.execute("SELECT stage,minutes,deadline FROM drafts WHERE chat=? AND uid=? AND prompt=?", (cid,uid,reply)).fetchone()
    if draft and not session:
        stage,minutes,end = draft
        if stage=="custom":
            value = custom_time(m.get("text",""),now)
            if value:
                ask(db,cid,user,"شنو موضوع السشن؟",*value)
            else:
                ask(db,cid,user,"جرّب ٩٠ للدقائق أو ١٩:٣٠ للساعة. أقل مدة ساعة كاملة.", stage="custom")
            return
        if stage=="topic":
            topic = " ".join(m.get("text","").split())
            if not 1 <= len(topic) <= 100:
                ask(db,cid,user,"اكتب موضوعك من حرف إلى ١٠٠ حرف.",minutes,end)
            elif end and end-now<3600:
                session_menu(db,cid,user,now)
            else:
                start_session(db,cid,user,topic,now,end or now+minutes*60)
            return
    if session:
        end, strikes, last = session
        if mid < last:
            return
        member = api("getChatMember", chat_id=cid, user_id=uid)
        if member["status"] in ("administrator", "creator"):
            if mid != last:
                say(cid, f"🟡 {tag(uid,user['first_name'])}، تذكير 🤫 بعدك بالسشن لحد {clock(end)}. خلّينا نأجّل السوالف لبعدها.")
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
            say(cid, f"🟡 {tag(uid,user['first_name'])}، جلستك تخلص {clock(end)}. الرسالة الجاية تفعّل الكتم 🤫")
        elif end-time.time() > 65:
            if member["status"] == "member":
                api("restrictChatMember", chat_id=cid, user_id=uid, until_date=end,
                    permissions={"can_send_messages": False})
                say(cid, f"🔴 <b>تم الكتم</b>\n{tag(uid,user['first_name'])} انكتم لحد <b>{clock(end)}</b>، نهاية سشنه.")
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
