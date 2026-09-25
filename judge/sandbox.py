"""The only execution backend. There is deliberately no unsandboxed fallback."""
import os
import shutil
import signal
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

OUTPUT_LIMIT = 16 * 1024 * 1024


class SandboxError(RuntimeError):
    pass


def read_regular(path, limit=OUTPUT_LIMIT):
    """Do not follow links or block on a FIFO created by the submitted program."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        s = os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_size > limit or s.st_nlink != 1:
            raise SandboxError("Недопустимый выходной файл песочницы")
        return f.read(limit + 1)


@dataclass
class Result:
    verdict: str
    time_ms: int = 0
    memory_kb: int = 0
    exit_code: int = 0
    stdout: bytes = b""
    stderr: bytes = b""
    artifact: bytes = b""


def verdict_from_meta(meta):
    if meta.get("status") == "XX":
        raise SandboxError(meta.get("message", "Внутренняя ошибка isolate"))
    if "cg-oom-killed" in meta:
        return "MLE"
    if meta.get("status") == "TO":
        return "TLE"
    if meta.get("exitsig") == str(signal.SIGXFSZ):
        return "OLE"
    if meta.get("status") in {"RE", "SG"} or int(meta.get("exitcode", "0")) != 0:
        return "RE"
    return "OK"


class Isolate:
    def __init__(self, box_id=0):
        self.executable = shutil.which(os.environ.get("PCMS_ISOLATE", "isolate"))
        if not self.executable:
            raise SandboxError("isolate не установлен. Запуск решений без песочницы запрещён")
        self.box_id = box_id
        if not 0 <= box_id <= 999:
            raise ValueError("box-id должен быть от 0 до 999")

    def command(self, *args):
        return [self.executable, f"--box-id={self.box_id}", "--cg", *args]

    def cleanup(self):
        proc = subprocess.run(self.command("--cleanup"), capture_output=True, timeout=30)
        if proc.returncode:
            raise SandboxError("Не удалось очистить isolate: " + proc.stderr.decode(errors="replace")[:1000])

    def run(self, argv, files=None, input_bytes=b"", time_ms=1000, memory_kb=262144,
            processes=1, artifact=None, input_name="", output_name=""):
        # Disk quotas bound total writable disk usage, not only individual files.
        init = subprocess.run(self.command("--quota=262144,4096", "--init"), capture_output=True, text=True, timeout=30)
        if init.returncode:
            raise SandboxError("isolate init: " + init.stderr[:2000])
        base = Path(init.stdout.strip())
        box = base / "box"
        if not base.is_absolute() or not box.is_dir():
            raise SandboxError("isolate вернул некорректный путь песочницы")
        try:
            for name, content in (files or {}).items():
                if Path(name).is_absolute() or ".." in Path(name).parts:
                    raise SandboxError("Некорректный путь входного файла")
                dest = box / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                for parent in dest.parents:
                    if parent == box:
                        break
                    parent.chmod(0o755)
                dest.write_bytes(content)
                dest.chmod(0o755 if name == "main" else 0o644)
            (box / (input_name or ".pcms-input")).write_bytes(input_bytes)
            (box / (input_name or ".pcms-input")).chmod(0o644)
            if input_name:
                (box / ".pcms-input").write_bytes(b"")
                (box / ".pcms-input").chmod(0o644)
            seconds = time_ms / 1000
            wall = seconds * 3 + 2
            with tempfile.TemporaryDirectory(prefix="pcms-meta-") as td:
                meta_file = Path(td) / "meta"
                # Ubuntu OpenJDK links its public runtime configuration into /etc.
                # Expose only this directory read-only, never all of /etc.
                java_config = Path('/etc/java-21-openjdk')
                runtime_dirs = [f'--dir=etc/java-21-openjdk={java_config}'] if java_config.is_dir() else []
                cmd = self.command(f"--meta={meta_file}", f"--time={seconds}", f"--wall-time={wall}",
                                   f"--cg-mem={memory_kb}", f"--processes={processes}", "--open-files=64",
                                   f"--fsize={OUTPUT_LIMIT // 1024}", "--core=0", "--chdir=/box",
                                   "--stdin=.pcms-input", "--stdout=.pcms-stdout", "--stderr=.pcms-stderr",
                                   "--env=PATH=/usr/bin:/bin", "--env=LANG=C.UTF-8", "--env=HOME=/box",
                                   "--dir=etc=", "--dir=tmp=", "--dir=dev/shm=", "--env=TMPDIR=/box",
                                   *runtime_dirs, "--run", "--", *argv)
                try:
                    run = subprocess.run(cmd, capture_output=True, timeout=wall + 10)
                except subprocess.TimeoutExpired as exc:
                    raise SandboxError("isolate не завершился в установленный срок") from exc
                if run.returncode not in {0, 1} or not meta_file.exists():
                    raise SandboxError("isolate run: " + run.stderr.decode(errors="replace")[:2000])
                meta = dict(line.split(":", 1) for line in meta_file.read_text().splitlines() if ":" in line)
                verdict = verdict_from_meta(meta)
                if "time" not in meta or "cg-mem" not in meta:
                    raise SandboxError("isolate не предоставил время/память; проверьте cgroup v2 и ядро Linux >=5.19")
                result = Result(verdict, round(float(meta["time"]) * 1000), int(meta["cg-mem"]), int(meta.get("exitcode", "-1")))
            out = box / (output_name or ".pcms-stdout")
            try:
                if out.exists() or out.is_symlink():
                    result.stdout = read_regular(out)
                elif verdict == "OK":
                    result.verdict = "WA"
                err = box / ".pcms-stderr"
                if err.exists() or err.is_symlink():
                    result.stderr = read_regular(err)[:16384]
                if artifact and result.verdict == "OK":
                    result.artifact = read_regular(box / artifact)
            except (OSError, SandboxError):
                result.verdict = "RE"
            if len(result.stdout) >= OUTPUT_LIMIT:
                result.verdict = "OLE"
            return result
        finally:
            self.cleanup()

    def check(self):
        r = self.run(["/usr/bin/true"])
        if r.verdict != "OK":
            raise SandboxError("Самопроверка isolate не прошла")


LANGUAGES = {
    "c": {"title": "C17 · GCC", "source": "main.c", "compiler": "/usr/bin/gcc", "standard": "c17"},
    "cpp": {"title": "C++20 · GCC", "source": "main.cpp", "compiler": "/usr/bin/g++", "standard": "c++20"},
    "python": {"title": "Python 3", "source": "main.py"},
}


def compile_source(sandbox, language, source, extra_files=None, source_name=None, standard=None):
    lang = LANGUAGES[language]
    name = source_name or lang["source"]
    files = dict(extra_files or {})
    files[name] = source
    if language == "python":
        r = sandbox.run(["/usr/bin/python3", "-I", "-m", "py_compile", name], files,
                        time_ms=10000, memory_kb=524288)
        r.artifact = source if r.verdict == "OK" else b""
        return r
    include_dirs = sorted({str(Path(p).parent) for p in files})
    cmd = [lang["compiler"], f"-std={standard or lang['standard']}", "-O2", "-pipe", "-DONLINE_JUDGE", "./" + name, "-o", "main"]
    for path in include_dirs:
        cmd += ["-I", path]
    if language == "c":
        cmd += ["-lm"]
    return sandbox.run(cmd, files, time_ms=30000, memory_kb=1048576, processes=32, artifact="main")
