import json
import os
import re
import sqlite3
import time
from pathlib import Path

from werkzeug.security import generate_password_hash

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
 password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS contests (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL,
 starts_at REAL, duration_minutes INTEGER NOT NULL DEFAULT 300,
 created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS problems (
 id INTEGER PRIMARY KEY, contest_id INTEGER NOT NULL REFERENCES contests(id),
 label TEXT NOT NULL, title TEXT NOT NULL, package_dir TEXT NOT NULL,
 manifest TEXT NOT NULL, UNIQUE(contest_id, label)
);
CREATE TABLE IF NOT EXISTS submissions (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
 problem_id INTEGER NOT NULL REFERENCES problems(id),
 language TEXT NOT NULL, source TEXT NOT NULL, created_at REAL NOT NULL,
 verdict TEXT NOT NULL DEFAULT 'QUEUED', claimed_by INTEGER,
 finished_at REAL, test_number INTEGER, time_ms INTEGER, memory_kb INTEGER,
 compile_log TEXT NOT NULL DEFAULT '', internal_log TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS queue_idx ON submissions(verdict, id);
CREATE INDEX IF NOT EXISTS user_submissions_idx ON submissions(user_id, id);
CREATE TABLE IF NOT EXISTS test_results (
 submission_id INTEGER NOT NULL REFERENCES submissions(id),
 number INTEGER NOT NULL, verdict TEXT NOT NULL,
 time_ms INTEGER NOT NULL, memory_kb INTEGER NOT NULL,
 PRIMARY KEY(submission_id, number)
);
CREATE TABLE IF NOT EXISTS moderation_log (
 id INTEGER PRIMARY KEY, submission_id INTEGER NOT NULL REFERENCES submissions(id),
 admin_id INTEGER NOT NULL REFERENCES users(id), action TEXT NOT NULL,
 reason TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (
 id INTEGER PRIMARY KEY, archive TEXT NOT NULL, options TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'QUEUED', claimed_by INTEGER,
 message TEXT NOT NULL DEFAULT '', result_contest INTEGER, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
 address TEXT PRIMARY KEY, window_start REAL NOT NULL, count INTEGER NOT NULL
);
"""


def data_dir():
    return Path(os.environ.get("PCMS_DATA", "data")).resolve()


def connect(root=None):
    con = sqlite3.connect(Path(root or data_dir()) / "judge.sqlite3", timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def initialize(root=None):
    root = Path(root or data_dir())
    root.mkdir(parents=True, exist_ok=True)
    (root / "packages").mkdir(exist_ok=True)
    with connect(root) as con:
        con.executescript(SCHEMA)
        if 'score' not in {r[1] for r in con.execute('PRAGMA table_info(submissions)')}:
            con.execute('ALTER TABLE submissions ADD COLUMN score REAL')
        columns = {r[1] for r in con.execute('PRAGMA table_info(submissions)')}
        for name, kind in [('manual_verdict', 'TEXT'), ('manual_score', 'REAL')]:
            if name not in columns:
                con.execute(f'ALTER TABLE submissions ADD COLUMN {name} {kind}')


def add_user(con, username, password, admin=False):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", username):
        raise ValueError("Логин: 1–40 латинских букв, цифр или символов _ . -")
    if not 10 <= len(password) <= 256:
        raise ValueError("Пароль должен содержать от 10 до 256 символов")
    return con.execute(
        "INSERT INTO users(username,password_hash,is_admin) VALUES(?,?,?)",
        (username, generate_password_hash(password), int(admin)),
    ).lastrowid


def contest_open(contest, now=None):
    now = time.time() if now is None else now
    return contest["starts_at"] is None or (
        contest["starts_at"] <= now < contest["starts_at"] + contest["duration_minutes"] * 60
    )


def claim(con, worker_id):
    # BEGIN IMMEDIATE serializes the read+write across independent worker processes.
    con.execute("BEGIN IMMEDIATE")
    row = con.execute("SELECT * FROM submissions WHERE verdict='QUEUED' ORDER BY id LIMIT 1").fetchone()
    if row:
        con.execute("UPDATE submissions SET verdict='RUNNING',claimed_by=? WHERE id=?", (worker_id, row["id"]))
    con.commit()
    return row


def effective_submission(row):
    result = dict(row)
    result['automatic_verdict'] = result['verdict']
    if result.get('manual_verdict'):
        result['verdict'] = result['manual_verdict']
        result['score'] = result['manual_score']
    return result


def standings(con, contest):
    problems = con.execute("SELECT * FROM problems WHERE contest_id=? ORDER BY id", (contest["id"],)).fetchall()
    rows = con.execute("""SELECT s.*, u.username FROM submissions s
        JOIN users u ON u.id=s.user_id JOIN problems p ON p.id=s.problem_id
        WHERE p.contest_id=? AND u.is_admin=0 ORDER BY s.created_at,s.id""", (contest["id"],)).fetchall()
    rows = [effective_submission(row) for row in rows]
    scored = any(json.loads(p['manifest']).get('scoring') == 'points' for p in problems)
    if scored:
        people = {}
        maxima = {p['id']: json.loads(p['manifest']).get('max_score') or 100 for p in problems}
        for s in rows:
            if contest['starts_at'] is not None and not contest_open(contest, s['created_at']):
                continue
            person = people.setdefault(s['user_id'], {'username': s['username'], 'score': 0, 'cells': {}})
            cell = person['cells'].setdefault(s['problem_id'], {'score': 0})
            value = s['score'] if s['score'] is not None else (maxima[s['problem_id']] if s['verdict'] == 'AC' else 0)
            if s['verdict'] not in {'JE', 'RUNNING', 'QUEUED'}:
                cell['score'] = max(cell['score'], value)
        for person in people.values():
            person['score'] = sum(cell['score'] for cell in person['cells'].values())
        return problems, sorted(people.values(), key=lambda p: (-p['score'], p['username']))
    people = {}
    for s in rows:
        if contest["starts_at"] is not None and not contest_open(contest, s["created_at"]):
            continue
        person = people.setdefault(s["user_id"], {"username": s["username"], "solved": 0, "penalty": 0, "cells": {}})
        cell = person["cells"].setdefault(s["problem_id"], {"accepted": False, "wrong": 0, "pending": False, "minute": None})
        if cell["accepted"]:
            continue
        if s["verdict"] == "AC":
            cell["accepted"] = True
            cell["pending"] = False
            minute = int((s["created_at"] - contest["starts_at"]) / 60) if contest["starts_at"] is not None else 0
            cell["minute"] = minute
            person["solved"] += 1
            person["penalty"] += minute + 20 * cell["wrong"] if contest["starts_at"] is not None else 0
        elif s["verdict"] in {"WA", "PE", "RE", "TLE", "MLE", "OLE"}:
            cell["wrong"] += 1
        elif s["verdict"] in {"RUNNING", "QUEUED"}:
            cell["pending"] = True
    return problems, sorted(people.values(), key=lambda p: (-p["solved"], p["penalty"], p["username"]))
