#!/usr/bin/env python3
"""Ubuntu installer. Run via sudo bash install.sh --domain judge.example.org."""
import argparse
import contextlib
import fcntl
import grp
import hashlib
import ipaddress
import json
import os
import pwd
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

MARKER = "# Managed by PCMS Lite installer v1"
APP = Path("/opt/pcms-lite")
DATA = Path("/var/lib/pcms-lite")
CONF = Path("/etc/pcms-lite")
RUNTIME = Path("/var/lib/pcms-lite-runtime")
BOXES = Path("/var/lib/pcmsboxes")
SITE = Path("/etc/nginx/conf.d/pcms-lite.conf")
UNITDIR = Path("/etc/systemd/system")
REV = "8f185bb37f3f23e29b33b0c7727c91c13429abe3"
SHA256 = "00ffa7006e79dac51d1bd1899224cac55c9875445f4926ee083eee938eaefbb9"
SOURCE = Path(__file__).resolve().parents[1]


def run(*args, capture=False, **kwargs):
    result = subprocess.run([str(a) for a in args], check=True, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None, **kwargs)
    return result.stdout if capture else ""


def atomic_write(path, text, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pcms-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            os.fchmod(f.fileno(), mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def require_owned(path):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError(f"Отказ: {path} — символическая ссылка")
    if path.exists() and not path.read_text().startswith(MARKER + "\n"):
        raise RuntimeError(f"Отказ: существующий файл {path} не создан этим установщиком")


def managed_write(path, content, mode=0o644):
    require_owned(path)
    atomic_write(path, MARKER + "\n" + content, mode)


def validate_domain(value):
    value = value.lower().rstrip(".")
    if not value or len(value) > 253:
        raise argparse.ArgumentTypeError("Некорректный домен или IPv4")
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in value.split(".")):
        raise argparse.ArgumentTypeError("Нужен домен без http://, пути, пробелов и порта")
    return value


def nginx_conf(domain, tls=False):
    ipv6 = Path("/proc/net/if_inet6").exists() and bool(Path("/proc/net/if_inet6").read_text().strip())
    listen6_http = "    listen [::]:80;\n" if ipv6 else ""
    listen6_tls = "    listen [::]:443 ssl;\n" if ipv6 else ""
    common = """    client_max_body_size 128m;
    location /.well-known/acme-challenge/ {
        root /var/lib/pcms-acme;
    }
    location / {
        proxy_pass http://unix:/run/pcms-lite/web.sock;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 180s;
    }
"""
    if not tls:
        return f"server {{\n    listen 80;\n{listen6_http}    server_name {domain};\n" + common + "}\n"
    return f"""server {{
    listen 80;
{listen6_http}    server_name {domain};
    location /.well-known/acme-challenge/ {{ root /var/lib/pcms-acme; }}
    location / {{ return 301 https://{domain}$request_uri; }}
}}
server {{
    listen 443 ssl;
{listen6_tls}    server_name {domain};
    ssl_certificate /etc/letsencrypt/live/pcms-lite/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/pcms-lite/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
{common}}}
"""


def check_nginx_domain(dump, domain, own_site=SITE):
    sections = re.split(r"(?m)^# configuration file (.+):\n", dump)
    for i in range(1, len(sections), 2):
        current, body = sections[i:i + 2]
        if current == str(own_site):
            continue
        body = "\n".join(line.split("#", 1)[0] for line in body.splitlines())
        for match in re.finditer(r"\bserver_name\s+([^;]+);", body):
            if domain in [s.strip("\"'").lower() for s in match.group(1).split()]:
                raise RuntimeError(f"Домен {domain} уже настроен в {current}. Выберите отдельный поддомен")


def publish_nginx(content, domain, site=SITE, command=run, backup_dir=None):
    """Only this one file is changed. On failed validation/reload restore its bytes."""
    site = Path(site)
    require_owned(site)
    old = site.read_bytes() if site.exists() else None
    if old is not None and backup_dir:
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "pcms-lite.conf.bak").write_bytes(old)
    try:
        check_nginx_domain(command("nginx", "-T", capture=True), domain, site)
        managed_write(site, content)
        command("nginx", "-t")
        dump = command("nginx", "-T", capture=True)
        if f"# configuration file {site}:" not in dump:
            raise RuntimeError(f"nginx не подключает {site}. Добавьте include /etc/nginx/conf.d/*.conf в http-блок вручную")
        # nginx emits server-name conflicts as warnings, despite exit status 0.
        check_nginx_domain(dump, domain, site)
        command("systemctl", "reload", "nginx")
    except BaseException:
        if old is None:
            site.unlink(missing_ok=True)
        else:
            atomic_write(site, old.decode())
        # Reload of an invalid config is atomic in nginx; restore disk config too.
        with contextlib.suppress(Exception):
            command("nginx", "-t")
            command("systemctl", "reload", "nginx")
        raise


