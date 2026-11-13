"""Stops the background Creator Studio server (frees port 7860)."""
import subprocess

out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True).stdout
pids = {line.split()[-1] for line in out.splitlines()
        if "127.0.0.1:7860" in line and "LISTENING" in line}
for pid in pids:
    subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True)
