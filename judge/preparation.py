"""Build reference programs and answers only through the isolated executor.

Profiles belong to the server administrator; archives cannot provide commands.
"""
import json
import os
import platform
import re
from pathlib import Path

from .sandbox import SandboxError, compile_source

JAVA_FLAGS = ['-Xms16m', '-Xmx512m', '-Xss64m', '-DONLINE_JUDGE=true', '-Djava.io.tmpdir=/box', '-XX:+UseSerialGC', '-XX:ActiveProcessorCount=1', '-XX:-UsePerfData']
JDK = '/usr/lib/jvm/java-21-openjdk-' + ('arm64' if platform.machine() == 'aarch64' else 'amd64') + '/bin'

# Runtime paths are explicit, avoiding alternatives symlinks into hidden /etc.
# Additional Polygon compiler IDs can be mapped in PCMS_REFERENCE_PROFILES.
PROFILES = {
    'python.3': {'source': 'main.py', 'run': ['/usr/bin/python3', '-I', 'main.py']},
    'python.2': {'source': 'main.py', 'run': ['/usr/bin/python2', '-E', '-s', 'main.py']},
    'python.pypy3': {'source': 'main.py', 'run': ['/usr/bin/pypy3', '-I', 'main.py']},
    'python.pypy': {'source': 'main.py', 'run': ['/usr/bin/pypy', '-E', '-s', 'main.py']},
    'ruby': {'source': 'main.rb', 'run': ['/usr/bin/ruby', 'main.rb']},
    'perl': {'source': 'main.pl', 'run': ['/usr/bin/perl', 'main.pl']},
    'php': {'source': 'main.php', 'run': ['/usr/bin/php8.3', '-n', 'main.php']},
    'javascript': {'source': 'main.js', 'run': ['/usr/bin/node', 'main.js']},
    'go': {'source': 'main.go', 'compile': ['/usr/bin/go', 'build', '-o', 'main', 'main.go'], 'artifact': 'main'},
    'rust': {'source': 'main.rs', 'compile': ['/usr/bin/rustc', '-O', 'main.rs', '-o', 'main'], 'artifact': 'main'},
    'pascal': {'source': 'main.pas', 'compile': ['/usr/bin/fpc', '-O2', '-omain', 'main.pas'], 'artifact': 'main'},
    'haskell': {'source': 'main.hs', 'compile': ['/usr/bin/ghc', '-O2', '-o', 'main', 'main.hs'], 'artifact': 'main'},
    'ocaml': {'source': 'main.ml', 'compile': ['/usr/bin/ocamlopt', '-o', 'main', 'main.ml'], 'artifact': 'main'},
    'd': {'source': 'main.d', 'compile': ['/usr/bin/gdc', '-O2', '-o', 'main', 'main.d'], 'artifact': 'main'},
    'csharp.mono': {'source': 'main.cs', 'compile': ['/usr/bin/mcs', '-out:program.exe', 'main.cs'],
                    'artifact': 'program.exe', 'run': ['/usr/bin/mono', 'program.exe']},
}


def profiles():
    result = dict(PROFILES)
    config = os.environ.get('PCMS_REFERENCE_PROFILES', '/etc/pcms-lite/reference-languages.json')
    try:
        overrides = json.loads(Path(config).read_text())
    except FileNotFoundError:
        overrides = {}
    except OSError as exc:
        raise ValueError(f"Нет доступа к профилям {config}: проверьте права чтения файла и прохода к каталогу для пользователя pcms") from exc
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"Некорректный JSON профилей: {config}") from exc
    if not isinstance(overrides, dict) or any(not isinstance(value, dict) for value in overrides.values()):
        raise ValueError(f"Профили {config} должны быть JSON-объектом с объектами настроек языков")
    result.update(overrides)
    return result


def reference_language(compiler):
    if compiler in profiles():
        return compiler
    if compiler.startswith('cpp.') or compiler == 'cpp':
        return 'cpp'
    if compiler in {'c', 'c.gcc', 'c.gcc11', 'c.gcc17'}:
        return 'c'
    if re.fullmatch(r'java(?:\.jdk)?(?:8|11|17|21)', compiler):
        return 'java'
    raise ValueError(f'Для языка эталона {compiler!r} нет профиля. Администратору нужно установить runtime и добавить профиль в /etc/pcms-lite/reference-languages.json')


def check_result(result, step):
    if result.verdict != 'OK':
        detail = (result.stderr or result.stdout).decode(errors='replace')[:2000]
        raise SandboxError(f'{step}: {result.verdict}. {detail}')