def unit_files(https, workers, socket_group="www-data"):
    env = f"Environment=PCMS_DATA={DATA}\nEnvironment=PCMS_ISOLATE={APP}/runtime/isolate\n"
    units = {"pcms-web.service": f"""[Unit]
Description=PCMS Lite web
After=network.target
[Service]
User=pcms
Group={socket_group}
WorkingDirectory={APP}/current
{env}Environment=PCMS_HTTPS={int(https)}
RuntimeDirectory=pcms-lite
RuntimeDirectoryMode=0750
ExecStart={APP}/current/.venv/bin/gunicorn --bind unix:/run/pcms-lite/web.sock --umask 7 --workers 2 --timeout 180 judge.web:create_app()
Restart=on-failure
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths={DATA} /run/pcms-lite
ProtectHome=true
[Install]
WantedBy=multi-user.target
""", "pcms-worker@.service": f"""[Unit]
Description=PCMS Lite worker %i
After=pcms-isolate.service var-lib-pcmsboxes.mount
Requires=pcms-isolate.service var-lib-pcmsboxes.mount
[Service]
User=pcms
Group=pcms
WorkingDirectory={APP}/current
{env}ExecStart={APP}/current/.venv/bin/pcms worker --box-id %i
Restart=on-failure
RestartSec=5
UMask=0077
TimeoutStopSec=30
[Install]
WantedBy=multi-user.target
"""}
    return units


def preflight(args):
    if os.geteuid() != 0:
        raise RuntimeError("Запустите установщик через sudo")
    release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    if release.get("ID", "").strip('"') != "ubuntu" or float(release["VERSION_ID"].strip('"')) < 24.04:
        raise RuntimeError("Установщик рассчитан на Ubuntu 24.04 или новее")
    if not Path("/run/systemd/system").is_dir() or not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
        raise RuntimeError("Нужны запущенный systemd и cgroup v2")
    if not {"memory", "cpuset"} <= set(Path("/sys/fs/cgroup/cgroup.controllers").read_text().split()):
        raise RuntimeError("В cgroup v2 недоступны memory/cpuset; нужен полноценный Ubuntu-хост/VM")
    if shutil.which("nginx"):
        check_nginx_domain(run("nginx", "-T", capture=True), args.domain)
    require_owned(SITE)
    for name in ("pcms-web.service", "pcms-worker@.service", "pcms-isolate.service", "var-lib-pcmsboxes.mount"):
        require_owned(UNITDIR / name)
    if (APP.exists() or RUNTIME.exists()) and not (CONF / "installer.json").exists():
        raise RuntimeError("Каталог установки уже занят. Автоматическое принятие чужой установки запрещено")
    if args.email:
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", args.email):
            raise RuntimeError("Некорректный email")
        try:
            ipaddress.ip_address(args.domain)
        except ValueError:
            pass
        else:
            raise RuntimeError("Для автоматического HTTPS укажите DNS-имя")
    if not (RUNTIME / "sandboxes.ext4").exists() and shutil.disk_usage("/var/lib").free < (args.sandbox_gib + 2) * 1024**3:
        raise RuntimeError("Недостаточно места: нужен размер образа песочниц плюс минимум 2 ГиБ")


