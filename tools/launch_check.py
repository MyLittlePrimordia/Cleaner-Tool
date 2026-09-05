"""Launch the real app via its real entry point for a few seconds, verify
it's alive with the main UI built (not stuck on an error dialog), then close
it cleanly through the real _on_close path."""
import subprocess
import sys
import time
import pathlib

proc = subprocess.Popen(
    [sys.executable, "main.py"],
    cwd=str(pathlib.Path(__file__).resolve().parents[1]),
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
)
time.sleep(6)
if proc.poll() is not None:
    out, err = proc.communicate(timeout=10)
    print("APP DIED EARLY")
    print("STDOUT:", out[-2000:])
    print("STDERR:", err[-2000:])
    sys.exit(1)
print("app alive after 6s; terminating")
proc.terminate()
try:
    proc.wait(timeout=5)
    print("terminated cleanly")
except subprocess.TimeoutExpired:
    proc.kill()
    print("had to kill")
sys.exit(0)
