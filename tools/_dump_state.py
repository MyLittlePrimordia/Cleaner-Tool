import io, sys
out = io.StringIO()
lines = open(r"C:\Users\User\Desktop\Cleaner Tool\app\gameping.py",
             encoding="utf-8", errors="replace").read().splitlines()

def dump(lines_, a, b, title):
    out.write(f"===== {title} [{a}-{b}] =====\n")
    for i in range(a - 1, min(b, len(lines_))):
        out.write(f"{i+1}: {lines_[i]}\n")

import app.gameping as _gp
out.write(f"GAME_TABS_FINAL len={len(_gp.GAME_TABS)}\n")
_games = [t for t, k in _gp.GAME_TABS if not k.startswith("company_") and k not in ("companies", "custom")]
_comps = [t for t, k in _gp.GAME_TABS if k.startswith("company_")]
out.write(f"GAMES={len(_games)} COMPANIES={len(_comps)}\n")
out.write(f"SMOKE_COMPANIMENT: custom_in_games={[t for t,k in _gp.GAME_TABS if k=='custom']}\n")
open(r"C:\Users\User\AppData\Local\Temp\opencode\tablayout_final.txt", "w",
     encoding="utf-8").write("\n".join(out.getvalue(),))