def sandbox_setup(args):
    image = RUNTIME / "sandboxes.ext4"
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    if not image.exists():
        if BOXES.is_mount() or (BOXES.exists() and any(BOXES.iterdir())):
            raise RuntimeError(f"Нельзя монтировать поверх непустого {BOXES}")
        pending = RUNTIME / "sandboxes.new"
        if pending.exists():
            raise RuntimeError(f"Обнаружен незавершённый образ {pending}; проверьте предыдущую установку")
        run("fallocate", "-l", f"{args.sandbox_gib}G", pending)
        pending.chmod(0o600)
        run("mkfs.ext4", "-F", "-O", "quota", pending)
        pending.rename(image)
    BOXES.mkdir(mode=0o755, exist_ok=True)
    managed_write(UNITDIR / "var-lib-pcmsboxes.mount", f"""[Unit]
Description=PCMS isolated sandbox volume
[Mount]
What={image}
Where={BOXES}
Type=ext4
Options=loop,usrquota,nodev,nosuid
[Install]
WantedBy=multi-user.target
""")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "var-lib-pcmsboxes.mount")
    config = CONF / "isolate.conf"
    if not config.exists():
        ownership = CONF / "sandbox-account-owned"
        try:
            pwd.getpwnam("pcms-sandbox")
        except KeyError:
            atomic_write(ownership, "PCMS Lite\n", 0o600)
            run("useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", "pcms-sandbox")
        else:
            if not ownership.exists():
                raise RuntimeError("Учётная запись pcms-sandbox уже занята")
        ranges = []
        for file in ("/etc/subuid", "/etc/subgid"):
            if Path(file).exists():
                for line in Path(file).read_text().splitlines():
                    if not line.strip():
                        continue
                    _, first, count = line.split(":")
                    ranges.append((int(first), int(first) + int(count)))
        for kind in ("passwd", "group"):
            ranges += [(int(l.split(":")[2]), int(l.split(":")[2]) + 1) for l in run("getent", kind, capture=True).splitlines()]
        assigned = []
        for file in ("/etc/subuid", "/etc/subgid"):
            entries = [line.split(":") for line in Path(file).read_text().splitlines() if line.startswith("pcms-sandbox:")] if Path(file).exists() else []
            assigned.append(entries)
        if not any(assigned):
            first = 4000000
            while any(a < first + 1000 and first < b for a, b in ranges):
                first += 1000
            run("usermod", "--add-subuids", f"{first}-{first+999}", "--add-subgids", f"{first}-{first+999}", "pcms-sandbox")
        elif not all(entries and sum(int(e[2]) for e in entries) >= 1000 for entries in assigned):
            raise RuntimeError("Неполное назначение subordinate UID/GID для pcms-sandbox; проверьте /etc/subuid и /etc/subgid")
        managed_write(config, f"box_root = {BOXES}\nlock_root = /run/pcms-isolate/locks\ncg_root = auto:/run/pcms-isolate/cgroup\nsubid_user = pcms-sandbox\nnum_boxes = 1000\n")
    else:
        require_owned(config)
    binary = APP / "runtime/isolate"
    if not binary.exists():
        with tempfile.TemporaryDirectory(prefix="pcms-isolate-build-") as td:
            archive = Path(td) / "isolate.tgz"
            with urllib.request.urlopen(f"https://codeload.github.com/ioi/isolate/tar.gz/{REV}", timeout=60) as response:
                archive.write_bytes(response.read(1024 * 1024))
            if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
                raise RuntimeError("Контрольная сумма исходников isolate не совпала")
            with tarfile.open(archive) as tar:
                tar.extractall(td, filter="data")
            source = Path(td) / f"isolate-{REV}"
            run("make", "-j2", f"CONFIG={config}", "isolate", "isolate-cg-keeper", cwd=source)
            binary.parent.mkdir(parents=True, exist_ok=True)
            run("install", "-m", "4755", source / "isolate", binary)
            run("install", "-m", "0755", source / "isolate-cg-keeper", binary.parent / "isolate-cg-keeper")
    managed_write(UNITDIR / "pcms-isolate.service", f"""[Unit]
Description=PCMS dedicated isolate cgroup keeper
Requires=var-lib-pcmsboxes.mount
After=var-lib-pcmsboxes.mount
[Service]
Type=notify
ExecStart={APP}/runtime/isolate-cg-keeper
Delegate=true
RuntimeDirectory=pcms-isolate
RuntimeDirectoryMode=0755
[Install]
WantedBy=multi-user.target
""")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "pcms-isolate.service")


