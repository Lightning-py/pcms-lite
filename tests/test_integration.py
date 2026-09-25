"""Run only on a dedicated, configured Linux judge host:

PCMS_INTEGRATION=1 python -m unittest discover -s tests -p test_integration.py -v
Requires a free isolate box 998, cgroup v2, and working disk quotas.
"""
import io
import os
import tempfile
import time
import unittest
from pathlib import Path

from judge.db import add_user, claim, connect, initialize
from judge.polygon import import_archive
from judge.sandbox import Isolate
from judge.worker import judge_submission
from tools.make_demo import problem_zip


@unittest.skipUnless(os.environ.get("PCMS_INTEGRATION") == "1", "requires a configured isolate host")
class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        initialize(self.root)
        self.con = connect(self.root)
        with self.con:
            self.uid = add_user(self.con, "test", "integration-test-password")
        import_archive(self.con, self.root, io.BytesIO(problem_zip()))
        self.sandbox = Isolate(998)
        self.sandbox.check()

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def submit(self, language, source, expected):
        with self.con:
            self.con.execute("INSERT INTO submissions(user_id,problem_id,language,source,created_at) VALUES(?,1,?,?,?)",
                             (self.uid, language, source, time.time()))
        row = claim(self.con, 998)
        judge_submission(self.con, self.root, self.sandbox, row)
        actual = self.con.execute("SELECT * FROM submissions WHERE id=?", (row["id"],)).fetchone()
        self.assertEqual(actual["verdict"], expected, dict(actual))

    def test_c_accepted(self):
        self.submit("c", '#include <stdio.h>\nint main(){long long a,b;scanf("%lld%lld",&a,&b);printf("%lld\\n",a+b);}', "AC")

    def test_cpp_accepted(self):
        self.submit("cpp", '#include <iostream>\nint main(){long long a,b;std::cin>>a>>b;std::cout<<a+b;}', "AC")

    def test_python_accepted(self):
        self.submit("python", 'print(sum(map(int,input().split())))', "AC")

    def test_wrong_answer(self):
        self.submit("python", 'print(0)', "WA")

    def test_compile_error(self):
        self.submit("cpp", 'this is not C++', "CE")

    def test_python_syntax_error(self):
        self.submit("python", 'def broken(:', "CE")

    def test_time_limit(self):
        self.submit("cpp", 'int main(){for(;;){}}', "TLE")

    def test_runtime_error(self):
        self.submit("python", 'raise RuntimeError("test")', "RE")

    def test_output_limit(self):
        self.submit("c", '#include <stdio.h>\nint main(){for(;;)puts("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx");}', "OLE")

    def test_memory_limit(self):
        self.submit("python", 'a=bytearray(256*1024*1024)', "MLE")

    def test_no_host_secrets_or_network(self):
        secret = self.root / "secret.txt"
        secret.write_text("must not be visible")
        code = f'''import os, socket
assert not os.path.exists({str(secret)!r})
s=socket.socket()
s.settimeout(0.2)
try:
    s.connect(("1.1.1.1", 443))
except OSError:
    pass
else:
    raise RuntimeError("network escaped")
print(sum(map(int,input().split())))
'''
        self.submit("python", code, "AC")


if __name__ == "__main__":
    unittest.main()
