"""Creator Studio launcher (no console window).

Starts the local server in the background if it isn't running yet, then opens
the app in its own window (Edge/Chrome app mode, no tabs or address bar).
"""
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 7860
URL = f"http://127.0.0.1:{PORT}"
NO_WINDOW = 0x08000000      # CREATE_NO_WINDOW
DETACHED = 0x00000008       # DETACHED_PROCESS


def server_up():
    try:
        urllib.request.urlopen(URL + "/api/library", timeout=1)
        return True
    except urllib.error.HTTPError:   # answered (e.g. 401 "please sign in"), so it's running
        return True
    except Exception:
        return False


def start_server():
    exe = sys.executable  # pythonw.exe when launched from the shortcut
    log = open(os.path.join(HERE, "studio.log"), "a", encoding="utf-8")
    env = dict(os.environ, NO_BROWSER="1", PYTHONIOENCODING="utf-8")
    subprocess.Popen([exe, os.path.join(HERE, "server.py")], cwd=HERE, env=env,
                     stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                     creationflags=NO_WINDOW | DETACHED, close_fds=True)


def open_window():
    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for b in candidates:
        if os.path.exists(b):
            subprocess.Popen([b, f"--app={URL}", "--window-size=1320,900"], creationflags=DETACHED)
            return
    os.startfile(URL)


def stop_server():
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                         creationflags=NO_WINDOW).stdout
    pids = {line.split()[-1] for line in out.splitlines()
            if f"127.0.0.1:{PORT}" in line and "LISTENING" in line}
    for pid in pids:
        subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True,
                       creationflags=NO_WINDOW)
    for _ in range(20):
        if not server_up(): break
        time.sleep(0.25)


def auto_update():
    """Grab the latest version from GitHub; never let a failed update stop the app opening."""
    try:
        sys.path.insert(0, HERE)
        import updater
        return updater.update()
    except Exception:
        return False


def main():
    if auto_update() and server_up():
        stop_server()                 # running the old code, restart it on the new code
    if not server_up():
        start_server()
        for _ in range(120):          # first start loads ML libraries, give it up to 60s
            if server_up(): break
            time.sleep(0.5)
    open_window()


if __name__ == "__main__":
    main()
