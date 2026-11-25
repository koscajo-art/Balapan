from contextlib import closing
from datetime import date, time, timedelta
from pathlib import Path
import hashlib
import hmac
import os
import secrets
import sqlite3
from typing import Annotated, Generic, Literal, TypeVar

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.datastructures import UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parent
DB = Path(os.getenv("BALAPAN_DB", ROOT / "balapan.db"))
MEDIA = Path(os.getenv("BALAPAN_MEDIA", ROOT / "event-media"))
app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")
ROLES = {"parent": "Ата-ана", "teacher": "Тәрбиеші", "admin": "Директор"}
WEEKDAYS = ("Дүйсенбі", "Сейсенбі", "Сәрсенбі", "Бейсенбі", "Жұма", "Сенбі", "Жексенбі")
STATES = {"pending": "Қаралуда", "accepted": "Қабылданды", "rejected": "Қабылданбады"}
ATTENDANCE = {"present": "Келді", "absent": "Келмеді", "excused": "Себепті"}
MENU = [("08:30", "Таңғы ас", "Сұлы ботқасы, ірімшік, сүт"), ("10:00", "Екінші таңғы ас", "Алма, су"), ("12:00", "Түскі ас", "Көкөніс сорпасы, тауық еті, қарақұмық"), ("15:30", "Бесін ас", "Айран, сүзбе қосылған бәліш")]
ROUTINES = (
    ("2–3 жас", ("Еркін ойын және ортаға бейімделу", "Сенсорлық ойындар", "Серуен", "Ұзақ тынығу", "Ертегі және тыныш ойын"), ("08:00", "09:10", "10:20", "12:20", "15:50")),
    ("3–4 жас", ("Таңғы жаттығу", "Тіл дамыту және шығармашылық", "Серуен және қимылды ойын", "Тынығу", "Құрастыру"), ("08:10", "09:30", "10:45", "12:45", "16:00")),
    ("4–5 жас", ("Таңғы жаттығу", "Зерттеу, санау және топтық тапсырмалар", "Спорттық ойындар және серуен", "Тынығу", "Шағын жоба және оқу"), ("08:15", "09:30", "11:00", "13:00", "16:10")),
)


def connect():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)


def add_user(db, email, name, password, role):
    salt = secrets.token_bytes(16)
    return db.execute(
        "INSERT INTO users(email,name,salt,password,role) VALUES (?,?,?,?,?)",
        (email, name, salt, password_hash(password, salt), role),
    ).lastrowid


