"""Persistent import jobs, processed under the same sandbox lock as submissions."""
import json
import time
import uuid
from pathlib import Path

from .polygon import import_archive


def enqueue_import(con, data, upload, **options):
    folder = Path(data) / 'imports'
    folder.mkdir(exist_ok=True)
    name = uuid.uuid4().hex + '.zip'
    path = folder / name
    try:
        upload.save(path)
        job = con.execute('INSERT INTO imports(archive,options,created_at) VALUES(?,?,?)',
                          (name, json.dumps(options), time.time())).lastrowid
        con.commit()
        return job
    except Exception:
        con.rollback()
        path.unlink(missing_ok=True)
        raise


def process_next_import(con, data, sandbox, worker_id):
    con.execute('BEGIN IMMEDIATE')
    job = con.execute("SELECT * FROM imports WHERE status='QUEUED' ORDER BY id LIMIT 1").fetchone()
    if job:
        con.execute("UPDATE imports SET status='RUNNING',claimed_by=?,message='Чтение пакета' WHERE id=?", (worker_id, job['id']))
    con.commit()
    if not job:
        return False
    def progress(message):
        con.execute('UPDATE imports SET message=? WHERE id=?', (message, job['id']))
        con.commit()
    path = Path(data) / 'imports' / job['archive']
    try:
        import_archive(con, data, path, sandbox=sandbox, progress=progress, job_id=job['id'], **json.loads(job['options']))
    except Exception as exc:
        con.rollback()
        con.execute("UPDATE imports SET status='FAILED',message=? WHERE id=?", (str(exc)[:4000], job['id']))
        con.commit()
    finally:
        path.unlink(missing_ok=True)
    return True
