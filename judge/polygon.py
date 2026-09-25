"""Offline import of full Polygon packages. Never executes package scripts."""
import json
import re
import shutil
import stat
import tempfile
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException


class ImportError(ValueError):
    pass


def relative_path(value):
    if not value or "\\" in value or ":" in value or "\x00" in value:
        raise ImportError(f"Недопустимый путь в пакете: {value!r}")
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in {"..", "."} for x in value.split("/")):
        raise ImportError(f"Путь выходит за пределы пакета: {value!r}")
    return p.as_posix()


def xml(path):
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ImportError("XML-дескриптор слишком большой")
    try:
        return ET.parse(path, forbid_dtd=True).getroot()
    except (ET.ParseError, DefusedXmlException) as exc:
        raise ImportError(f"Некорректный XML: {path.name}") from exc


class ExtractionBudget:
    def __init__(self):
        self.bytes = 0
        self.files = 0


def extract(archive, target, budget):
    try:
        with zipfile.ZipFile(archive) as z:
            seen = set()
            for info in z.infolist():
                name = relative_path(info.filename.rstrip("/"))
                if name in seen:
                    raise ImportError(f"Повторяющийся путь в ZIP: {name}")
                seen.add(name)
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}):
                    raise ImportError("Ссылки и специальные файлы в ZIP запрещены")
                budget.files += 1
                budget.bytes += info.file_size
                if budget.files > 30000 or budget.bytes > 2 * 1024**3 or info.file_size > 256 * 1024**2:
                    raise ImportError("Превышен лимит распаковки (2 ГиБ / 30000 файлов / 256 МиБ на файл)")
                dest = target / name
                if info.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, dest.open("xb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise ImportError(f"Не удалось распаковать ZIP: {exc}") from exc


def require_file(root, value):
    name = relative_path(value)
    if not (root / name).is_file():
        raise ImportError(f"В полном пакете отсутствует файл {name}")
    return name


def pattern_path(pattern, index):
    if len(re.findall(r"%0?[1-9]?d", pattern)) != 1 or "%" in re.sub(r"%0?[1-9]?d", "", pattern):
        raise ImportError(f"Неподдерживаемый шаблон тестов: {pattern}")
    return pattern % index


def integer(node, tag, low, high):
    try:
        value = int(node.findtext(tag, ""))
    except ValueError as exc:
        raise ImportError(f"Отсутствует или некорректно поле {tag}") from exc
    if not low <= value <= high:
        raise ImportError(f"Поле {tag} вне диапазона {low}…{high}")
    return value


def parse_problem(root):
    doc = xml(root / "problem.xml")
    if doc.tag != "problem":
        raise ImportError("Ожидался дескриптор <problem>")
    judging = doc.find("judging")
    if judging is None:
        raise ImportError("Отсутствует секция judging")
    if doc.find("assets/interactor") is not None or judging.get("interactive", "false") == "true":
        raise ImportError("Интерактивные задачи пока не поддерживаются")
    sets = judging.findall("testset")
    if len(sets) != 1:
        raise ImportError("В первой версии поддерживается ровно один testset")
    ts = sets[0]
    count = integer(ts, "test-count", 1, 2000)
    declared = ts.findall("tests/test")
    if len(declared) != count:
        raise ImportError("test-count не совпадает с количеством элементов test")
    if ts.find("groups") is not None or any(t.get("group") or t.get("points") for t in declared):
        raise ImportError("Баллы и группы тестов пока не поддерживаются; нужен формат AC/WA")
    inputs = ts.findtext("input-path-pattern", "")
    answers = ts.findtext("answer-path-pattern", "")
    tests = [{"input": require_file(root, pattern_path(inputs, i)),
              "answer": require_file(root, pattern_path(answers, i))} for i in range(1, count + 1)]
    checker = doc.find("assets/checker")
    if checker is None or checker.get("type") != "testlib":
        raise ImportError("Требуется чекер типа testlib с исходником C++")
    source = checker.find("source")
    if source is None or not source.get("type", "").startswith("cpp"):
        raise ImportError("Чекер должен иметь исходник C++")
    checker_path = require_file(root, source.get("path", ""))
    # Headers are compiler inputs, never host-executed files.
    headers = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p.suffix in {".h", ".hpp"}]
    if not any(Path(p).name == "testlib.h" for p in headers):
        raise ImportError("В пакет необходимо включить testlib.h")
    names = doc.findall("names/name")
    name = next((x.get("value") for x in names if x.get("language") == "russian"), None)
    title = name or next((x.get("value") for x in names if x.get("value")), doc.get("short-name", "Задача"))
    io_names = {}
    for attr in ("input-file", "output-file"):
        value = judging.get(attr, "")
        if value:
            value = relative_path(value)
            if "/" in value or value in {"main", "main.py", ".pcms-input", ".pcms-stdout", ".pcms-stderr"}:
                raise ImportError(f"Неподдерживаемое имя файла ввода/вывода: {value}")
        io_names[attr] = value
    if io_names["input-file"] and io_names["input-file"] == io_names["output-file"]:
        raise ImportError("Файлы ввода и вывода должны различаться")
    statements = []
    public_files = set()
    for s in doc.findall("statements/statement"):
        if s.get("type") not in {"text/html", "application/pdf"}:
            continue
        p = require_file(root, s.get("path", ""))
        # Public assets must live inside a dedicated statement directory.
        if not p.startswith("statements/"):
            continue
        statements.append({"path": p, "language": s.get("language", ""), "type": s.get("type")})
        public_files.add(p)
        for f in (root / p).parent.rglob("*"):
            if f.is_file() and f.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".css", ".webp"}:
                public_files.add(f.relative_to(root).as_posix())
    return {"title": title, "short_name": doc.get("short-name", ""), "polygon_url": doc.get("url", ""),
            "revision": doc.get("revision", ""), "time_ms": integer(ts, "time-limit", 1, 60000),
            "memory_kb": (integer(ts, "memory-limit", 1024 * 1024, 2 * 1024**3) + 1023) // 1024,
            "tests": tests, "checker": checker_path, "headers": headers,
            "statements": statements, "public_files": sorted(public_files), **io_names}


