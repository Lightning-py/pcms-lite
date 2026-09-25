import io
import json
import os
import stat
import tempfile
import time
import unittest
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch
from pathlib import Path

from judge.db import add_user, claim, connect, initialize, standings
from judge.polygon import ImportError, import_archive
from judge.sandbox import Result, SandboxError, read_regular, verdict_from_meta
from judge.web import create_app
from judge.worker import judge_submission
from tools.make_demo import contest_zip, problem_zip


def change_zip(data, changes=None, remove=()):
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(buf, "w") as dest:
        for name in source.namelist():
            if name not in remove:
                dest.writestr(name, (changes or {}).get(name, source.read(name)))
        for name, content in (changes or {}).items():
            if name not in source.namelist():
                dest.writestr(name, content)
    return buf.getvalue()


class SystemTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        initialize(self.root)
        self.con = connect(self.root)
        with self.con:
            self.uid = add_user(self.con, "alice", "correct-password")
            self.other = add_user(self.con, "bob", "correct-password")
            self.admin = add_user(self.con, "admin", "correct-password", True)
        self.app = create_app({"TESTING": True, "DATA": self.root, "SECRET_KEY": "x" * 48})
        self.client = self.app.test_client()

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def load(self, data=None, **kwargs):
        return import_archive(self.con, self.root, io.BytesIO(data or problem_zip()), **kwargs)

    def login(self, username="alice"):
        self.client.get("/login")
        with self.client.session_transaction() as session:
            csrf = session["csrf"]
        response = self.client.post("/login", data={"username": username, "password": "correct-password", "csrf": csrf})
        self.assertEqual(response.status_code, 302)

    def post(self, path, data=None):
        with self.client.session_transaction() as session:
            csrf = session["csrf"]
        return self.client.post(path, data={"csrf": csrf, **(data or {})})

    def test_import_full_contest(self):
        cid = self.load(contest_zip())
        problems = self.con.execute("SELECT * FROM problems WHERE contest_id=? ORDER BY id", (cid,)).fetchall()
        self.assertEqual([p["label"] for p in problems], ["A", "B"])
        m = json.loads(problems[0]["manifest"])
        self.assertEqual((m["time_ms"], m["memory_kb"]), (1000, 65536))
        self.assertEqual((self.root / "packages" / problems[0]["package_dir"] / m["tests"][2]["answer"]).read_text(), "2000000000\n")

    def test_import_rollback_for_partial_package(self):
        bad = change_zip(problem_zip(), remove=["tests/02.a"])
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, "w") as z:
            z.writestr("good.zip", problem_zip())
            z.writestr("bad.zip", bad)
        with self.assertRaises(ImportError):
            self.load(bundle.getvalue())
        self.assertEqual(self.con.execute("SELECT count(*) FROM contests").fetchone()[0], 0)
        self.assertEqual(list((self.root / "packages").iterdir()), [])

    def test_zip_slip_symlink_and_xml_entities_rejected(self):
        with self.assertRaises(ImportError):
            self.load(change_zip(problem_zip(), {"../escape": "x"}))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            entry = zipfile.ZipInfo("link")
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(entry, "/etc/passwd")
        with self.assertRaises(ImportError):
            self.load(buf.getvalue())
        with self.assertRaises(ImportError):
            self.load(change_zip(problem_zip(), {"problem.xml": '<!DOCTYPE problem [<!ENTITY x SYSTEM "file:///etc/passwd">]><problem>&x;</problem>'}))

    def test_unsupported_interactive_and_unknown_groups_rejected(self):
        package = problem_zip()
        with zipfile.ZipFile(io.BytesIO(package)) as z:
            xml = z.read("problem.xml").decode()
        for altered in [xml.replace("<assets>", "<assets><interactor/>"), xml.replace('<test method="manual"/>', '<test method="manual" group="g1"/>').replace('</testset>', '<groups/></testset>')]:
            with self.assertRaises(ImportError):
                self.load(change_zip(package, {"problem.xml": altered}))

    def test_existing_contest_adds_next_label(self):
        cid = self.load()
        self.load(contest_id=cid)
        self.assertEqual([r[0] for r in self.con.execute("SELECT label FROM problems ORDER BY id")], ["A", "B"])

    def test_submission_auth_csrf_and_private_files(self):
        package = problem_zip()
        with zipfile.ZipFile(io.BytesIO(package)) as z:
            xml = z.read('problem.xml').decode().replace('</statements>', '<statement language="russian" type="application/pdf" path="statements/russian/problem.pdf"/></statements>')
        cid = self.load(change_zip(package, {'problem.xml':xml, 'statements/russian/problem.pdf': b'%PDF-1.4\n%%EOF'}))
        pid = self.con.execute("SELECT id FROM problems").fetchone()[0]
        self.assertEqual(self.client.get(f"/problems/{pid}").status_code, 302)
        self.login()
        self.assertEqual(self.client.get("/admin").status_code, 403)
        self.assertEqual(self.client.post(f"/problems/{pid}", data={"source": "print(1)", "language": "python"}).status_code, 400)
        response = self.post(f"/problems/{pid}", {"source": "print(sum(map(int,input().split())))", "language": "python"})
        self.assertEqual(response.status_code, 302)
        sid = self.con.execute("SELECT id FROM submissions").fetchone()[0]
        self.assertEqual(self.client.get(f"/submissions/{sid}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/submissions/{sid}").json["verdict"], "QUEUED")
        self.assertEqual(self.client.get(f"/problems/{pid}/statement/tests/01.a").status_code, 404)
        self.assertEqual(self.client.get(f"/problems/{pid}/statement/statements/russian/index.html").status_code, 404)
        page = self.client.get(f'/problems/{pid}').data
        self.assertNotIn(b'<iframe', page)
        self.assertNotIn(b'index.html', page)
        self.assertIn(b'problem.pdf', page)
        statement = self.client.get(f"/problems/{pid}/statement/statements/russian/problem.pdf")
        self.assertEqual(statement.status_code, 200)
        self.assertEqual(statement.mimetype, 'application/pdf')
        self.assertIn('attachment;', statement.headers['Content-Disposition'])
        self.assertIn("sandbox", statement.headers["Content-Security-Policy"])
        statement.close()
        self.login("bob")
        self.assertEqual(self.client.get(f"/submissions/{sid}").status_code, 403)
        self.assertEqual(self.client.get(f"/api/submissions/{sid}").status_code, 403)
        for path in ["/", f"/contests/{cid}", f"/contests/{cid}/submissions", f"/contests/{cid}/standings", f"/problems/{pid}"]:
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_future_contest_hides_statements_and_rejects_submission(self):
        cid = self.load(starts_at=time.time() + 3600)
        self.login()
        self.assertEqual(self.client.get(f"/contests/{cid}").status_code, 200)
        self.assertEqual(self.client.get("/problems/1").status_code, 403)
        self.assertEqual(self.client.get("/problems/1/statement/statements/russian/index.html").status_code, 403)
        self.assertEqual(self.post("/problems/1", {"source": "print(3)", "language": "python"}).status_code, 403)

    def test_admin_import_and_user_creation(self):
        self.login("admin")
        self.assertEqual(self.client.get("/admin").status_code, 200)
        response = self.post("/admin", {"action": "import", "title": "Test contest", "duration": "300", "archive": (io.BytesIO(contest_zip()), "contest.zip")})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.con.execute("SELECT count(*) FROM problems").fetchone()[0], 0)
        from judge.imports import process_next_import
        self.assertTrue(process_next_import(self.con, self.root, None, 0))
        self.assertEqual(self.con.execute("SELECT status FROM imports").fetchone()[0], 'DONE')
        self.assertEqual(self.con.execute("SELECT count(*) FROM problems").fetchone()[0], 2)
        self.post("/admin", {"action": "user", "username": "carol", "password": "another-long-password"})
        self.assertIsNotNone(self.con.execute("SELECT id FROM users WHERE username='carol'").fetchone())

    def test_claim_is_exclusive(self):
        self.load()
        with self.con:
            self.con.execute("INSERT INTO submissions(user_id,problem_id,language,source,created_at) VALUES(?,1,'python','print(3)',?)", (self.uid, time.time()))
        first = claim(self.con, 0)
        with connect(self.root) as other_con:
            second = claim(other_con, 1)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_icpc_scoring(self):
        start = time.time() - 3600
        self.load(starts_at=start)
        with self.con:
            for minute, verdict in [(1, "CE"), (2, "JE"), (3, "WA"), (10, "AC"), (11, "WA")]:
                self.con.execute("INSERT INTO submissions(user_id,problem_id,language,source,created_at,verdict) VALUES(?,1,'python','x',?,?)", (self.uid, start + minute * 60, verdict))
        contest = self.con.execute("SELECT * FROM contests").fetchone()
        _, people = standings(self.con, contest)
        self.assertEqual((people[0]["solved"], people[0]["penalty"]), (1, 30))

    def test_worker_failure_and_answer_isolation(self):
        self.load()
        with self.con:
            self.con.execute("INSERT INTO submissions(user_id,problem_id,language,source,created_at) VALUES(?,1,'python','print(0)',?)", (self.uid, time.time()))
        s = claim(self.con, 0)
        class FakeSandbox:
            def __init__(self):
                self.calls = []
            def run(self, argv, files=None, input_bytes=b"", **kwargs):
                self.calls.append((argv, files))
                if argv[0] == "/usr/bin/g++":
                    return Result("OK", artifact=b"checker")
                if "py_compile" in argv:
                    return Result("OK")
                if argv[0] == "/usr/bin/python3":
                    assert set(files) == {"main.py"}
                    return Result("OK", time_ms=25, memory_kb=10000, stdout=b"0\n")
                assert set(files) == {"main", "input", "output", "answer"}
                return Result("RE", exit_code=1)
        fake = FakeSandbox()
        judge_submission(self.con, self.root, fake, s)
        result = self.con.execute("SELECT * FROM submissions").fetchone()
        self.assertEqual((result["verdict"], result["test_number"]), ("WA", 1))
        self.assertEqual(len(fake.calls), 4)
        self.assertEqual(self.con.execute("SELECT count(*) FROM test_results").fetchone()[0], 1)

    def test_local_time_edit_and_scheduled_start(self):
        anchor = int(time.time() // 60) * 60
        cid = self.load(starts_at=anchor-3600, duration=1)
        self.login('admin')
        local = lambda value: datetime.fromtimestamp(value, ZoneInfo('Europe/Moscow')).strftime('%Y-%m-%dT%H:%M')
        self.post(f'/contests/{cid}/edit', {'title':'Restarted','starts_at':local(anchor),'duration':'60'})
        self.assertEqual(self.con.execute('SELECT starts_at FROM contests').fetchone()[0], anchor)
        edit = self.client.get(f'/contests/{cid}/edit').data
        self.assertIn(local(anchor).encode(), edit)
        self.assertIn(b'Europe/Moscow', edit)
        self.assertNotIn(b'data-start-delay', edit)
        future = anchor+120
        self.post(f'/contests/{cid}/edit', {'title':'Future','starts_at':local(future),'duration':'60'})
        self.login()
        with patch('judge.web.time.time', return_value=anchor+60):
            self.assertEqual(self.client.get('/problems/1').status_code,403)
            self.assertIn(b'data-start-delay="60000.0"', self.client.get(f'/contests/{cid}').data)
        with patch('judge.web.time.time', return_value=future):
            self.assertEqual(self.client.get('/problems/1').status_code,200)
            self.assertNotIn(b'data-start-delay', self.client.get(f'/contests/{cid}').data)
            self.assertEqual(self.post('/problems/1', {'language':'python','source':'print(3)'}).status_code,302)

    def test_web_import_uses_same_local_timezone(self):
        self.login('admin')
        self.post('/admin', {'action':'import','title':'Timed','starts_at':'2026-09-25T12:00', 'duration':'60','archive':(io.BytesIO(problem_zip()),'task.zip')})
        from judge.imports import process_next_import
        self.assertTrue(process_next_import(self.con,self.root,None,0))
        timestamp = datetime(2026,9,25,12,tzinfo=ZoneInfo('Europe/Moscow')).timestamp()
        self.assertEqual(self.con.execute('SELECT starts_at FROM contests').fetchone()[0],timestamp)

    def test_append_contest_with_colliding_labels_keeps_existing_problems(self):
        cid = self.load(contest_zip())
        old = [tuple(r) for r in self.con.execute('SELECT id,label,package_dir FROM problems ORDER BY id')]
        self.load(contest_zip(), contest_id=cid)
        rows = self.con.execute('SELECT id,label,package_dir FROM problems ORDER BY id').fetchall()
        self.assertEqual([r['label'] for r in rows], ['A','B','C','D'])
        self.assertEqual([tuple(r) for r in rows[:2]], old)

    def test_edit_contest_permissions_and_schedule_changes(self):
        cid = self.load(starts_at=time.time()-10000, duration=1)
        self.login()
        path = f'/contests/{cid}/edit'
        self.assertEqual(self.client.get(path).status_code,403)
        self.assertEqual(self.post(path, {'title':'Bad','duration':'10'}).status_code,403)
        self.login('admin')
        self.assertEqual(self.client.get(path).status_code,200)
        self.assertEqual(self.client.post(path,data={'title':'No csrf'}).status_code,400)
        self.assertEqual(self.post(path, {'title':'Renamed','starts_at':'','duration':'60'}).status_code,302)
        c = self.con.execute('SELECT * FROM contests').fetchone()
        self.assertEqual((c['title'], c['starts_at'], c['duration_minutes']),('Renamed',None,60))
        self.post(path, {'title':'Invalid','duration':'0'})
        self.assertEqual(self.con.execute('SELECT title FROM contests').fetchone()[0],'Renamed')
        self.post(path, {'title':'Future','starts_at':'2099-01-01T12:00','duration':'60'})
        self.login()
        self.assertEqual(self.client.get('/problems/1').status_code,403)

    def test_moderation_survives_worker_result_and_updates_standings(self):
        cid = self.load()
        with self.con:
            self.con.execute("INSERT INTO submissions(user_id,problem_id,language,source,created_at,verdict) VALUES(?,1,'python','print(0)',?,'RUNNING')", (self.uid,time.time()))
        path = '/submissions/1/moderate'
        self.login()
        self.assertEqual(self.post(path, {'action':'accept','reason':'x'}).status_code,403)
        self.login('admin')
        self.assertEqual(self.client.post(path,data={'action':'accept'}).status_code,400)
        self.assertEqual(self.post(path, {'action':'accept','reason':''}).status_code,400)
        self.assertEqual(self.post(path, {'action':'accept','reason':'Manual review'}).status_code,302)
        self.assertEqual(self.client.get('/api/submissions/1').json['verdict'],'AC')
        c = self.con.execute('SELECT * FROM contests').fetchone()
        self.assertEqual(standings(self.con,c)[1][0]['solved'],1)
        self.post(path, {'action':'ban','reason':'Invalid submission'})
        # The worker finishes after the administrator's action.
        with self.con:
            self.con.execute("UPDATE submissions SET verdict='AC' WHERE id=1")
        self.assertEqual(self.client.get('/api/submissions/1').json['verdict'],'BAN')
        self.assertEqual(standings(self.con,c)[1][0]['solved'],0)
        self.assertEqual(standings(self.con,c)[1][0]['penalty'],0)
        self.assertEqual(self.client.get('/submissions/1').status_code,200)
        self.assertEqual(self.client.get(f'/contests/{cid}/submissions').status_code,200)
        self.post(path, {'action':'restore','reason':'Restored'})
        self.assertEqual(self.client.get('/api/submissions/1').json['verdict'],'AC')
        self.assertEqual(self.con.execute('SELECT count(*) FROM moderation_log').fetchone()[0],3)
        self.login()
        self.assertIn(b'Restored', self.client.get('/submissions/1').data)

    def test_manual_accept_gives_full_points_and_rejudge_preserves_ban(self):
        cid = self.load()
        p = self.con.execute('SELECT * FROM problems').fetchone()
        manifest = json.loads(p['manifest'])
        manifest.update(scoring='points',max_score=75)
        with self.con:
            self.con.execute('UPDATE problems SET manifest=?',(json.dumps(manifest),))
            self.con.execute("INSERT INTO submissions(user_id,problem_id,language,source,created_at,verdict,score) VALUES(?,1,'python','print(0)',?,'WA',10)", (self.uid,time.time()))
        self.login('admin')
        self.post('/submissions/1/moderate', {'action':'accept','reason':'Accepted'})
        c=self.con.execute('SELECT * FROM contests').fetchone()
        self.assertEqual(standings(self.con,c)[1][0]['score'],75)
        self.post('/submissions/1/moderate', {'action':'ban','reason':'Banned'})
        self.assertEqual(standings(self.con,c)[1][0]['score'],0)
        self.post('/submissions/1/rejudge')
        self.assertEqual(self.client.get('/api/submissions/1').json['verdict'],'BAN')
        self.post('/submissions/1/moderate', {'action':'restore','reason':'Return to judging'})
        self.assertEqual(self.client.get('/api/submissions/1').json['verdict'],'QUEUED')

    def test_sandbox_metadata_and_unsafe_output(self):
        self.assertEqual(verdict_from_meta({"status": "TO"}), "TLE")
        self.assertEqual(verdict_from_meta({"status": "SG", "cg-oom-killed": "1"}), "MLE")
        self.assertEqual(verdict_from_meta({"exitcode": "1"}), "RE")
        with self.assertRaises(SandboxError):
            verdict_from_meta({"status": "XX"})
        (self.root / "secret").write_text("secret")
        (self.root / "link").symlink_to(self.root / "secret")
        with self.assertRaises(OSError):
            read_regular(self.root / "link")
        os.mkfifo(self.root / "fifo")
        with self.assertRaises(SandboxError):
            read_regular(self.root / "fifo")


if __name__ == "__main__":
    unittest.main()