def initialize():
    with closing(connect()) as db, db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          salt BLOB NOT NULL, password BLOB NOT NULL, role TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(
          token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
          expires TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS groups(
          id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, ages TEXT NOT NULL,
          capacity INTEGER NOT NULL CHECK(capacity > 0), description TEXT NOT NULL,
          teacher_id INTEGER NOT NULL REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS applications(
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
          group_id INTEGER NOT NULL REFERENCES groups(id), child TEXT NOT NULL,
          birth TEXT NOT NULL, phone TEXT NOT NULL, note TEXT NOT NULL,
          state TEXT DEFAULT 'pending' CHECK(state IN ('pending','accepted','rejected')),
          created TEXT DEFAULT CURRENT_DATE);
        CREATE UNIQUE INDEX IF NOT EXISTS active_application
          ON applications(user_id, child, birth) WHERE state != 'rejected';
        CREATE TABLE IF NOT EXISTS attendance(
          child_id INTEGER REFERENCES applications(id), day TEXT, state TEXT NOT NULL,
          PRIMARY KEY(child_id,day));
        CREATE TABLE IF NOT EXISTS diaries(
          child_id INTEGER NOT NULL REFERENCES applications(id), day TEXT NOT NULL,
          meal TEXT NOT NULL, rest TEXT NOT NULL, activity TEXT NOT NULL,
          note TEXT NOT NULL, PRIMARY KEY(child_id,day));
        CREATE TABLE IF NOT EXISTS absence_notices(
          child_id INTEGER NOT NULL REFERENCES applications(id), day TEXT NOT NULL,
          reason TEXT NOT NULL, PRIMARY KEY(child_id,day));
        CREATE TABLE IF NOT EXISTS news(
          id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
          created TEXT DEFAULT CURRENT_DATE);
        CREATE TABLE IF NOT EXISTS event_details(
          news_id INTEGER PRIMARY KEY REFERENCES news(id),
          details TEXT NOT NULL, event_day TEXT, location TEXT NOT NULL,
          photo TEXT, video TEXT);
        """)
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return
        teacher = add_user(db, "teacher@balapan.test", "Айгерім Серікқызы", "Balapan2026!", "teacher")
        parent = add_user(db, "parent@balapan.test", "Әлия Омарова", "Balapan2026!", "parent")
        add_user(db, "admin@balapan.test", "Меруерт Қанатқызы", "Balapan2026!", "admin")
        db.executemany("INSERT INTO groups(name,ages,capacity,description,teacher_id) VALUES (?,?,?,?,?)", [
            ("Балапан", "2–3 жас", 12, "Бейімделу, сенсорлық ойындар және алғашқы достар.", teacher),
            ("Қарлығаш", "3–4 жас", 16, "Тіл дамыту, сурет салу және қозғалыс ойындары.", teacher),
            ("Күншуақ", "4–5 жас", 18, "Шығармашылық, құрастыру және топтық жобалар.", teacher),
        ])
        db.execute("INSERT INTO applications(user_id,group_id,child,birth,phone,note,state) VALUES (?,?,?,?,?,?,'accepted')",
                   (parent, 2, "Аяла Омарова", date.today().replace(day=1, month=1, year=date.today().year-4).isoformat(), "+7 700 000 00 00", ""))
        db.executemany("INSERT INTO news(title,body) VALUES (?,?)", [
            ("Ашық есік күні", "Ата-аналарды топ бөлмелерімен танысуға және тәрбиешілермен әңгімелесуге шақырамыз."),
            ("Шығармашылық шеберхана", "Осы аптада балалар түрлі түсті қағаздан өз қолдарымен жануарлар жасайды."),
        ])


initialize()


def initialize_event_examples():
    examples = [
        ("Ашық есік күні",
         "Алдымен топ бөлмелерін аралаймыз. Содан кейін тәрбиеші күн тәртібі мен бейімделу кезеңін түсіндіреді. Кездесу соңында ата-аналар қабылдау тәртібі бойынша сұрақ қоя алады.",
         "Балабақшаның кіреберісі"),
        ("Шығармашылық шеберхана",
         "Балалар түрлі түсті қағаздан жануарлардың пішінін құрастырады. Тәрбиеші материалдар мен құралдарды дайындайды. Дайын жұмыстар топта шағын көрмеге қойылады.",
         "Топ бөлмесі"),
    ]
    with closing(connect()) as db, db:
        db.executemany("""
          INSERT OR IGNORE INTO event_details(news_id,details,location)
          SELECT id,?,? FROM news WHERE title=?
        """, [(details, location, title) for title, details, location in examples])


initialize_event_examples()


def initialize_schedule():
    with closing(connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='lessons' AND type='table'").fetchone():
            return
        db.execute("""
          CREATE TABLE lessons(
            id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL REFERENCES groups(id),
            weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
            starts TEXT NOT NULL, ends TEXT NOT NULL, title TEXT NOT NULL, task TEXT NOT NULL,
            CHECK(starts < ends))
        """)
        demo = [
            ("Балапан", 0, "09:30", "09:50", "Сенсорлық ойын", "Түсті текшелер мен сұрыптау себеттерін дайындау."),
            ("Қарлығаш", 0, "10:00", "10:25", "Тіл дамыту", "Суретті карточкалармен жаңа сөздерді қайталау."),
            ("Күншуақ", 0, "10:30", "10:55", "Құрастыру", "Балаларды шағын топтарға бөліп, құрастырғыштарды тарату."),
            ("Қарлығаш", 2, "09:30", "09:55", "Сурет салу", "Қағаз бен қарындаштарды дайындау."),
            ("Күншуақ", 4, "10:00", "10:25", "Қимылды ойындар", "Ойын алаңын дайындау және қауіпсіздік ережелерін қайталау."),
        ]
        db.executemany("""
          INSERT INTO lessons(group_id,weekday,starts,ends,title,task)
          SELECT id,?,?,?,?,? FROM groups WHERE name=?
        """, [(*lesson[1:], lesson[0]) for lesson in demo])


initialize_schedule()


def current_user(request, db):
    token = request.cookies.get("balapan", "")
    return db.execute("""
      SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id
      WHERE token=? AND expires>=?
    """, (token, date.today().isoformat())).fetchone()


def attendance_report(db, day):
    return db.execute("""
      SELECT g.id AS group_id, g.name, COUNT(a.id) AS total,
        COUNT(CASE WHEN t.state='present' THEN 1 END) AS present,
        COUNT(CASE WHEN t.state='absent' THEN 1 END) AS absent,
        COUNT(CASE WHEN t.state='excused' THEN 1 END) AS excused,
        COUNT(CASE WHEN a.id IS NOT NULL AND t.state IS NULL THEN 1 END) AS unmarked
      FROM groups g LEFT JOIN applications a ON a.group_id=g.id AND a.state='accepted'
      LEFT JOIN attendance t ON t.child_id=a.id AND t.day=?
      GROUP BY g.id ORDER BY g.id
    """, (day,)).fetchall()


def groups(db):
    return db.execute("""
      SELECT g.*, u.name AS teacher,
        (SELECT COUNT(*) FROM applications a WHERE a.group_id=g.id AND a.state='accepted') AS enrolled
      FROM groups g JOIN users u ON u.id=g.teacher_id ORDER BY g.id
    """).fetchall()


def show(request, page, user=None, status=200, **context):
    return templates.TemplateResponse(request=request, name="site.html", context={
        "page": page, "user": user, "roles": ROLES, "states": STATES,
        "attendance_labels": ATTENDANCE, "today": date.today().isoformat(),
        **context,
    }, status_code=status)


def require(db, request, *roles):
    user = current_user(request, db)
    if user is None or user["role"] not in roles:
        raise HTTPException(403, "Бұл әрекетке рұқсат жоқ.")
    return user


def check_origin(request):
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
        raise HTTPException(403, "Сұрау қабылданбады.")


def clean(form, name, limit):
    return str(form.get(name, "")).strip()[:limit]


@app.get("/")
def home(request: Request):
    with closing(connect()) as db:
        return show(request, "home", current_user(request, db), groups=groups(db),
                    news=db.execute("SELECT * FROM news ORDER BY id DESC LIMIT 3").fetchall())


@app.get("/toptar")
def group_directory(request: Request):
    with closing(connect()) as db:
        return show(request, "groups", current_user(request, db), groups=groups(db))


@app.get("/tarbieshiler")
def teacher_directory(request: Request):
    with closing(connect()) as db:
        roster = {}
        for group in groups(db):
            member = roster.setdefault(group["teacher_id"], {"name": group["teacher"], "groups": []})
            member["groups"].append(group["name"])
        return show(request, "teachers", current_user(request, db), roster=roster.values())


@app.get("/keste")
def schedule(request: Request, group: int | None = None, age: int = 0):
    with closing(connect()) as db:
        user = current_user(request, db)
        assigned = groups(db) if user and user["role"] == "admin" else []
        if user and user["role"] == "teacher":
            assigned = [g for g in groups(db) if g["teacher_id"] == user["id"]]
        if user and user["role"] == "parent":
            ids = {row[0] for row in db.execute(
                "SELECT DISTINCT group_id FROM applications WHERE user_id=? AND state='accepted'", (user["id"],)
            )}
            assigned = [g for g in groups(db) if g["id"] in ids]
        selected = next((g for g in assigned if g["id"] == group), None)
        if group is not None and selected is None:
            raise HTTPException(404, "Топтың күн тәртібі қолжетімсіз.")
        selected = selected or (assigned[0] if assigned else None)
        if selected:
            minimum_age = next((int(c) for c in selected["ages"] if c in "0123456789"), 3)
            age = min(2, max(0, minimum_age - 2))
        if not 0 <= age < len(ROUTINES):
            raise HTTPException(404)
        label, activities, hours = ROUTINES[age]
        plan = [("07:30", "Балаларды қарсы алу"), *zip(hours, activities), ("18:30", "Күннің аяқталуы")]
        return show(request, "schedule", user, menu=MENU, schedule=plan, assigned=assigned,
                    selected_group=selected, age=age, profiles=[r[0] for r in ROUTINES], age_label=label)


@app.get("/janalyq/{news_id}")
def news_detail(request: Request, news_id: int):
    with closing(connect()) as db:
        user = current_user(request, db)
        item = db.execute("""
          SELECT n.*, d.details, d.event_day, d.location, d.photo, d.video
          FROM news n LEFT JOIN event_details d ON d.news_id=n.id WHERE n.id=?
        """, (news_id,)).fetchone()
        if item is None:
            raise HTTPException(404)
        return show(request, "event", user, item=item, msg=request.query_params.get("msg", ""))


MEDIA_TYPES = {
    "photo": {".jpg": (b"\xff\xd8\xff", "image/jpeg"), ".png": (b"\x89PNG\r\n\x1a\n", "image/png")},
    "video": {".mp4": (b"ftyp", "video/mp4"), ".webm": (b"\x1a\x45\xdf\xa3", "video/webm")},
}
MEDIA_LIMITS = {"photo": 5 * 1024 * 1024, "video": 25 * 1024 * 1024}


@app.get("/janalyq/{news_id}/{kind}")
def event_file(news_id: int, kind: str):
    if kind not in MEDIA_TYPES:
        raise HTTPException(404)
    with closing(connect()) as db:
        record = db.execute("SELECT photo,video FROM event_details WHERE news_id=?", (news_id,)).fetchone()
    filename = record[kind] if record else None
    if not filename:
        raise HTTPException(404)
    path = MEDIA / filename
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type=MEDIA_TYPES[kind][path.suffix][1],
                        headers={"X-Content-Type-Options": "nosniff"})


@app.get("/kiru")
def login_page(request: Request):
    with closing(connect()) as db:
        if current_user(request, db):
            return RedirectResponse("/kabinet", 303)
    return show(request, "login")


@app.get("/tirkelu")
def register_page(request: Request):
    return show(request, "register")


@app.post("/kiru")
@app.post("/tirkelu")
async def identify(request: Request):
    check_origin(request)
    form = await request.form()
    email = clean(form, "email", 254).lower()
    password = str(form.get("password", ""))
    registering = request.url.path == "/tirkelu"
    with closing(connect()) as db, db:
        if registering:
            name = clean(form, "name", 100)
            if not name or "@" not in email or not 8 <= len(password) <= 128:
                return show(request, "register", status=400, error="Аты-жөніңізді, email және 8–128 таңбалы құпиясөз енгізіңіз.")
            try:
                user_id = add_user(db, email, name, password, "parent")
            except sqlite3.IntegrityError:
                return show(request, "register", status=409, error="Бұл email жүйеде тіркелген.")
        else:
            user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if not user or len(password) > 128 or not hmac.compare_digest(user["password"], password_hash(password, user["salt"])):
                return show(request, "login", status=401, error="Email немесе құпиясөз қате.")
            user_id = user["id"]
        token = secrets.token_urlsafe(32)
        db.execute("DELETE FROM sessions WHERE expires < ? OR token=?", (date.today().isoformat(), request.cookies.get("balapan", "")))
        db.execute("INSERT INTO sessions VALUES (?,?,?)",
                   (token, user_id, (date.today()+timedelta(days=7)).isoformat()))
    response = RedirectResponse("/kabinet", 303)
    response.set_cookie("balapan", token, httponly=True, samesite="lax", max_age=604800, secure=request.url.scheme=="https")
    return response


@app.post("/shygu")
def logout(request: Request):
    check_origin(request)
    with closing(connect()) as db, db:
        db.execute("DELETE FROM sessions WHERE token=?", (request.cookies.get("balapan", ""),))
    response = RedirectResponse("/", 303)
    response.delete_cookie("balapan")
    return response


@app.get("/kabinet")
def dashboard(request: Request, status: str = "", q: str = "", day: date | None = None, msg: str = ""):
    selected_day = day or date.today()
    day = selected_day.isoformat()
    with closing(connect()) as db:
        user = current_user(request, db)
        if not user:
            return RedirectResponse("/kiru", 303)
        sql = """
          SELECT a.*, g.name AS group_name, u.name AS parent_name, t.state AS attendance, n.reason AS absence_reason,
            (SELECT MAX(d.day) FROM diaries d WHERE d.child_id=a.id AND d.day<=?) AS latest_diary
          FROM applications a JOIN groups g ON g.id=a.group_id JOIN users u ON u.id=a.user_id
          LEFT JOIN attendance t ON t.child_id=a.id AND t.day=?
          LEFT JOIN absence_notices n ON n.child_id=a.id AND n.day=? WHERE 1=1
        """
        params = [day, day, day]
        if user["role"] == "parent":
            sql += " AND a.user_id=?"
            params.append(user["id"])
        elif user["role"] == "teacher":
            sql += " AND g.teacher_id=? AND a.state='accepted'"
            params.append(user["id"])
        if status in STATES and user["role"] == "admin":
            sql += " AND a.state=?"
            params.append(status)
        rows = db.execute(sql + " ORDER BY a.id DESC", params).fetchall()
        rows = [row for row in rows if q.casefold() in row["child"].casefold()]
        report = attendance_report(db, day) if user["role"] == "admin" else []
        lessons = []
        if user["role"] == "teacher":
            lessons = db.execute("""
              SELECT l.*, g.name AS group_name
              FROM lessons l JOIN groups g ON g.id=l.group_id
              WHERE g.teacher_id=? AND l.weekday=? ORDER BY l.starts,l.id
            """, (user["id"], selected_day.weekday())).fetchall()
        return show(request, "dashboard", user, groups=groups(db), rows=rows,
                    day=day, quick_days=[(date.today()-timedelta(days=n)).isoformat() for n in range(3)], report=report, lessons=lessons, weekday=WEEKDAYS[selected_day.weekday()], selected_status=status, q=q, msg=msg,
                    news=db.execute("SELECT * FROM news ORDER BY id DESC LIMIT 5").fetchall(),
                    teachers=db.execute("SELECT id,name FROM users WHERE role='teacher' ORDER BY name").fetchall())


@app.get("/otinim")
def apply_page(request: Request, group: int = 0):
    with closing(connect()) as db:
        user = current_user(request, db)
        if not user:
            return RedirectResponse("/kiru", 303)
        require(db, request, "parent")
        return show(request, "apply", user, groups=groups(db), selected=str(group), values={})


@app.post("/otinim")
async def apply(request: Request):
    check_origin(request)
    form = await request.form()
    values = {key: clean(form, key, limit) for key, limit in
              [("child", 100), ("birth", 10), ("phone", 30), ("note", 500), ("group", 10)]}
    with closing(connect()) as db, db:
        user = require(db, request, "parent")
        available = groups(db)
        group = next((g for g in available if str(g["id"]) == values["group"]), None)
        try:
            birth = date.fromisoformat(values["birth"])
        except ValueError:
            birth = None
        error = ""
        if not values["child"] or not birth or birth >= date.today() or sum(c.isdigit() for c in values["phone"]) < 10:
            error = "Баланың аты-жөнін, дұрыс туған күнін және телефон нөмірін енгізіңіз."
        elif not group or group["enrolled"] >= group["capacity"]:
            error = "Бос орны бар топты таңдаңыз."
        else:
            try:
                db.execute("INSERT INTO applications(user_id,group_id,child,birth,phone,note) VALUES (?,?,?,?,?,?)",
                           (user["id"], group["id"], values["child"], birth.isoformat(), values["phone"], values["note"]))
            except sqlite3.IntegrityError:
                error = "Бұл балаға белсенді өтінім бар."
        if error:
            return show(request, "apply", user, status=400, groups=available, selected=values["group"], values=values, error=error)
    return redirect("submitted")


def redirect(msg, day=None):
    return RedirectResponse("/kabinet?msg=" + msg + ("&day=" + day if day else ""), 303)


@app.post("/otinim/{application_id}/sheshim")
async def decide(request: Request, application_id: int):
    check_origin(request)
    form = await request.form()
    state = form.get("state")
    if state not in ("accepted", "rejected"):
        raise HTTPException(400)
    with closing(connect()) as db, db:
        require(db, request, "admin")
        result = decide_application(db, application_id, state)
    return redirect("decided" if result == "missing" else result)


def decide_application(db, application_id, state):
    db.execute("BEGIN IMMEDIATE")
    application = db.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone()
    if application is None:
        return "missing"
    if application["state"] != "pending":
        return "decided"
    if state == "accepted":
        group = next(g for g in groups(db) if g["id"] == application["group_id"])
        if group["enrolled"] >= group["capacity"]:
            return "full"
    db.execute("UPDATE applications SET state=? WHERE id=?", (state, application_id))
    return "saved"


@app.post("/katysu/{child_id}")
async def mark(request: Request, child_id: int):
    check_origin(request)
    form = await request.form()
    state = form.get("state")
    try:
        day = date.fromisoformat(str(form.get("day")))
    except ValueError:
        raise HTTPException(400) from None
    if state not in ATTENDANCE or day > date.today():
        raise HTTPException(400)
    with closing(connect()) as db, db:
        user = require(db, request, "teacher")
        child = db.execute("""
          SELECT a.id FROM applications a JOIN groups g ON g.id=a.group_id
          WHERE a.id=? AND a.state='accepted' AND g.teacher_id=?
        """, (child_id, user["id"])).fetchone()
        if not child:
            raise HTTPException(404)
        db.execute("""
          INSERT INTO attendance VALUES (?,?,?) ON CONFLICT(child_id,day)
          DO UPDATE SET state=excluded.state
        """, (child_id, day.isoformat(), state))
    return redirect("saved", day.isoformat())


@app.post("/toptar")
async def save_group(request: Request):
    check_origin(request)
    form = await request.form()
    name = clean(form, "name", 60)
    ages = clean(form, "ages", 30)
    description = clean(form, "description", 250)
    try:
        capacity = int(str(form.get("capacity")))
        teacher = int(str(form.get("teacher")))
        group_id = int(str(form.get("group_id", "0")))
    except ValueError:
        return redirect("invalid")
    with closing(connect()) as db, db:
        require(db, request, "admin")
        db.execute("BEGIN IMMEDIATE")
        if not name or not ages or not description or not 1 <= capacity <= 100 or not db.execute("SELECT 1 FROM users WHERE id=? AND role='teacher'", (teacher,)).fetchone():
            return redirect("invalid")
        enrolled = db.execute("SELECT COUNT(*) FROM applications WHERE group_id=? AND state='accepted'", (group_id,)).fetchone()[0]
        if enrolled > capacity:
            return redirect("capacity")
        if group_id and db.execute("""
          SELECT 1 FROM lessons own JOIN lessons other
            ON other.weekday=own.weekday AND other.starts<own.ends AND other.ends>own.starts
          JOIN groups assigned ON assigned.id=other.group_id
          WHERE own.group_id=? AND other.group_id!=? AND assigned.teacher_id=?
        """, (group_id, group_id, teacher)).fetchone():
            return redirect("schedule_conflict")
        try:
            if group_id:
                db.execute("UPDATE groups SET name=?,ages=?,capacity=?,description=?,teacher_id=? WHERE id=?",
                           (name, ages, capacity, description, teacher, group_id))
            else:
                db.execute("INSERT INTO groups(name,ages,capacity,description,teacher_id) VALUES (?,?,?,?,?)",
                           (name, ages, capacity, description, teacher))
        except sqlite3.IntegrityError:
            return redirect("duplicate")
    return redirect("saved")


async def read_event_upload(form, kind):
    upload = form.get(kind)
    if not isinstance(upload, UploadFile) or not upload.filename:
        return None
    suffix = Path(upload.filename).suffix.lower()
    suffix = ".jpg" if suffix == ".jpeg" else suffix
    limit = MEDIA_LIMITS[kind]
    header = MEDIA_TYPES[kind].get(suffix)
    data = await upload.read(limit + 1)
    offset = 4 if suffix == ".mp4" else 0
    if not header or len(data) > limit or not data.startswith(header[0], offset):
        raise ValueError
    return secrets.token_hex(16) + suffix, data


@app.post("/habarlandyru")
@app.post("/habarlandyru/{news_id}")
async def publish(request: Request, news_id: int | None = None):
    check_origin(request)
    with closing(connect()) as db:
        require(db, request, "admin")
        if news_id is not None and not db.execute("SELECT 1 FROM news WHERE id=?", (news_id,)).fetchone():
            raise HTTPException(404)
    try:
        async with request.form(max_files=2, max_fields=8) as form:
            title, body = clean(form, "title", 100), clean(form, "body", 1000)
            details, location = clean(form, "details", 5000), clean(form, "location", 140)
            event_day = clean(form, "event_day", 10)
            if event_day:
                event_day = date.fromisoformat(event_day).isoformat()
            if not title or not body:
                raise ValueError
            uploads = {kind: await read_event_upload(form, kind) for kind in MEDIA_TYPES}
    except ValueError:
        if news_id is not None:
            return RedirectResponse(f"/janalyq/{news_id}?msg=event_invalid", 303)
        return redirect("event_invalid")

    written, replaced = [], []
    try:
        with closing(connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if news_id is None:
                news_id = db.execute("INSERT INTO news(title,body) VALUES (?,?)", (title, body)).lastrowid
            else:
                db.execute("UPDATE news SET title=?,body=? WHERE id=?", (title, body, news_id))
            previous = db.execute("SELECT photo,video FROM event_details WHERE news_id=?", (news_id,)).fetchone()
            filenames = {}
            for kind, upload in uploads.items():
                old = previous[kind] if previous else None
                filenames[kind] = old
                if upload:
                    MEDIA.mkdir(parents=True, exist_ok=True)
                    filename, data = upload
                    path = MEDIA / filename
                    written.append(path)
                    path.write_bytes(data)
                    filenames[kind] = filename
                    if old:
                        replaced.append(MEDIA / old)
            db.execute("""
              INSERT INTO event_details VALUES (?,?,?,?,?,?) ON CONFLICT(news_id)
              DO UPDATE SET details=excluded.details,event_day=excluded.event_day,
                location=excluded.location,photo=excluded.photo,video=excluded.video
            """, (news_id, details, event_day or None, location, filenames["photo"], filenames["video"]))
    except (OSError, sqlite3.Error):
        for path in written:
            path.unlink(missing_ok=True)
        raise
    for path in replaced:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return RedirectResponse(f"/janalyq/{news_id}?msg=saved", 303)


def child_access(db, user, child_id):
    child = db.execute("""
      SELECT a.id, a.child, g.name AS group_name
      FROM applications a JOIN groups g ON g.id=a.group_id
      WHERE a.id=? AND a.state='accepted' AND
        ((?='parent' AND a.user_id=?) OR (?='teacher' AND g.teacher_id=?))
    """, (child_id, user["role"], user["id"], user["role"], user["id"])).fetchone()
    if child is None:
        raise HTTPException(404)
    return child


@app.get("/kundelik/{child_id}")
def diary(request: Request, child_id: int, day: date | None = None, msg: str = ""):
    day = (day or date.today()).isoformat()
    with closing(connect()) as db:
        user = require(db, request, "parent", "teacher")
        child = child_access(db, user, child_id)
        entry = db.execute("SELECT * FROM diaries WHERE child_id=? AND day=?", (child_id, day)).fetchone()
        notice = db.execute("SELECT reason FROM absence_notices WHERE child_id=? AND day=?", (child_id, day)).fetchone()
        return show(request, "diary", user, child=child, day=day, entry=entry, notice=notice, msg=msg)


@app.post("/kundelik/{child_id}")
async def diary_update(request: Request, child_id: int):
    check_origin(request)
    form = await request.form()
    try:
        day = date.fromisoformat(clean(form, "day", 10))
    except ValueError:
        raise HTTPException(400) from None
    with closing(connect()) as db, db:
        user = require(db, request, "parent", "teacher")
        db.execute("BEGIN IMMEDIATE")
        child_access(db, user, child_id)
        msg = "saved"
        if user["role"] == "teacher":
            fields = [clean(form, key, 500) for key in ("meal", "rest", "activity", "note")]
            if day > date.today() or not all(fields[:3]):
                msg = "invalid"
            else:
                db.execute("""
                  INSERT INTO diaries VALUES (?,?,?,?,?,?) ON CONFLICT(child_id,day)
                  DO UPDATE SET meal=excluded.meal, rest=excluded.rest,
                    activity=excluded.activity, note=excluded.note
                """, (child_id, day.isoformat(), *fields))
        elif day < date.today():
            msg = "invalid"
        elif form.get("action") == "cancel":
            db.execute("DELETE FROM absence_notices WHERE child_id=? AND day=?", (child_id, day.isoformat()))
        else:
            reason = clean(form, "reason", 250)
            if not reason:
                msg = "invalid"
            else:
                db.execute("""
                  INSERT INTO absence_notices VALUES (?,?,?) ON CONFLICT(child_id,day)
                  DO UPDATE SET reason=excluded.reason
                """, (child_id, day.isoformat(), reason))
    return RedirectResponse(f"/kundelik/{child_id}?day={day.isoformat()}&msg={msg}", 303)


@app.get("/sabak-kestesi")
def lesson_schedule(request: Request, msg: str = ""):
    with closing(connect()) as db:
        user = require(db, request, "admin", "teacher")
        query = """
          SELECT l.*,g.name AS group_name,u.name AS teacher
          FROM lessons l JOIN groups g ON g.id=l.group_id JOIN users u ON u.id=g.teacher_id
        """
        params = ()
        if user["role"] == "teacher":
            query += " WHERE g.teacher_id=?"
            params = (user["id"],)
        lessons = db.execute(query + " ORDER BY l.weekday,l.starts,l.id", params).fetchall()
        return show(request, "lessons", user, lessons=lessons, weekdays=WEEKDAYS, groups=groups(db), msg=msg)


def lesson_redirect(msg):
    return RedirectResponse(f"/sabak-kestesi?msg={msg}", 303)


@app.post("/sabak-kestesi")
async def save_lesson(request: Request):
    check_origin(request)
    form = await request.form()
    with closing(connect()) as db, db:
        require(db, request, "admin")
        try:
            lesson_id = int(str(form.get("lesson_id", "0")))
            group_id = int(str(form.get("group")))
            weekday = int(str(form.get("weekday")))
            starts, ends = (time.fromisoformat(str(form.get(key))).strftime("%H:%M") for key in ("starts", "ends"))
        except ValueError:
            return lesson_redirect("invalid")
        title = clean(form, "title", 100)
        task = clean(form, "task", 500)
        if not (title and task and 0 <= weekday < len(WEEKDAYS) and starts < ends):
            return lesson_redirect("invalid")
        db.execute("BEGIN IMMEDIATE")
        result, _ = store_lesson(db, lesson_id, group_id, weekday, starts, ends, title, task)
        if result == "missing":
            raise HTTPException(404)
    return lesson_redirect(result)


def store_lesson(db, lesson_id, group_id, weekday, starts, ends, title, task):
    group = db.execute("SELECT teacher_id FROM groups WHERE id=?", (group_id,)).fetchone()
    if group is None:
        return "invalid", None
    conflict = db.execute("""
      SELECT 1 FROM lessons l JOIN groups g ON g.id=l.group_id
      WHERE g.teacher_id=? AND l.weekday=? AND l.starts<? AND l.ends>? AND l.id!=?
    """, (group["teacher_id"], weekday, ends, starts, lesson_id)).fetchone()
    if conflict:
        return "conflict", None
    values = (group_id, weekday, starts, ends, title, task)
    if lesson_id:
        changed = db.execute("""
          UPDATE lessons SET group_id=?,weekday=?,starts=?,ends=?,title=?,task=? WHERE id=?
        """, (*values, lesson_id))
        if not changed.rowcount:
            return "missing", None
    else:
        lesson_id = db.execute("""
          INSERT INTO lessons(group_id,weekday,starts,ends,title,task) VALUES (?,?,?,?,?,?)
        """, values).lastrowid
    return "saved", lesson_id


@app.post("/sabak-kestesi/{lesson_id}/oshiru")
def delete_lesson(request: Request, lesson_id: int):
    check_origin(request)
    with closing(connect()) as db, db:
        require(db, request, "admin")
        if not db.execute("DELETE FROM lessons WHERE id=?", (lesson_id,)).rowcount:
            raise HTTPException(404)
    return lesson_redirect("deleted")


@app.middleware("http")
async def director_api_only(request: Request, call_next):
    path = request.url.path
    if path != "/api" and not path.startswith("/api/"):
        return await call_next(request)
    with closing(connect()) as db:
        user = current_user(request, db)
    headers = {"Cache-Control": "no-store"}
    if user is None:
        return JSONResponse({"detail": "Кіру қажет."}, status_code=401, headers=headers)
    if user["role"] != "admin":
        return JSONResponse({"detail": "Директорға ғана қолжетімді."}, status_code=403, headers=headers)
    if request.method in {"POST", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        expected = f"{request.url.scheme}://{request.headers.get('host')}"
        if request.headers.get("sec-fetch-site") == "cross-site" or (origin and origin != expected):
            return JSONResponse({"detail": "Сұрау қабылданбады."}, status_code=403, headers=headers)
    response = await call_next(request)
    response.headers.update(headers)
    return response


@app.get("/api/docs", include_in_schema=False)
def api_docs():
    return get_swagger_ui_html(
        openapi_url="/api/openapi.json",
        title="Балапан · REST API",
        swagger_ui_parameters={"validatorUrl": None, "docExpansion": "list", "persistAuthorization": False},
    )


@app.get("/api/openapi.json", include_in_schema=False)
def api_schema():
    return get_openapi(
        title="Балапан REST API", version="1.1", routes=api.routes,
        description="Директор кабинетіне кіргеннен кейін осы беттен сұрауларды орындауға болады. "
                    "Өзгерістер сайттағы дерекқорға бірден жазылады.",
    )



T = TypeVar("T")
ApplicationState = Literal["pending", "accepted", "rejected"]
LessonTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
LessonTask = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Clock = Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]


class Collection(BaseModel, Generic[T]):
    items: list[T]


class ErrorResponse(BaseModel):
    detail: str


api = APIRouter(prefix="/api/v1", responses={
    401: {"model": ErrorResponse, "description": "Кіру қажет"},
    403: {"model": ErrorResponse, "description": "Директорға ғана қолжетімді"},
})


class GroupData(BaseModel):
    id: int
    name: str
    ages: str
    teacher: str
    capacity: int
    enrolled: int
    available: int


class AttendanceData(BaseModel):
    group_id: int
    name: str
    total: int
    present: int
    absent: int
    excused: int
    unmarked: int


class DailyAttendance(Collection[AttendanceData]):
    day: date


class AttendancePeriod(Collection[DailyAttendance]):
    start: date
    end: date


class ApplicationData(BaseModel):
    id: int
    child: str
    group_id: int
    group_name: str
    state: ApplicationState
    created: date


class ApplicationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["accepted", "rejected"]


class LessonInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_id: int = Field(gt=0)
    weekday: int = Field(ge=0, le=6, description="0 — дүйсенбі, 6 — жексенбі")
    starts: Clock
    ends: Clock
    title: LessonTitle
    task: LessonTask


class LessonPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_id: Annotated[int, Field(gt=0)] | None = None
    weekday: Annotated[int, Field(ge=0, le=6)] | None = None
    starts: Clock | None = None
    ends: Clock | None = None
    title: LessonTitle | None = None
    task: LessonTask | None = None


class LessonData(LessonInput):
    id: int
    group_name: str
    teacher: str
    weekday_name: str


class EventSummary(BaseModel):
    id: int
    title: str
    body: str
    created: date
    url: str


class EventData(EventSummary):
    details: str | None
    event_day: date | None
    location: str | None
    photo_url: str | None
    video_url: str | None


@api.get("/groups", tags=["Топтар"], summary="Топтар және бос орындар", response_model=Collection[GroupData])
def api_groups():
    with closing(connect()) as db:
        return {"items": [{**dict(g), "available": g["capacity"] - g["enrolled"]} for g in groups(db)]}


def check_api_group(db, group_id):
    if group_id is not None and not db.execute("SELECT 1 FROM groups WHERE id=?", (group_id,)).fetchone():
        raise HTTPException(404, "Топ табылмады.")


def daily_attendance(db, day, group_id):
    return {"day": day, "items": [
        dict(row) for row in attendance_report(db, day.isoformat())
        if group_id is None or row["group_id"] == group_id
    ]}


@api.get("/attendance", tags=["Қатысу"], summary="Бір күннің есебі", response_model=DailyAttendance)
def api_attendance(day: date | None = None, group_id: int | None = None):
    with closing(connect()) as db:
        check_api_group(db, group_id)
        return daily_attendance(db, day or date.today(), group_id)


@api.get("/attendance/period", tags=["Қатысу"], summary="Кезең бойынша есеп", response_model=AttendancePeriod)
def api_attendance_period(start: date, end: date, group_id: int | None = None):
    days = (end - start).days + 1
    if not 1 <= days <= 31:
        raise HTTPException(422, "Кезең 1–31 күн аралығында болуы керек.")
    with closing(connect()) as db, db:
        db.execute("BEGIN")
        check_api_group(db, group_id)
        return {"start": start, "end": end, "items": [
            daily_attendance(db, start + timedelta(days=n), group_id) for n in range(days)
        ]}


APPLICATION_QUERY = """
    SELECT a.id,a.child,a.group_id,g.name AS group_name,a.state,a.created
    FROM applications a JOIN groups g ON g.id=a.group_id
"""


@api.get("/applications", tags=["Өтінімдер"], summary="Қабылдау өтінімдері", response_model=Collection[ApplicationData])
def api_applications(state: ApplicationState | None = None, group_id: int | None = None,
                     limit: Annotated[int, Query(ge=1, le=100)] = 50,
                     offset: Annotated[int, Query(ge=0, le=1_000_000_000)] = 0):
    with closing(connect()) as db:
        check_api_group(db, group_id)
        rows = db.execute(APPLICATION_QUERY + """
          WHERE (? IS NULL OR a.state=?) AND (? IS NULL OR a.group_id=?)
          ORDER BY a.id DESC LIMIT ? OFFSET ?
        """, (state, state, group_id, group_id, limit, offset))
        return {"items": [dict(row) for row in rows]}


@api.patch("/applications/{application_id}", tags=["Өтінімдер"], summary="Өтінім бойынша шешім",
           response_model=ApplicationData, responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}})
def api_decide(application_id: int, decision: ApplicationDecision):
    with closing(connect()) as db, db:
        result = decide_application(db, application_id, decision.state)
        if result == "missing":
            raise HTTPException(404, "Өтінім табылмады.")
        if result == "decided":
            raise HTTPException(409, "Бұл өтінім бойынша шешім қабылданған.")
        if result == "full":
            raise HTTPException(409, "Топта бос орын жоқ.")
        return dict(db.execute(APPLICATION_QUERY + " WHERE a.id=?", (application_id,)).fetchone())


LESSON_QUERY = """
    SELECT l.*, g.name AS group_name, u.name AS teacher
    FROM lessons l JOIN groups g ON g.id=l.group_id JOIN users u ON u.id=g.teacher_id
"""


def lesson_data(row):
    return {**dict(row), "weekday_name": WEEKDAYS[row["weekday"]]}


@api.get("/schedule", tags=["Сабақ кестесі"], summary="Апталық сабақ кестесі", response_model=Collection[LessonData])
def api_schedule(group_id: int | None = None):
    with closing(connect()) as db:
        check_api_group(db, group_id)
        rows = db.execute(LESSON_QUERY + """
          WHERE (? IS NULL OR g.id=?) ORDER BY l.weekday,l.starts,l.id
        """, (group_id, group_id))
        return {"items": [lesson_data(row) for row in rows]}


def write_api_lesson(db, lesson_id, lesson):
    if lesson.starts >= lesson.ends:
        raise HTTPException(422, "Аяқталу уақыты басталу уақытынан кейін болуы керек.")
    result, saved_id = store_lesson(db, lesson_id, **lesson.model_dump())
    if result == "invalid":
        raise HTTPException(404, "Топ табылмады.")
    if result == "conflict":
        raise HTTPException(409, "Тәрбиешінің осы уақытта басқа сабағы бар.")
    if result == "missing":
        raise HTTPException(404, "Сабақ табылмады.")
    return lesson_data(db.execute(LESSON_QUERY + " WHERE l.id=?", (saved_id,)).fetchone())


LESSON_ERRORS = {404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}}


@api.post("/schedule", tags=["Сабақ кестесі"], summary="Сабақ қосу",
          response_model=LessonData, status_code=201, responses=LESSON_ERRORS)
def api_add_lesson(lesson: LessonInput):
    with closing(connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        return write_api_lesson(db, 0, lesson)


@api.patch("/schedule/{lesson_id}", tags=["Сабақ кестесі"], summary="Сабақты өзгерту",
           response_model=LessonData, responses=LESSON_ERRORS)
def api_patch_lesson(lesson_id: int, patch: LessonPatch):
    updates = patch.model_dump(exclude_unset=True)
    if not updates or None in updates.values():
        raise HTTPException(422, "Өзгеретін өрістерді толтырыңыз.")
    with closing(connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("""
          SELECT group_id,weekday,starts,ends,title,task FROM lessons WHERE id=?
        """, (lesson_id,)).fetchone()
        if current is None:
            raise HTTPException(404, "Сабақ табылмады.")
        return write_api_lesson(db, lesson_id, LessonInput(**{**dict(current), **updates}))


@api.delete("/schedule/{lesson_id}", tags=["Сабақ кестесі"], summary="Сабақты өшіру",
            status_code=204, responses={404: {"model": ErrorResponse}})
def api_delete_lesson(lesson_id: int):
    with closing(connect()) as db, db:
        if not db.execute("DELETE FROM lessons WHERE id=?", (lesson_id,)).rowcount:
            raise HTTPException(404, "Сабақ табылмады.")
    return Response(status_code=204)


@api.get("/events", tags=["Іс-шаралар"], summary="Жарияланған іс-шаралар",
         response_model=Collection[EventSummary])
def api_events(limit: Annotated[int, Query(ge=1, le=100)] = 50,
               offset: Annotated[int, Query(ge=0, le=1_000_000_000)] = 0):
    with closing(connect()) as db:
        rows = db.execute("SELECT * FROM news ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
        return {"items": [{**dict(row), "url": f"/janalyq/{row['id']}"} for row in rows]}


@api.get("/events/{event_id}", tags=["Іс-шаралар"], summary="Іс-шараның толық ақпараты",
         response_model=EventData, responses={404: {"model": ErrorResponse}})
def api_event(event_id: int):
    with closing(connect()) as db:
        row = db.execute("""
          SELECT n.*,d.details,d.event_day,d.location,d.photo,d.video
          FROM news n LEFT JOIN event_details d ON d.news_id=n.id WHERE n.id=?
        """, (event_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Іс-шара табылмады.")
        return {**dict(row), "url": f"/janalyq/{event_id}",
                "photo_url": f"/janalyq/{event_id}/photo" if row["photo"] else None,
                "video_url": f"/janalyq/{event_id}/video" if row["video"] else None}


app.include_router(api)