def as_app(release, *args, https=False, capture=False):
    return run("runuser", "-u", "pcms", "--", "env", f"PCMS_DATA={DATA}",
               f"PCMS_ISOLATE={APP}/runtime/isolate", f"PCMS_HTTPS={int(https)}",
               str(release / ".venv/bin/python"), *args, cwd=release, capture=capture)


def install(args):
    preflight(args)
    CONF.mkdir(mode=0o700, exist_ok=True)
    state_path = CONF / "installer.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if state.get("domain", args.domain) != args.domain:
        raise RuntimeError("Для смены домена измените настройки отдельно; повторная установка сохраняет существующий домен")
    state.setdefault("domain", args.domain)
    atomic_write(state_path, json.dumps(state), 0o600)
    run("apt-get", "update")
    run("apt-get", "install", "-y", "--no-upgrade", "-o", "Dpkg::Options::=--force-confold", "python3-venv", "build-essential", "pkg-config", "libcap-dev", "libseccomp-dev", "libsystemd-dev", "nginx", "curl", "ca-certificates", "quota", "e2fsprogs", "util-linux")
    try:
        user = pwd.getpwnam("pcms")
        if user.pw_dir != str(DATA) or user.pw_shell != "/usr/sbin/nologin":
            raise RuntimeError("Существующая учётная запись pcms не принадлежит этой установке")
    except KeyError:
        run("useradd", "--system", "--user-group", "--home-dir", DATA, "--no-create-home", "--shell", "/usr/sbin/nologin", "pcms")
    run("install", "-d", "-m", "0700", "-o", "pcms", "-g", "pcms", DATA)
    APP.mkdir(exist_ok=True)
    sandbox_setup(args)
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    release = APP / "releases" / stamp
    release.mkdir(parents=True)
    release.chmod(0o755)
    for name in ("judge", "tests", "tools", "examples"):
        shutil.copytree(SOURCE / name, release / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("pyproject.toml", "requirements.lock", "README.md", "THIRD_PARTY.md"):
        shutil.copy2(SOURCE / name, release / name)
    # Source files copied from a root-only upload must still be readable by pcms.
    for path in release.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    run("python3", "-m", "venv", release / ".venv")
    run(release / ".venv/bin/pip", "install", "-r", release / "requirements.lock")
    run(release / ".venv/bin/pip", "install", "--no-deps", str(release))
    as_app(release, "-m", "judge.cli", "init")
    # Password is generated only if no administrator exists; it never resets one.
    admin_script = """from judge.db import connect,add_user
import secrets
with connect() as c:
 if not c.execute('SELECT 1 FROM users WHERE is_admin=1').fetchone():
  if c.execute('SELECT 1 FROM users WHERE username=?',('admin',)).fetchone():
   raise RuntimeError('Login admin already belongs to a participant')
  password=secrets.token_urlsafe(24)
  add_user(c,'admin',password,True)
  c.commit()
  print('admin\\n'+password)
"""
    credentials = as_app(release, "-c", admin_script, capture=True)
    if credentials.strip():
        atomic_write(CONF / "admin-credentials", credentials, 0o600)
    as_app(release, "-m", "judge.cli", "doctor")
    # Real compilation, runtime and isolation tests must pass before publishing.
    run("runuser", "-u", "pcms", "--", "env", "PCMS_INTEGRATION=1", f"PCMS_ISOLATE={APP}/runtime/isolate",
        release / ".venv/bin/python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_integration.py", "-v", cwd=release)
    https = bool(state.get("https", False))
    nginx_dump = run("nginx", "-T", capture=True)
    nginx_user = re.search(r"(?m)^\s*user\s+([a-zA-Z0-9_-]+)(?:\s+([a-zA-Z0-9_-]+))?\s*;", nginx_dump)
    if not nginx_user:
        raise RuntimeError("В nginx нет явной директивы user; невозможно безопасно определить права сокета")
    socket_group = nginx_user.group(2) or grp.getgrgid(pwd.getpwnam(nginx_user.group(1)).pw_gid).gr_name
    backup = CONF / "backups" / stamp
    backup.mkdir(parents=True, mode=0o700)
    current = APP / "current"
    previous = os.readlink(current) if current.is_symlink() else None
    previous_units = {}
    old_workers = state.get("workers", args.workers)
    services = ["pcms-web.service"] + [f"pcms-worker@{i}.service" for i in range(max(old_workers, args.workers))]
    old_site = SITE.read_text() if SITE.exists() else None
    published = False
    try:
        for name, body in unit_files(https, args.workers, socket_group).items():
            path = UNITDIR / name
            previous_units[path] = path.read_text() if path.exists() else None
            if path.exists():
                shutil.copy2(path, backup / name)
            managed_write(path, body)
        if previous:
            run("systemctl", "stop", *services)
        temporary_link = APP / ".current-new"
        temporary_link.unlink(missing_ok=True)
        temporary_link.symlink_to(release)
        os.replace(temporary_link, current)
        run("systemctl", "daemon-reload")
        active = ["pcms-web.service"] + [f"pcms-worker@{i}.service" for i in range(args.workers)]
        run("systemctl", "enable", *active)
        run("systemctl", "restart", *active)
        for _ in range(30):
            try:
                run("curl", "--fail", "--silent", "--max-time", "2", "--unix-socket", "/run/pcms-lite/web.sock", "http://localhost/login", capture=True)
                break
            except subprocess.CalledProcessError:
                time.sleep(1)
        else:
            raise RuntimeError("Веб-служба не прошла проверку готовности")
        run("systemctl", "is-active", *active)
        run("systemctl", "enable", "--now", "nginx")
        publish_nginx(nginx_conf(args.domain, https), args.domain, backup_dir=backup)
        published = True
        port = 443 if https else 80
        scheme = "https" if https else "http"
        page = run("curl", "--noproxy", "*", "--fail", "--silent", "--max-time", "15", "--resolve", f"{args.domain}:{port}:127.0.0.1", f"{scheme}://{args.domain}/login", capture=True)
        if "Система проверки решений" not in page:
            raise RuntimeError("nginx отвечает другим сайтом; конфигурация будет восстановлена")
    except BaseException:
        if published:
            if old_site is None:
                SITE.unlink(missing_ok=True)
            else:
                atomic_write(SITE, old_site)
            with contextlib.suppress(Exception):
                run("nginx", "-t")
                run("systemctl", "reload", "nginx")
        with contextlib.suppress(Exception):
            run("systemctl", "stop", *services)
        if previous:
            current.unlink(missing_ok=True)
            current.symlink_to(previous)
        else:
            current.unlink(missing_ok=True)
        for path, original in previous_units.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, original)
        run("systemctl", "daemon-reload")
        if previous:
            run("systemctl", "start", "pcms-web.service", *[f"pcms-worker@{i}.service" for i in range(old_workers)])
        raise
    for i in range(args.workers, old_workers):
        run("systemctl", "disable", "--now", f"pcms-worker@{i}.service")
    state.update(workers=args.workers, release=str(release), https=https)
    atomic_write(state_path, json.dumps(state), 0o600)
    if args.email and not https:
        cert_config = Path("/etc/letsencrypt/renewal/pcms-lite.conf")
        if cert_config.exists() and not state.get("certificate_requested"):
            raise RuntimeError("Сертификат pcms-lite уже существует и не принадлежит этой установке")
        state["certificate_requested"] = True
        atomic_write(state_path, json.dumps(state), 0o600)
        run("apt-get", "install", "-y", "--no-upgrade", "-o", "Dpkg::Options::=--force-confold", "certbot")
        acme = Path("/var/lib/pcms-acme")
        acme.mkdir(mode=0o755, exist_ok=True)
        # Certbot never edits nginx. Explicit --email authorizes ACME registration.
        run("certbot", "certonly", "--webroot", "-w", acme, "--cert-name", "pcms-lite", "-d", args.domain,
            "--email", args.email, "--agree-tos", "--non-interactive")
        hook = Path("/etc/letsencrypt/renewal-hooks/deploy/pcms-lite-reload")
        if hook.is_symlink() or (hook.exists() and "Managed by PCMS Lite" not in hook.read_text()):
            raise RuntimeError(f"Существующий hook {hook} не принадлежит установщику")
        atomic_write(hook, "#!/bin/sh\n" + MARKER + "\nset -eu\nnginx -t\nsystemctl reload nginx\n", 0o755)
        http_site = SITE.read_text()
        http_unit = (UNITDIR / "pcms-web.service").read_text()
        try:
            publish_nginx(nginx_conf(args.domain, True), args.domain, backup_dir=backup / "https")
            managed_write(UNITDIR / "pcms-web.service", unit_files(True, args.workers, socket_group)["pcms-web.service"])
            run("systemctl", "daemon-reload")
            run("systemctl", "restart", "pcms-web.service")
            for attempt in range(15):
                try:
                    run("curl", "--noproxy", "*", "--fail", "--silent", "--max-time", "3", "--resolve", f"{args.domain}:443:127.0.0.1", f"https://{args.domain}/login", capture=True)
                    break
                except subprocess.CalledProcessError:
                    if attempt == 14:
                        raise
                    time.sleep(1)
        except BaseException:
            atomic_write(SITE, http_site)
            atomic_write(UNITDIR / "pcms-web.service", http_unit)
            run("systemctl", "daemon-reload")
            run("systemctl", "restart", "pcms-web.service")
            run("nginx", "-t")
            run("systemctl", "reload", "nginx")
            raise
        state["https"] = True
        atomic_write(state_path, json.dumps(state), 0o600)
        run("systemctl", "enable", "--now", "certbot.timer")
        https = True
    print(f"\nГотово: {'https' if https else 'http'}://{args.domain}")
    print(f"Первоначальный логин и пароль: sudo cat {CONF}/admin-credentials")
    print(f"Данные: {DATA}; резервные копии своих конфигураций: {backup}")


