import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import server  # noqa: E402

RUNNER = "import sys; sys.path.insert(0, sys.argv.pop(1)); import cb; sys.exit(cb.main())"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def site():
    srv = server.serve(0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="session")
def cb_env():
    state = tempfile.mkdtemp(prefix="cb-test-")
    env = {k: v for k, v in os.environ.items() if not k.startswith("CB_")}
    env.update(CB_STATE_DIR=state, CB_CDP_PORT=str(_free_port()), CB_HEADLESS="1", NO_PROXY="*", no_proxy="*")
    yield env
    subprocess.run([sys.executable, "-c", RUNNER, str(ROOT), "close"], env=env, capture_output=True, timeout=60)
    shutil.rmtree(state, ignore_errors=True)


class CB:
    def __init__(self, env, site):
        self.env = env
        self.site = site

    def __call__(self, *args, input=None, ok=True, env=None, timeout=90):
        e = dict(self.env, **(env or {}))
        r = subprocess.run([sys.executable, "-c", RUNNER, str(ROOT), *args], input=input, env=e,
                           capture_output=True, text=True, timeout=timeout, cwd=str(ROOT))
        if ok is True and r.returncode != 0:
            raise AssertionError(f"cb {' '.join(args)} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
        if ok is False and r.returncode == 0:
            raise AssertionError(f"cb {' '.join(args)} unexpectedly succeeded:\n{r.stdout}")
        r.out = r.stdout + r.stderr
        return r

    def goto(self, page):
        return self("goto", f"{self.site}/{page}")


@pytest.fixture(scope="session")
def cb(cb_env, site):
    c = CB(cb_env, site)
    c("launch")
    return c
