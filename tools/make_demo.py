"""Generate a self-contained demo contest with the real bundled testlib header."""
import argparse
import io
import zipfile
from pathlib import Path

CHECKER = r'''#include "testlib.h"
int main(int argc, char** argv) {
    registerTestlibCmd(argc, argv);
    while (!ans.seekEof()) {
        std::string expected = ans.readToken();
        std::string actual = ouf.readToken();
        if (expected != actual) quitf(_wa, "Tokens differ");
    }
    if (!ouf.seekEof()) quitf(_wa, "Extra output");
    quitf(_ok, "All tokens match");
}
'''


def problem_zip(short="sum", title="Сумма двух чисел", cases=None):
    cases = cases or [("1 2\n", "3\n"), ("-7 10\n", "3\n"), ("1000000000 1000000000\n", "2000000000\n")]
    buffer = io.BytesIO()
    tests = "".join('<test method="manual"/>' for _ in cases)
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<problem short-name="{short}" url="https://polygon.codeforces.com/demo/{short}" revision="1">
  <names><name language="russian" value="{title}"/></names>
  <statements><statement language="russian" type="text/html" path="statements/russian/index.html"/></statements>
  <judging input-file="" output-file=""><testset name="tests">
    <time-limit>1000</time-limit><memory-limit>67108864</memory-limit>
    <test-count>{len(cases)}</test-count><input-path-pattern>tests/%02d</input-path-pattern>
    <answer-path-pattern>tests/%02d.a</answer-path-pattern><tests>{tests}</tests>
  </testset></judging>
  <assets><checker type="testlib"><source path="files/check.cpp" type="cpp.g++17"/></checker></assets>
</problem>'''
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("problem.xml", xml)
        z.writestr("files/check.cpp", CHECKER)
        z.writestr("files/testlib.h", (Path(__file__).resolve().parents[1] / "tests/fixtures/testlib.h").read_bytes())
        operation = "сумму" if short == "sum" else "произведение"
        z.writestr("statements/russian/index.html", f'''<!doctype html><html lang="ru"><meta charset="utf-8"><title>{title}</title>
<style>body{{font:16px/1.7 Arial;padding:20px;max-width:800px;margin:auto;color:#202a35}}pre{{background:#f1f3f6;padding:12px}}</style>
<h1>{title}</h1><p>Даны два целых числа a и b. Выведите их {operation}.</p>
<h2>Входные данные</h2><p>Два числа в одной строке. |a|, |b| ≤ 10⁹.</p>
<h2>Выходные данные</h2><p>Одно целое число — ответ.</p><h2>Пример</h2><pre>{cases[0][0]}</pre><pre>{cases[0][1]}</pre></html>''')
        for n, (input_text, output_text) in enumerate(cases, 1):
            z.writestr(f"tests/{n:02d}", input_text)
            z.writestr(f"tests/{n:02d}.a", output_text)
    return buffer.getvalue()


def contest_zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("contest.xml", '''<contest><problems>
<problem index="A" url="https://polygon.codeforces.com/demo/sum"/>
<problem index="B" url="https://polygon.codeforces.com/demo/product"/>
</problems></contest>''')
        z.writestr("sum.zip", problem_zip())
        z.writestr("product.zip", problem_zip("product", "Произведение двух чисел", [("2 3\n", "6\n"), ("-5 7\n", "-35\n"), ("1000000000 1000000000\n", "1000000000000000000\n")]))
    return buffer.getvalue()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", nargs="?", default="demo-contest.zip")
    args = parser.parse_args()
    Path(args.output).write_bytes(contest_zip())
    print(f"Создан {args.output}")
