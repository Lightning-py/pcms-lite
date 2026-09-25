import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from defusedxml import ElementTree as ET
from werkzeug.datastructures import FileStorage

from judge.db import initialize, connect, add_user, claim, standings
from judge.imports import enqueue_import, process_next_import
from judge.polygon import ImportError, import_archive
from judge.preparation import build_reference, build_checker, run_checker, reference_language, profiles
from judge.sandbox import Result, LANGUAGES
from judge.scoring import parse_scoring, calculate_score
from judge.worker import judge_submission
from tools.make_demo import problem_zip


def reference_package(kind='python.3', source=b'print(sum(map(int,input().split())))'):
    data = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(problem_zip())) as z, zipfile.ZipFile(data, 'w') as out:
        for name in z.namelist():
            if name.endswith('.a'):
                continue
            content = z.read(name)
            if name == 'problem.xml':
                content = content.replace(b'</assets>', ('<solutions><solution tag="main"><source path="solutions/ref" type="'+kind+'"/></solution></solutions></assets>').encode())
            out.writestr(name, content)
        out.writestr('solutions/ref', source)
    return data.getvalue()


class FakeSandbox:
    def __init__(self, failure=False):
        self.calls = []
        self.failure = failure

    def run(self, argv, files=None, input_bytes=b'', **kwargs):
        self.calls.append((argv, files, kwargs))
        if argv[0] == '/usr/bin/g++':
            return Result('OK', artifact=b'checker')
        if argv[0] == '/box/main':
            assert files['answer'] == files['output']
            return Result('OK', exit_code=0)
        assert set(files) == {'main.py'}
        return Result('RE' if self.failure else 'OK', stdout=str(sum(map(int, input_bytes.split()))).encode()+b'\n')


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        initialize(self.root)
        self.con = connect(self.root)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_answers_generated_only_in_sandbox_and_saved(self):
        sandbox = FakeSandbox()
        import_archive(self.con, self.root, io.BytesIO(reference_package()), sandbox=sandbox)
        p = self.con.execute('SELECT * FROM problems').fetchone()
        m = json.loads(p['manifest'])
        root = self.root/'packages'/p['package_dir']
        self.assertEqual((root/'tests/03.a').read_text(), '2000000000\n')
        self.assertEqual(len(sandbox.calls), 7)
        self.assertEqual(m['reference']['type'], 'python.3')

    def test_failure_rolls_back_entire_import(self):
        with self.assertRaisesRegex(ImportError, 'Эталон, тест 1'):
            import_archive(self.con, self.root, io.BytesIO(reference_package()), sandbox=FakeSandbox(True))
        self.assertEqual(self.con.execute('SELECT count(*) FROM contests').fetchone()[0], 0)
        self.assertEqual(list((self.root/'packages').iterdir()), [])

    def test_job_failure_records_context_and_removes_upload(self):
        upload = FileStorage(stream=io.BytesIO(reference_package()), filename='task.zip')
        enqueue_import(self.con, self.root, upload, title='Test')
        self.assertTrue(process_next_import(self.con, self.root, FakeSandbox(True), 0))
        job = self.con.execute('SELECT * FROM imports').fetchone()
        self.assertEqual(job['status'], 'FAILED')
        self.assertIn('Эталон, тест 1', job['message'])
        self.assertEqual(list((self.root/'imports').iterdir()), [])
        self.assertFalse(process_next_import(self.con, self.root, FakeSandbox(), 1))

    def test_unknown_language_requires_admin_profile_and_does_not_expand_participant_languages(self):
        with self.assertRaisesRegex(ValueError, 'reference-languages.json'):
            reference_language('unknown.compiler')
        config = self.root/'profiles.json'
        config.write_text(json.dumps({'custom.language': {'source': 'main.xyz', 'run': ['/usr/bin/custom', 'main.xyz']}}))
        with patch.dict('os.environ', {'PCMS_REFERENCE_PROFILES': str(config)}):
            self.assertEqual(reference_language('custom.language'), 'custom.language')
        self.assertEqual(set(LANGUAGES), {'c', 'cpp', 'python'})

    def test_profile_access_error_is_explicit_not_silently_ignored(self):
        with patch('judge.preparation.Path.read_text', side_effect=PermissionError('denied')):
            with self.assertRaisesRegex(ValueError, 'права чтения файла'):
                profiles()
        with patch('judge.preparation.Path.read_text', side_effect=FileNotFoundError()):
            self.assertIn('python.3', profiles())
        with patch('judge.preparation.Path.read_text', return_value='[]'):
            with self.assertRaisesRegex(ValueError, 'JSON-объектом'):
                profiles()

    def test_java_renames_public_class_and_never_invokes_host_compiler(self):
        (self.root/'123.java').write_text('public class Example { public static void main(String[] a) {} }')
        class JavaSandbox:
            def run(inner, argv, files, **kwargs):
                self.assertIn('Example.java', files)
                self.assertIn('javac', files['build.py'].decode())
                self.assertEqual(kwargs['artifact'], 'program.jar')
                return Result('OK', artifact=b'compiled-jar')
        command, files = build_reference(JavaSandbox(), self.root, {'type': 'java21', 'path': '123.java'}, [])
        self.assertEqual(command[-1], 'Example')
        self.assertEqual(files, {'program.jar': b'compiled-jar'})

    def test_java_checker_carries_library_and_uses_testlib_framework(self):
        (self.root/'Check.java').write_text('public class Check {}')
        (self.root/'testlib4j.jar').write_bytes(b'library')
        class JavaSandbox:
            def run(inner, argv, files, **kwargs):
                self.assertEqual(files['library0.jar'], b'library')
                self.assertIn("'-cp', 'library0.jar'", files['build.py'].decode())
                return Result('OK', artifact=b'checker-jar')
        checker = build_checker(JavaSandbox(), self.root, {'checker':'Check.java', 'checker_type':'java8',
                                'checker_libraries':['testlib4j.jar'], 'headers':[]})
        self.assertEqual(checker[0][-2:], ['ru.ifmo.testlib.CheckerFramework','Check'])
        class Runner:
            def run(inner, argv, files, **kwargs):
                self.assertEqual(argv[-3:], ['input','output','answer'])
                self.assertEqual(files['answer'], b'42')
                self.assertEqual(files['program.jar'], b'checker-jar')
                return Result('RE',exit_code=1)
        self.assertEqual(run_checker(Runner(),checker,b'1',b'0',b'42').exit_code,1)

    def test_scoring_dependencies_each_test_and_cycles(self):
        ts = ET.fromstring('''<testset><groups>
          <group name="a" points-policy="each-test"/>
          <group name="b" points-policy="complete-group" points="60"><dependencies><dependency group="a"/></dependencies></group>
        </groups></testset>''')
        tests = [{'group':'a','points':'20'}, {'group':'a','points':'20'}, {'group':'b','points':'60'}]
        m = {'tests':tests, **parse_scoring(ts, tests)}
        self.assertEqual(m['max_score'], 100)
        self.assertEqual(calculate_score(m, ['AC','WA','AC']), 20)
        self.assertEqual(calculate_score(m, ['AC','AC','WA']), 40)
        self.assertEqual(calculate_score(m, ['AC','AC','AC']), 100)
        ts.find('groups/group').append(ET.fromstring('<dependencies><dependency group="b"/></dependencies>'))
        with self.assertRaisesRegex(ValueError, 'Цикл'):
            parse_scoring(ts, tests)

    def test_scored_worker_runs_after_failure_and_standings_uses_best_attempt(self):
        import_archive(self.con, self.root, io.BytesIO(problem_zip()))
        row = self.con.execute('SELECT * FROM problems').fetchone()
        m = json.loads(row['manifest'])
        for i,t in enumerate(m['tests']):
            t.update(group=str(i), points=0)
        m.update(scoring='points', max_score=100, groups={str(i): {'policy':'complete-group','points':p,'dependencies':[]} for i,p in enumerate([20,30,50])})
        self.con.execute('UPDATE problems SET manifest=?', (json.dumps(m),))
        uid = add_user(self.con, 'alice', 'long-password')
        self.con.execute("INSERT INTO submissions(user_id,problem_id,language,source,created_at) VALUES(?,1,'python','print(0)',0)", (uid,))
        self.con.commit()
        class Judge:
            n = 0
            def run(inner, argv, files=None, input_bytes=b'', **kwargs):
                if argv[0] == '/usr/bin/g++': return Result('OK', artifact=b'checker')
                if argv[0] == '/usr/bin/python3': return Result('OK', stdout=b'0')
                inner.n += 1
                return Result('RE' if inner.n == 1 else 'OK', exit_code=1 if inner.n == 1 else 0)
        judge_submission(self.con, self.root, Judge(), claim(self.con,0))
        s = self.con.execute('SELECT * FROM submissions').fetchone()
        self.assertEqual((s['verdict'],s['score'],s['test_number']), ('WA',80,3))
        self.assertEqual(self.con.execute('SELECT count(*) FROM test_results').fetchone()[0],3)
        self.con.execute("INSERT INTO submissions(user_id,problem_id,language,source,created_at,verdict,score) VALUES(?,1,'python','x',1,'WA',20)", (uid,))
        self.con.commit()
        _, people = standings(self.con,self.con.execute('SELECT * FROM contests').fetchone())
        self.assertEqual(people[0]['score'],80)

    def test_migration_preserves_old_rows_and_is_idempotent(self):
        self.con.execute('ALTER TABLE submissions DROP COLUMN score')
        self.con.commit()
        initialize(self.root)
        initialize(self.root)
        self.assertIn('score', [r[1] for r in self.con.execute('PRAGMA table_info(submissions)')])