def letter(index):
    result = ""
    while index >= 0:
        result = chr(65 + index % 26) + result
        index = index // 26 - 1
    return result


def import_archive(con, data, archive, contest_id=None, title="Импорт Polygon", starts_at=None, duration=300):
    """All-or-nothing DB import; local contest.xml URLs match packaged problem URLs."""
    data = Path(data)
    moved = []
    try:
        with tempfile.TemporaryDirectory(prefix="polygon-") as tmp:
            stage = Path(tmp)
            budget = ExtractionBudget()
            extract(archive, stage / "outer", budget)
            # Contest bundle may contain full packages as ZIP files, one nesting level.
            nested = sorted((stage / "outer").rglob("*.zip"))
            for n, z in enumerate(nested):
                if len(nested) > 100:
                    raise ImportError("Слишком много вложенных пакетов")
                extract(z, stage / f"nested-{n}", budget)
            roots = sorted(p.parent for p in stage.rglob("problem.xml"))
            if not roots or len(roots) > 100:
                raise ImportError("Ожидается от 1 до 100 полных пакетов с problem.xml")
            parsed = [(p, parse_problem(p)) for p in roots]
            descriptors = list((stage / "outer").rglob("contest.xml"))
            if len(descriptors) > 1:
                raise ImportError("В архиве несколько contest.xml")
            ordered = []
            if descriptors:
                descriptor = xml(descriptors[0])
                if descriptor.tag != "contest":
                    raise ImportError("Ожидался дескриптор <contest>")
                for problem in descriptor.findall("problems/problem"):
                    label = problem.get("index", "")
                    url = problem.get("url", "")
                    local = problem.get("path", "")
                    if local:
                        local = relative_path(local).rstrip("/")
                    matches = [(p, m) for p, m in parsed if (
                        (url and m["polygon_url"].rstrip("/") == url.rstrip("/")) or
                        (url and m["short_name"] == urlparse(url).path.rstrip("/").split("/")[-1]) or
                        (local and (descriptors[0].parent / local).resolve() in {p.resolve(), (p / "problem.xml").resolve()})
                    )]
                    if len(matches) != 1:
                        raise ImportError(f"Для задачи {label} не найден единственный полный пакет. Добавьте ZIP из Polygon; ссылки без тестов недостаточно")
                    ordered.append((label, *matches[0]))
                if not ordered or len({p for _, p, _ in ordered}) != len(parsed) or len(ordered) != len(parsed):
                    raise ImportError("Список contest.xml должен однозначно соответствовать всем пакетам")
            else:
                ordered = [(letter(i), p, m) for i, (p, m) in enumerate(parsed)]
            labels = [x[0] for x in ordered]
            if len(set(labels)) != len(labels) or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,12}", l) for l in labels):
                raise ImportError("Некорректные или повторяющиеся обозначения задач")
            con.execute("BEGIN IMMEDIATE")
            if contest_id is None:
                contest_id = con.execute("INSERT INTO contests(title,starts_at,duration_minutes,created_at) VALUES(?,?,?,?)",
                                         (title[:200], starts_at, duration, time.time())).lastrowid
            elif not con.execute("SELECT id FROM contests WHERE id=?", (contest_id,)).fetchone():
                raise ImportError("Контест не найден")
            else:
                existing = {r[0] for r in con.execute("SELECT label FROM problems WHERE contest_id=?", (contest_id,))}
                if not descriptors:
                    offset = 0
                    relabeled = []
                    for _, p, m in ordered:
                        while letter(offset) in existing:
                            offset += 1
                        relabeled.append((letter(offset), p, m))
                        offset += 1
                    ordered = relabeled
                elif existing.intersection(labels):
                    raise ImportError("Обозначения задач уже заняты в контесте")
            for label, root, manifest in ordered:
                key = uuid.uuid4().hex
                dest = data / "packages" / key
                moved.append(dest)
                shutil.copytree(root, dest)
                con.execute("INSERT INTO problems(contest_id,label,title,package_dir,manifest) VALUES(?,?,?,?,?)",
                            (contest_id, label, manifest["title"], key, json.dumps(manifest, ensure_ascii=False)))
            con.commit()
            return contest_id
    except Exception:
        con.rollback()
        for path in moved:
            shutil.rmtree(path, ignore_errors=True)
        raise
