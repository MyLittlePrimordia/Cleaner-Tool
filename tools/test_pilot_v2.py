"""Phase-5 headless verification of the new SessionPilot contract."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       if False else r"C:\Users\User\Desktop\Cleaner Tool"))
from app.session_pilot import SessionPilot, normalize_path, normalize_exe

fails = []
def check(name, fn):
    try:
        fn()
        print("PASS", name)
    except Exception as e:
        fails.append((name, e))
        print("FAIL", name, "->", e)

# --- normalize helpers ---------------------------------------------------
def t_norm():
    assert normalize_path("C:/Games/X/game.EXE") == "c:\\games\\x\\game.exe", \
        normalize_path("C:/Games/X/game.EXE")
    assert normalize_path("") == ""
    assert normalize_exe("C:/a/b/Thing.exe") == "thing.exe"
    assert normalize_exe("thing") == "thing.exe"
check("normalize helpers", t_norm)

# --- state machine with (name, path) source ------------------------------
src = [[]]
applied, reverted = [], []
p = SessionPilot(get_processes=lambda: src[0],
                 watched=["C:/Games/Elden Ring/eldenring.exe", "cs2.exe"],
                 on_apply=lambda h: applied.append(h),
                 on_revert=lambda: reverted.append(1))

def t_detect():
    src[0] = [("eldenring.exe", "C:\\Games\\Elden Ring\\eldenring.exe")]
    p.tick()
    assert p.state == "active", p.state
    assert applied and applied[0][0] == "eldenring.exe", applied
check("path-verified detect", t_detect)

def t_wrong_dir_ignored():
    # same basename, different folder: not OUR game — must not keep the
    # session alive; two clean polls must revert
    src[0] = [("eldenring.exe", "D:\\Other\\eldenring.exe")]
    p.tick()
    s1 = p.state
    p.tick()
    assert p.state == "idle" and reverted, (s1, p.state, reverted)
check("wrong-dir hit ignored -> clean exit", t_wrong_dir_ignored)

def t_wrong_dir_no_session():
    # wrong-dir hit while idle must NOT start a session
    src[0] = [("eldenring.exe", "D:\\Other\\eldenring.exe")]
    n_before = len(applied)
    p.tick()
    assert p.state == "idle" and len(applied) == n_before, (p.state, applied)
check("wrong-dir hit starts no session", t_wrong_dir_no_session)

def t_legacy_basename():
    # cs2.exe was registered bare: matches anywhere (legacy contract)
    src[0] = [("cs2.exe", "D:\\anywhere\\cs2.exe")]
    p.tick()
    assert p.state == "active", p.state
check("legacy basename match", t_legacy_basename)

def t_javaw_rule():
    # javaw outside Minecraft paths must never fire; inside must
    q = SessionPilot(get_processes=lambda: src[0], watched=["javaw.exe"],
                     on_apply=lambda h: applied.append(h))
    src[0] = [("javaw.exe", "C:\\Program Files\\JetBrains\\ide\\javaw.exe")]
    q.tick()
    assert q.state == "idle", "IDE javaw fired a session!"
    src[0] = [("javaw.exe", "C:\\Users\\me\\AppData\\Roaming\\.minecraft\\javaw.exe")]
    q.tick()
    assert q.state == "active", "Minecraft javaw did not fire"
check("javaw path rule", t_javaw_rule)

def t_pause_and_resume():
    # busy/absence edge: absent once = not yet exited; twice = exit
    r = SessionPilot(get_processes=lambda: src[0], watched=["cs2.exe"],
                     stable_polls=2)
    src[0] = [("cs2.exe", "")]
    r.tick()
    assert r.state == "active"
    src[0] = []
    r.tick()
    assert r.state == "active", "single miss must not exit"
    r.tick()
    assert r.state == "idle", "two misses must exit"
check("grace period respected", t_pause_and_resume)

print()
print("ALL PILOT CONTRACT TESTS PASS" if not fails else f"{len(fails)} FAILURES")
sys.exit(1 if fails else 0)
