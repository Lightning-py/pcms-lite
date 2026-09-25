import hmac
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import add_user, connect, contest_open, data_dir, standings, effective_submission
from .polygon import ImportError
from .imports import enqueue_import
from .sandbox import LANGUAGES

DUMMY_PASSWORD_HASH = generate_password_hash("not-a-real-password")


def create_app(config=None):
    app = Flask(__name__)
    root = data_dir()
    secret = os.environ.get("PCMS_SECRET")
    if secret is None and (root / "session.key").is_file():
        secret = (root / "session.key").read_text().strip()
    app.config.update(DATA=root, SECRET_KEY=secret, MAX_CONTENT_LENGTH=128 * 1024 * 1024,
                      MAX_FORM_MEMORY_SIZE=1024 * 1024, SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=os.environ.get("PCMS_HTTPS") == "1",
                      PERMANENT_SESSION_LIFETIME=12 * 3600)
    if config:
        app.config.update(config)
    if not app.config["SECRET_KEY"] or len(app.config["SECRET_KEY"]) < 32:
        raise RuntimeError("Сначала выполните pcms init или задайте PCMS_SECRET длиной минимум 32 символа")

    @app.before_request
    def before():
        g.db = connect(app.config["DATA"])
        g.user = g.db.execute("SELECT id,username,is_admin FROM users WHERE id=?", (session.get("uid"),)).fetchone()
        session.setdefault("csrf", secrets.token_urlsafe(32))
        if request.method == "POST":
            token = request.form.get("csrf", "")
            if not hmac.compare_digest(token, session["csrf"]):
                abort(400, "Сессия формы истекла. Обновите страницу")
        if request.endpoint not in {"login", "static"} and not g.user:
            return redirect(url_for("login"))

    @app.teardown_appcontext
    def close(_):
        if "db" in g:
            g.db.close()

    @app.after_request
    def headers(response):
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'; form-action 'self'")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.template_filter("date")
    def date_filter(value):
        return datetime.fromtimestamp(value, timezone.utc).strftime("%d.%m.%Y %H:%M UTC") if value is not None else "Тренировка"

    @app.context_processor
    def context():
        return {"languages": LANGUAGES, "now": time.time(), "contest_open": contest_open}

    def admin_only():
        if not g.user["is_admin"]:
            abort(403)

    def get_contest(cid):
        c = g.db.execute("SELECT * FROM contests WHERE id=?", (cid,)).fetchone()
        if c is None:
            abort(404)
        return c

    def get_problem(pid):
        p = g.db.execute("SELECT * FROM problems WHERE id=?", (pid,)).fetchone()
        if p is None:
            abort(404)
        c = get_contest(p["contest_id"])
        if c["starts_at"] is not None and time.time() < c["starts_at"] and not g.user["is_admin"]:
            abort(403, "Задачи откроются после начала контеста")
        return p, c

    def get_submission(sid):
        s = g.db.execute("""SELECT s.*,p.label,p.title,p.contest_id,u.username FROM submissions s
            JOIN problems p ON p.id=s.problem_id JOIN users u ON u.id=s.user_id WHERE s.id=?""", (sid,)).fetchone()
        if s is None:
            abort(404)
        if s["user_id"] != g.user["id"] and not g.user["is_admin"]:
            abort(403)
        return effective_submission(s)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            address = request.remote_addr or "unknown"
            clock = time.time()
            g.db.execute("BEGIN IMMEDIATE")
            g.db.execute("DELETE FROM login_attempts WHERE window_start<?", (clock - 900,))
            row = g.db.execute("SELECT * FROM login_attempts WHERE address=?", (address,)).fetchone()
            if row and row["count"] >= 20:
                g.db.rollback()
                abort(429, "Слишком много попыток входа. Повторите через 15 минут")
            g.db.execute("INSERT INTO login_attempts VALUES(?,?,1) ON CONFLICT(address) DO UPDATE SET count=count+1", (address, clock))
            g.db.commit()
            user = g.db.execute("SELECT * FROM users WHERE username=?", (request.form.get("username", "")[:40],)).fetchone()
            valid = check_password_hash(user["password_hash"] if user else DUMMY_PASSWORD_HASH, request.form.get("password", "")[:256])
            if user and valid:
                session.clear()
                session["uid"] = user["id"]
                session["csrf"] = secrets.token_urlsafe(32)
                session.permanent = True
                g.db.execute("DELETE FROM login_attempts WHERE address=?", (address,))
                g.db.commit()
                return redirect(url_for("index"))
            flash("Неверный логин или пароль", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        return render_template("index.html", contests=g.db.execute("SELECT * FROM contests ORDER BY id DESC").fetchall())

    @app.get("/contests/<int:cid>")
    def contest(cid):
        c = get_contest(cid)
        visible = c["starts_at"] is None or c["starts_at"] <= time.time() or g.user["is_admin"]
        problems = g.db.execute("SELECT * FROM problems WHERE contest_id=? ORDER BY id", (cid,)).fetchall() if visible else []
        solved = {r[0] for r in g.db.execute("SELECT DISTINCT problem_id FROM submissions WHERE user_id=? AND COALESCE(manual_verdict,verdict)='AC'", (g.user["id"],))}
        return render_template("contest.html", contest=c, problems=problems, solved=solved, visible=visible, tab="problems")

    @app.route("/contests/<int:cid>/edit", methods=["GET", "POST"])
    def edit_contest(cid):
        admin_only()
        c = get_contest(cid)
        if request.method == "POST":
            try:
                title = request.form.get('title', '').strip()
                if not title or len(title) > 200:
                    raise ValueError('Название: от 1 до 200 символов')
                start = request.form.get('starts_at', '').strip()
                starts_at = datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp() if start else None
                duration = int(request.form.get('duration', '300'))
                if not 1 <= duration <= 10080:
                    raise ValueError('Длительность: от 1 минуты до 7 дней')
                g.db.execute('UPDATE contests SET title=?,starts_at=?,duration_minutes=? WHERE id=?', (title, starts_at, duration, cid))
                g.db.commit()
                flash('Настройки контеста сохранены', 'success')
                return redirect(url_for('contest', cid=cid))
            except (ValueError, OverflowError) as exc:
                flash(str(exc), 'error')
        start_value = datetime.fromtimestamp(c['starts_at'], timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') if c['starts_at'] is not None else ''
        return render_template('edit_contest.html', contest=c, start_value=start_value)

    @app.post("/submissions/<int:sid>/moderate")
    def moderate(sid):
        admin_only()
        get_submission(sid)
        action = request.form.get('action', '')
        if action not in {'accept', 'ban', 'restore'}:
            abort(400, 'Неизвестное действие')
        reason = request.form.get('reason', '').strip()
        if not reason or len(reason) > 1000:
            abort(400, 'Укажите причину: от 1 до 1000 символов')
        g.db.execute('BEGIN IMMEDIATE')
        row = g.db.execute('SELECT p.manifest FROM submissions s JOIN problems p ON p.id=s.problem_id WHERE s.id=?', (sid,)).fetchone()
        manifest = json.loads(row['manifest'])
        verdict = {'accept': 'AC', 'ban': 'BAN', 'restore': None}[action]
        score = (manifest.get('max_score') or 100) if action == 'accept' else (0 if action == 'ban' else None)
        g.db.execute('UPDATE submissions SET manual_verdict=?,manual_score=? WHERE id=?', (verdict, score, sid))
        g.db.execute('INSERT INTO moderation_log(submission_id,admin_id,action,reason,created_at) VALUES(?,?,?,?,?)', (sid, g.user['id'], action, reason, time.time()))
        g.db.commit()
        return redirect(url_for('submission', sid=sid))

    @app.route("/problems/<int:pid>", methods=["GET", "POST"])
    def problem(pid):
        p, c = get_problem(pid)
        if request.method == "POST":
            if not contest_open(c):
                abort(403, "Приём решений сейчас закрыт")
            language = request.form.get("language", "")
            if language not in LANGUAGES:
                abort(400, "Выберите язык")
            source = request.form.get("source", "")
            upload = request.files.get("source_file")
            if upload and upload.filename:
                try:
                    source = upload.read(256 * 1024 + 1).decode("utf-8-sig")
                except UnicodeDecodeError:
                    abort(400, "Исходник должен быть в UTF-8")
            if not source.strip() or len(source.encode()) > 256 * 1024 or "\x00" in source:
                abort(400, "Нужен непустой исходник UTF-8 размером до 256 КиБ")
            g.db.execute("BEGIN IMMEDIATE")
            queued = g.db.execute("SELECT count(*) FROM submissions WHERE user_id=? AND verdict IN ('QUEUED','RUNNING')", (g.user["id"],)).fetchone()[0]
            recent = g.db.execute("SELECT max(created_at) FROM submissions WHERE user_id=?", (g.user["id"],)).fetchone()[0]
            if queued >= 5 or (recent is not None and time.time() - recent < 3):
                g.db.rollback()
                abort(429, "Подождите: не более 5 решений в очереди и одной отправки в 3 секунды")
            sid = g.db.execute("INSERT INTO submissions(user_id,problem_id,language,source,created_at) VALUES(?,?,?,?,?)",
                               (g.user["id"], pid, language, source, time.time())).lastrowid
            g.db.commit()
            return redirect(url_for("submission", sid=sid))
        return render_template("problem.html", problem=p, contest=c, manifest=json.loads(p["manifest"]), tab="problems")

    @app.get("/problems/<int:pid>/statement/<path:name>")
    def statement(pid, name):
        p, _ = get_problem(pid)
        manifest = json.loads(p["manifest"])
        if name not in manifest["public_files"]:
            abort(404)
        response = send_file(Path(app.config["DATA"]) / "packages" / p["package_dir"] / name)
        response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
        return response

    @app.get("/contests/<int:cid>/submissions")
    def submissions(cid):
        c = get_contest(cid)
        page = max(1, request.args.get("page", 1, type=int))
        query = """SELECT s.*,p.label,u.username FROM submissions s JOIN problems p ON p.id=s.problem_id
            JOIN users u ON u.id=s.user_id WHERE p.contest_id=?"""
        args = [cid]
        if not g.user["is_admin"]:
            query += " AND s.user_id=?"
            args.append(g.user["id"])
        query += " ORDER BY s.id DESC LIMIT 51 OFFSET ?"
        args.append((page - 1) * 50)
        rows = g.db.execute(query, args).fetchall()
        return render_template("submissions.html", contest=c, rows=[effective_submission(row) for row in rows[:50]], more=len(rows) > 50, page=page, tab="submissions")

    @app.get("/submissions/<int:sid>")
    def submission(sid):
        s = get_submission(sid)
        tests = g.db.execute("SELECT * FROM test_results WHERE submission_id=? ORDER BY number", (sid,)).fetchall()
        history = g.db.execute("SELECT m.*,u.username FROM moderation_log m JOIN users u ON u.id=m.admin_id WHERE submission_id=? ORDER BY m.id DESC", (sid,)).fetchall()
        return render_template("submission.html", submission=s, tests=tests, history=history, contest=get_contest(s["contest_id"]), tab="submissions")

    @app.get("/api/submissions/<int:sid>")
    def submission_status(sid):
        s = get_submission(sid)
        return jsonify({k: s[k] for k in ("id", "verdict", "test_number", "time_ms", "memory_kb", "score")})

    @app.get("/contests/<int:cid>/standings")
    def scoreboard(cid):
        c = get_contest(cid)
        if c["starts_at"] is not None and c["starts_at"] > time.time() and not g.user["is_admin"]:
            return redirect(url_for("contest", cid=cid))
        problems, people = standings(g.db, c)
        return render_template("standings.html", contest=c, problems=problems, people=people, scored=any(json.loads(p["manifest"]).get("scoring") == "points" for p in problems), tab="standings")

    @app.route("/admin", methods=["GET", "POST"])
    def admin():
        admin_only()
        if request.method == "POST":
            try:
                action = request.form.get("action")
                if action == "user":
                    add_user(g.db, request.form.get("username", ""), request.form.get("password", ""))
                    g.db.commit()
                    flash("Участник создан", "success")
                elif action == "import":
                    upload = request.files.get("archive")
                    if not upload or not upload.filename:
                        raise ValueError("Выберите ZIP-пакет")
                    title = request.form.get("title", "").strip()
                    cid = request.form.get("contest_id", "")
                    if not cid and not title:
                        raise ValueError("Введите название нового контеста")
                    start = request.form.get("starts_at", "").strip()
                    starts_at = datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp() if start else None
                    duration = int(request.form.get("duration", "300"))
                    if not 1 <= duration <= 10080:
                        raise ValueError("Длительность: от 1 минуты до 7 дней")
                    job = enqueue_import(g.db, app.config["DATA"], upload,
                                         contest_id=int(cid) if cid else None, title=title,
                                         starts_at=starts_at, duration=duration)
                    flash(f"Импорт #{job} поставлен в очередь. Статус подготовки — ниже.", "success")
                    return redirect(url_for("admin"))
                else:
                    abort(400)
            except (ValueError, ImportError, sqlite3.IntegrityError) as exc:
                g.db.rollback()
                flash("Логин уже существует" if isinstance(exc, sqlite3.IntegrityError) else str(exc), "error")
            return redirect(url_for("admin"))
        return render_template("admin.html", contests=g.db.execute("SELECT * FROM contests ORDER BY id DESC").fetchall(),
                               users=g.db.execute("SELECT username,is_admin FROM users ORDER BY username").fetchall(),
                               imports=g.db.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 30").fetchall())

    @app.post("/submissions/<int:sid>/rejudge")
    def rejudge(sid):
        admin_only()
        get_submission(sid)
        g.db.execute("BEGIN IMMEDIATE")
        s = g.db.execute("SELECT verdict FROM submissions WHERE id=?", (sid,)).fetchone()
        if s["verdict"] in {"QUEUED", "RUNNING"}:
            g.db.rollback()
            abort(409, "Решение уже проверяется")
        g.db.execute("DELETE FROM test_results WHERE submission_id=?", (sid,))
        g.db.execute("UPDATE submissions SET verdict='QUEUED',claimed_by=NULL,finished_at=NULL,test_number=NULL,time_ms=NULL,memory_kb=NULL,compile_log='',internal_log='',score=NULL WHERE id=?", (sid,))
        g.db.commit()
        return redirect(url_for("submission", sid=sid))

    for code in (400, 403, 404, 409, 413, 429):
        app.register_error_handler(code, lambda e: (render_template("error.html", error=e), e.code))
    return app
