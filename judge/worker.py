import fcntl
import json
import logging
import time
from pathlib import Path

from .db import claim, connect
from .sandbox import Isolate, SandboxError, compile_source
from .scoring import calculate_score
from .preparation import build_checker, run_checker

LOG = logging.getLogger(__name__)


def judge_submission(con, data, sandbox, submission):
    sid = submission["id"]
    problem = con.execute("SELECT * FROM problems WHERE id=?", (submission["problem_id"],)).fetchone()
    manifest = json.loads(problem["manifest"])
    root = Path(data) / "packages" / problem["package_dir"]
    compile_result = compile_source(sandbox, submission["language"], submission["source"].encode())
    if compile_result.verdict != "OK":
        con.execute("UPDATE submissions SET verdict='CE',compile_log=?,finished_at=? WHERE id=?",
                    ((compile_result.stderr or compile_result.stdout).decode(errors="replace")[:16384], time.time(), sid))
        con.commit()
        return
    checker = build_checker(sandbox, root, manifest)
    language = submission["language"]
    solution_name = "main.py" if language == "python" else "main"
    command = ["/usr/bin/python3", "-I", "main.py"] if language == "python" else ["/box/main"]
    max_time = max_memory = 0
    final = "AC"
    last_test = None
    verdicts = []
    scored = manifest.get("scoring") == "points"
    for number, test in enumerate(manifest["tests"], 1):
        input_bytes = (root / test["input"]).read_bytes()
        result = sandbox.run(command, {solution_name: compile_result.artifact}, input_bytes,
                             time_ms=manifest["time_ms"], memory_kb=manifest["memory_kb"],
                             input_name=manifest["input-file"], output_name=manifest["output-file"])
        max_time = max(max_time, result.time_ms)
        max_memory = max(max_memory, result.memory_kb)
        verdict = result.verdict
        if verdict == "OK":
            # Answer exists only in the checker sandbox, never in the solution sandbox.
            checked = run_checker(sandbox, checker, input_bytes, result.stdout, (root / test["answer"]).read_bytes())
            if checked.verdict in {"OK", "RE"} and checked.exit_code in {0, 1, 2, 4, 8}:
                verdict = {0: "AC", 1: "WA", 2: "PE", 4: "PE", 8: "PE"}[checked.exit_code]
            else:
                raise SandboxError(f"Чекер завершился с кодом {checked.exit_code}: " + checked.stderr.decode(errors="replace"))
        con.execute("INSERT INTO test_results VALUES(?,?,?,?,?)", (sid, number, verdict, result.time_ms, result.memory_kb))
        con.execute("UPDATE submissions SET test_number=?,time_ms=?,memory_kb=? WHERE id=?", (number, max_time, max_memory, sid))
        con.commit()
        last_test = number
        verdicts.append(verdict)
        if verdict != "AC":
            if final == "AC":
                final = verdict
            if not scored:
                break
    score = calculate_score(manifest, verdicts) if scored else None
    con.execute("UPDATE submissions SET score=? WHERE id=?", (score, sid))
    con.execute("UPDATE submissions SET verdict=?,test_number=?,time_ms=?,memory_kb=?,finished_at=? WHERE id=?",
                (final, last_test, max_time, max_memory, time.time(), sid))
    con.commit()


def work(data, box_id=0, once=False):
    data = Path(data)
    # One OS lock per sandbox ID prevents two workers sharing a sandbox.
    with (data / f"worker-{box_id}.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SandboxError(f"Worker {box_id} уже запущен") from exc
        sandbox = Isolate(box_id)
        sandbox.cleanup()  # recover the sandbox left by a terminated worker
        sandbox.check()  # fail before claiming work when isolation is unavailable
        with connect(data) as con:
            con.execute("UPDATE submissions SET verdict='QUEUED',claimed_by=NULL,test_number=NULL,time_ms=NULL,memory_kb=NULL WHERE verdict='RUNNING' AND claimed_by=?", (box_id,))
            con.execute("DELETE FROM test_results WHERE submission_id IN (SELECT id FROM submissions WHERE verdict='QUEUED')")
            con.execute("UPDATE imports SET status='QUEUED',message='Повтор после перезапуска' WHERE status='RUNNING' AND claimed_by=?", (box_id,))
            con.commit()
            while True:
                from .imports import process_next_import
                if process_next_import(con, data, sandbox, box_id):
                    if once:
                        return
                    continue
                submission = claim(con, box_id)
                if submission:
                    try:
                        judge_submission(con, data, sandbox, submission)
                    except Exception as exc:
                        LOG.exception("Judging failed for submission %s", submission["id"])
                        con.rollback()
                        con.execute("UPDATE submissions SET verdict='JE',internal_log=?,finished_at=? WHERE id=?",
                                    (str(exc)[:16384], time.time(), submission["id"]))
                        con.commit()
                if once:
                    return
                if not submission:
                    time.sleep(1)
