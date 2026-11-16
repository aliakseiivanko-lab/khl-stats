"""Скачивает новые протоколы khl.ru, разбирает их и пишет site/data/matches.json."""
import datetime as dt, json, sys
from dataclasses import asdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import protocols as P

_h = P.have_ids
P.have_ids = lambda: _h() | {int(k) for k in P.load_games()}   # уже разобранные заново не качаем
try:
    print("скачано новых:", P.download_all())
except P.Blocked as e:
    print("ВНИМАНИЕ:", e)
P.parse_new()
ms = P.build_matches()
out = Path("site/data/matches.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
                           "matches": [asdict(m) for m in ms]}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print("матчей:", len(ms))

# ── игроки: для каждой разобранной игры один раз читаем PDF (с диска или заново с khl.ru) ──
import time
from concurrent.futures import ThreadPoolExecutor
import players as PL
PJ = P.PDF_DIR / "players.json"
store = P._load_json(PJ, {})
games = P.load_games()
todo = [g for g, r in games.items() if not (r.get("bad") or r.get("warn")) and store.get(g, {}).get("v") != PL.VERSION]


def job(gid):
    r = games[gid]
    try:
        f = P.PDF_DIR / f"game-{gid}-ru.pdf"
        data = f.read_bytes() if f.exists() else P.fetch_pdf(int(gid))
        time.sleep(0.15)
        return gid, {"v": PL.VERSION, "id": int(gid), "d": r["date"], "h": r["home"], "a": r["away"],
                     "p": PL.parse_players(data, r["home"], r["away"])}
    except Exception as e:
        print("игроки, игра", gid, "->", repr(e))
        return gid, None


print("игроки: разбираю игр:", len(todo))
with ThreadPoolExecutor(4) as ex:
    for gid, rec in ex.map(job, todo):
        if rec:
            store[gid] = rec
P._save_json(PJ, store)
Path("site/data/players.json").write_text(json.dumps(
    {"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
     "games": sorted(store.values(), key=lambda x: (x["d"], x["id"]))}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print("игры с составами:", len(store))
sys.exit(0 if ms else 1)
