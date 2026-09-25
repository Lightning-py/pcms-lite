"""Polygon test/group points; dependency success is independent of points earned."""
import math


def points(value):
    try:
        n = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("Некорректные баллы") from exc
    if not math.isfinite(n) or not 0 <= n <= 1000000:
        raise ValueError("Баллы должны быть конечным числом от 0 до 1000000")
    return n


def parse_scoring(ts, tests):
    groups = {}
    for node in ts.findall('groups/group'):
        name = node.get('name', '')
        policy = node.get('points-policy', 'each-test')
        if not name or name in groups or policy not in {'each-test', 'complete-group'}:
            raise ValueError('Некорректное имя или политика группы')
        groups[name] = {'policy': policy, 'points': points(node.get('points', '0')),
                        'dependencies': [d.get('group', '') for d in node.findall('dependencies/dependency')]}
    for test in tests:
        test['points'] = points(test['points'])
        name = test['group']
        if name not in groups:
            # Polygon also exports group labels with no scoring declaration.
            if ts.find('groups') is not None and name:
                raise ValueError(f'Неизвестная группа {name}')
            groups[name] = {'policy': 'each-test', 'points': 0, 'dependencies': []}
    visiting, done = set(), set()
    def visit(name):
        if name in visiting or name not in groups:
            raise ValueError('Цикл или неизвестная зависимость групп')
        if name in done:
            return
        visiting.add(name)
        for dep in groups[name]['dependencies']:
            visit(dep)
        visiting.remove(name)
        done.add(name)
    for name in groups:
        visit(name)
    maximum = sum(g['points'] if g['policy'] == 'complete-group' else
                  sum(t['points'] for t in tests if t['group'] == name) for name, g in groups.items())
    return {'groups': groups, 'max_score': maximum, 'scoring': 'points' if maximum > 0 else 'icpc'}


def calculate_score(manifest, verdicts):
    tests, groups = manifest['tests'], manifest['groups']
    passed = {}
    def complete(name):
        if name not in passed:
            own = [i for i, t in enumerate(tests) if t['group'] == name]
            passed[name] = all(verdicts[i] == 'AC' for i in own) and all(complete(d) for d in groups[name]['dependencies'])
        return passed[name]
    total = 0
    for name, g in groups.items():
        if not all(complete(d) for d in g['dependencies']):
            continue
        if g['policy'] == 'complete-group':
            total += g['points'] if complete(name) else 0
        else:
            total += sum(t['points'] for i, t in enumerate(tests) if t['group'] == name and verdicts[i] == 'AC')
    return total
