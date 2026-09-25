"""Start a temporary Gunicorn server, test login/submission over HTTP, stop it."""
import http.cookiejar
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from judge.db import add_user, connect


def main():
    project = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="pcms-http-") as td:
        env = dict(os.environ, PCMS_DATA=td, PCMS_HTTPS="0")
        subprocess.run([sys.executable, "-m", "judge.cli", "init"], cwd=project, env=env, check=True, capture_output=True)
        with connect(td) as con:
            add_user(con, "smoke", "smoke-test-password")
        subprocess.run([sys.executable, "-m", "judge.cli", "import", "examples/demo-contest.zip"], cwd=project, env=env, check=True, capture_output=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with tempfile.TemporaryFile() as log:
            server = subprocess.Popen([sys.executable, "-m", "gunicorn", "--bind", f"127.0.0.1:{port}", "--workers", "1", "judge.web:create_app()"], cwd=project, env=env, stdout=log, stderr=log)
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                base = f"http://127.0.0.1:{port}"
                for _ in range(30):
                    try:
                        html = opener.open(base + "/login", timeout=1).read().decode()
                        break
                    except OSError:
                        time.sleep(0.2)
                else:
                    raise RuntimeError("HTTP server did not become ready")
                def csrf(page):
                    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)
                page = opener.open(base + "/login", urllib.parse.urlencode(dict(username="smoke", password="smoke-test-password", csrf=csrf(html))).encode(), timeout=5).read().decode()
                assert "Тренировка" in page
                page = opener.open(base + "/problems/1", timeout=5).read().decode()
                submitted = opener.open(base + "/problems/1", urllib.parse.urlencode(dict(csrf=csrf(page), language="python", source="print(sum(map(int,input().split())))")).encode(), timeout=5).read().decode()
                assert "QUEUED" in submitted
                assert b"QUEUED" in opener.open(base + "/api/submissions/1", timeout=5).read()
                print("HTTP smoke: Gunicorn, login, problem, submit, status API — OK")
            except Exception:
                log.seek(0)
                print(log.read().decode(), file=sys.stderr)
                raise
            finally:
                server.terminate()
                server.wait(timeout=10)


if __name__ == "__main__":
    main()
