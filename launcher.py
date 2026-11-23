from http.client import HTTPConnection, HTTPException
import json
from pathlib import Path
import sys
import threading
import time
import webbrowser

import uvicorn

from settings import APP_NAME, ROOT, WEB_HOST, WEB_PORT


URL = f"http://{WEB_HOST}:{WEB_PORT}/"
LOG_PATH = ROOT / ".telegram-data" / "launcher.log"


def attach_streams() -> None:
    """pythonw.exe has no console, and uvicorn's logging cannot write to a missing stream.

    Give both streams a file so the server starts silently and failures stay diagnosable.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle = LOG_PATH.open("a", encoding="utf-8", buffering=1)
    except OSError:
        handle = open(Path(__file__).with_name("launcher.log"), "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = handle
    if sys.stderr is None:
        sys.stderr = handle


def is_running() -> bool:
    connection = HTTPConnection(WEB_HOST, WEB_PORT, timeout=0.5)
    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        return response.status == 200 and json.loads(response.read(512)).get("app") == APP_NAME
    except (AttributeError, HTTPException, OSError, ValueError):
        return False
    finally:
        connection.close()


def open_when_ready() -> None:
    for _ in range(30):
        if is_running():
            webbrowser.open(URL)
            return
        time.sleep(0.25)


if __name__ == "__main__":
    attach_streams()
    print(f"极目 {URL}", flush=True)
    if is_running():
        webbrowser.open(URL)
    else:
        threading.Thread(target=open_when_ready, daemon=True).start()
        uvicorn.run("server:app", host=WEB_HOST, port=WEB_PORT, access_log=False)