def build_reference(sandbox, root, reference, headers):
    compiler = reference['type']
    language = reference_language(compiler)
    source = (root / reference['path']).read_bytes()
    if language in {'c', 'cpp'}:
        version = re.search(r'(98|03|11|14|17|20|23)$', compiler) if language == 'cpp' else None
        standard = 'gnu++' + version.group(1) if version else None
        result = compile_source(sandbox, language, source, {h: (root / h).read_bytes() for h in headers}, source_name=reference['path'], standard=standard)
        check_result(result, 'Компиляция эталона')
        return ['/box/main'], {'main': result.artifact}
    if language == 'java':
        # Rename Polygon's numbered source to its public class name.
        text = re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"', '', source.decode('utf-8-sig'), flags=re.S)
        match = re.search(r'\bpublic\s+(?:(?:final|abstract)\s+)*class\s+([A-Za-z_$][\w$]*)', text)
        if not match:
            match = re.search(r'\bclass\s+([A-Za-z_$][\w$]*)', text)
        if not match:
            raise ValueError('Не найден Java-класс эталонного решения')
        name = match.group(1)
        package = re.search(r'\bpackage\s+([\w.]+)\s*;', text)
        main_class = (package.group(1) + '.' if package else '') + name
        jdk = os.environ.get('PCMS_JAVA_BIN', JDK)
        libraries = {f'library{i}.jar': (root / path).read_bytes() for i, path in enumerate(reference.get('libraries', []))}
        classpath = ':'.join(libraries) or '.'
        # A fixed, trusted wrapper packages javac output in the same sandbox.
        wrapper = 'import subprocess\nsubprocess.run(' + repr([jdk+'/javac', *['-J'+x for x in JAVA_FLAGS], '-encoding', 'UTF-8', '-cp', classpath, '-d', 'classes', name+'.java']) + ',check=True)\nsubprocess.run(' + repr([jdk+'/jar', *['-J'+x for x in JAVA_FLAGS], '--create', '--file', 'program.jar', '-C', 'classes', '.']) + ',check=True)\n'
        result = sandbox.run(['/usr/bin/python3', '-I', 'build.py'], {name+'.java': source, 'build.py': wrapper.encode(), **libraries},
                             time_ms=60000, memory_kb=1048576, processes=64, artifact='program.jar')
        check_result(result, 'Компиляция Java-эталона')
        command = [jdk+'/java', *JAVA_FLAGS, '-cp', 'program.jar:' + classpath]
        if reference.get('checker'):
            command += ['ru.ifmo.testlib.CheckerFramework', main_class]
        else:
            command += [main_class]
        return command, {'program.jar': result.artifact, **libraries}
    profile = profiles()[language]
    name = profile['source']
    files = {name: source}
    executable = profile.get('compile', profile.get('run', ['']))[0]
    if not executable or not Path(executable).is_file():
        raise ValueError(f'Для {compiler} не установлен runtime/компилятор: {executable}')
    if profile.get('compile'):
        result = sandbox.run(profile['compile'], files, time_ms=120000, memory_kb=1048576, processes=64,
                             artifact=profile['artifact'])
        check_result(result, f'Компиляция эталона ({compiler})')
        files = {profile['artifact']: result.artifact}
    return profile.get('run', ['/box/main']), files


def prepare_answers(root, manifest, sandbox, progress=None, byte_budget=2*1024**3):
    missing = [t for t in manifest['tests'] if not (root / t['answer']).is_file()]
    if not missing:
        return 0
    if sandbox is None:
        raise ValueError('Нужна подготовка ответов в isolate: загрузите архив через админку или команду pcms import')
    command, files = build_reference(sandbox, root, manifest['reference'], manifest['headers'])
    checker = build_checker(sandbox, root, manifest)
    if manifest['input-file'] in files or manifest['output-file'] in files:
        raise ValueError('Имя файла ввода/вывода совпадает с программой эталона')
    total = 0
    for i, test in enumerate(manifest['tests'], 1):
        if (root / test['answer']).is_file():
            continue
        if progress:
            progress(f"{manifest['short_name']}: ответ {i}/{len(manifest['tests'])}")
        data = (root / test['input']).read_bytes()
        result = sandbox.run(command, files, data, time_ms=max(10000, manifest['time_ms'] * 5),
                             memory_kb=max(1048576, manifest['memory_kb']), processes=64,
                             input_name=manifest['input-file'], output_name=manifest['output-file'])
        check_result(result, f'Эталон, тест {i}')
        checked = run_checker(sandbox, checker, data, result.stdout, result.stdout)
        check_result(checked, f'Проверка сгенерированного ответа, тест {i}')
        total += len(result.stdout)
        if total > byte_budget:
            raise ValueError('Суммарный размер пакета и ответов превышает 2 ГиБ')
        dest = root / test['answer']
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(result.stdout)
    return total


def build_checker(sandbox, root, manifest):
    if manifest.get('checker_type', 'cpp').startswith('java'):
        return build_reference(sandbox, root, {'path': manifest['checker'], 'type': manifest['checker_type'],
                               'libraries': manifest['checker_libraries'], 'checker': True}, manifest['headers'])
    result = compile_source(sandbox, 'cpp', (root / manifest['checker']).read_bytes(),
                            {h: (root / h).read_bytes() for h in manifest['headers']}, manifest['checker'], testlib_compat=True)
    check_result(result, 'Компиляция чекера')
    return ['/box/main'], {'main': result.artifact}


def run_checker(sandbox, checker, data, output, answer):
    command, files = checker
    return sandbox.run([*command, 'input', 'output', 'answer'],
                       {**files, 'input': data, 'output': output, 'answer': answer},
                       time_ms=10000, memory_kb=1048576, processes=64)
