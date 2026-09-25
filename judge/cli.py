import argparse
import fcntl
import getpass
import logging
import os
import secrets

from .db import add_user, connect, data_dir, initialize


def main():
    parser = argparse.ArgumentParser(description="PCMS Lite")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("init", help="Создать БД и ключ сессий")
    user = subs.add_parser("user", help="Создать учётную запись")
    user.add_argument("username")
    user.add_argument("--admin", action="store_true")
    imp = subs.add_parser("import", help="Импортировать полный пакет Polygon или контест")
    imp.add_argument("archive")
    imp.add_argument("--title", default="Тренировка")
    imp.add_argument("--contest", type=int)
    serve = subs.add_parser("serve", help="Локальный сервер разработки")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    worker = subs.add_parser("worker", help="Проверяющий процесс; требует isolate с cgroup v2 и квотами")
    worker.add_argument("--box-id", type=int, default=0)
    worker.add_argument("--once", action="store_true")
    subs.add_parser("doctor", help="Проверить готовность isolate, компиляторов и Python")
    args = parser.parse_args()
    root = data_dir()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "init":
        initialize(root)
        key = root / "session.key"
        if not key.exists():
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(48))
        print(f"База данных: {root}")
    elif args.command == "user":
        password = getpass.getpass("Пароль (минимум 10 символов): ")
        if getpass.getpass("Повторите пароль: ") != password:
            parser.error("Пароли не совпадают")
        with connect(root) as con:
            add_user(con, args.username, password, args.admin)
        print("Пользователь создан")
    elif args.command == "import":
        from .polygon import import_archive
        from .sandbox import Isolate
        with (root / 'worker-997.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            class ImportSandbox:
                instance = None

                def run(self, *args, **kwargs):
                    if self.instance is None:
                        self.instance = Isolate(997)
                        self.instance.cleanup()
                        self.instance.check()
                    return self.instance.run(*args, **kwargs)

            sandbox = ImportSandbox()
            with connect(root) as con:
                cid = import_archive(con, root, args.archive, args.contest, args.title,
                                     sandbox=sandbox, progress=lambda message: print(message, flush=True))
        print(f"Импортирован контест {cid}")
    elif args.command == "serve":
        from .web import create_app
        create_app().run(host=args.host, port=args.port, debug=False)
    elif args.command == "worker":
        from .worker import work
        work(root, args.box_id, args.once)
    elif args.command == "doctor":
        from .sandbox import Isolate, compile_source
        sandbox = Isolate(999)
        sandbox.check()
        examples = {"c": b'#include <stdio.h>\nint main(){puts("42");}\n',
                    "cpp": b'#include <iostream>\nint main(){std::cout << 42;}\n',
                    "python": b'print(42)\n'}
        for lang, source in examples.items():
            compiled = compile_source(sandbox, lang, source)
            if compiled.verdict != "OK":
                raise RuntimeError(f"Компиляция {lang}: {compiled.stderr.decode(errors='replace')}")
            name = "main.py" if lang == "python" else "main"
            cmd = ["/usr/bin/python3", "-I", "main.py"] if lang == "python" else ["/box/main"]
            result = sandbox.run(cmd, {name: compiled.artifact})
            if result.verdict != "OK" or result.stdout.strip() != b"42":
                raise RuntimeError(f"Запуск {lang}: {result}")
            print(f"{lang}: OK")
        print("Проверка исполнения пройдена")


if __name__ == "__main__":
    main()