def main():
    parser = argparse.ArgumentParser(description="Установка PCMS Lite на Ubuntu 24.04+ без перезаписи чужих nginx-сайтов")
    parser.add_argument("--domain", required=True, type=validate_domain, help="Отдельный домен или IPv4")
    parser.add_argument("--email", help="Включить HTTPS Let's Encrypt; требуется DNS и доступный порт 80; принимаются условия ACME")
    parser.add_argument("--workers", type=int, default=1, choices=range(1, 9))
    parser.add_argument("--sandbox-gib", type=int, default=4, choices=range(2, 129), metavar="2..128", help="Размер нового ext4-образа; существующий не меняется")
    args = parser.parse_args()
    os.environ["DEBIAN_FRONTEND"] = "noninteractive"
    os.environ["NEEDRESTART_MODE"] = "l"
    os.umask(0o022)
    try:
        if os.geteuid() != 0:
            raise RuntimeError("Запустите через sudo")
        with open("/run/lock/pcms-lite-install.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            install(args)
    except (Exception, KeyboardInterrupt) as exc:
        print(f"\nУстановка остановлена: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr, file=sys.stderr)
        print("Пакеты ОС, созданные данные и образ песочниц сохранены. Исправьте причину и повторите запуск. Журналы: journalctl -u pcms-web -u pcms-worker@0 -u pcms-isolate", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
