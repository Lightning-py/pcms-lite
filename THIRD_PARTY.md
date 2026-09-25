# Third-party materials

`tests/fixtures/testlib.h` is an unmodified copy of testlib 0.9.45 by
Mike Mirzayanov and contributors, retrieved on 2026-09-25 from:

https://raw.githubusercontent.com/MikeMirzayanov/testlib/master/testlib.h

SHA-256: `bb323e3c89285214966076e0d23d5a295c5f6126da7ff198c1276ddb95ecb1a0`

The file retains its original copyright and permission notices. It is included
for demo package generation and integration tests. Production problem packages
provide their own compatible copy of testlib.h.

Python dependencies are declared in pyproject.toml and keep their respective
licenses. Isolate is an external system dependency, not bundled into this project.
