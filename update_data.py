"""Скачивает новые протоколы khl.ru, разбирает их и пишет site/data/*.json."""
import datetime as dt, json, logging, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
import protocols as P
import players as PL

_h = P.have_ids
P.have_ids = lambda: _h() | {int(k) for k, r in P.load_games().items() if not r.get("bad")}
P.PDF_DIR.mkdir(parents=True, exist_ok=True)
now = lambda: dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")

for x in filter(None, os.getenv("KHL_EXTRA_IDS", "").replace(" ", "").split(",")):   # протоколы, id которых вне диапазона
    gid, f = int(x), P.PDF_DIR / f"game-{x}-ru.pdf"
    if not f.exists() and x not in P.load_games():
        try:
            P._atomic_write(f, P.fetch_pdf(gid))
            print("доп. протокол скачан:", gid)
        except Exception as e:
            print("доп. протокол", gid, "не скачался:", repr(e))

blocked = False
try:
    print("скачано новых:", P.download_all())
except P.Blocked as e:
    blocked = True
    print("ВНИМАНИЕ:", e)
before = {k: dict(v) for k, v in P.load_games().items()}
refreshed = []
if not blocked:                         # перекачиваем игры, где мог остаться промежуточный счёт
    try:
        refreshed = P.refresh_stale(int(os.getenv("KHL_REFRESH_DAYS", "3")))
    except Exception as e:
        print("перепроверка протоколов не удалась:", repr(e))
games = P.parse_new()
ms = P.build_matches()
Path("site/data").mkdir(parents=True, exist_ok=True)
Path("site/data/matches.json").write_text(json.dumps({"updated": now(), "matches": [asdict(m) for m in ms]},
                                                    ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

try:                                                    # календарь (будущие матчи); сбой не должен ронять обновление
    import khl_schedule as SC
    n = SC.update(sorted({t for m in ms for t in (m.home, m.away)}), ms[-1].season if ms else None)
    print("календарь: игр в расписании:", n)
except Exception as e:
    print("календарь: не обновлён:", repr(e))

PJ = P.PDF_DIR / "players.json"
store = P._load_json(PJ, {})
for gid in refreshed:                   # счёт изменился -> составы этой игры тоже разбираем заново
    k = str(gid)
    if k not in games or games[k].get("score") != before.get(k, {}).get("score"):
        store.pop(k, None)
todo = [g for g, r in games.items() if not (r.get("bad") or r.get("warn")) and store.get(g, {}).get("v") != PL.VERSION]


DEADLINE = time.time() + 25 * 60      # на составы не больше 25 минут, остальное доберём в следующий запуск


def save_players():
    P._save_json(PJ, store)
    Path("site/data/players.json").write_text(json.dumps({"updated": now(), "games": sorted(store.values(), key=lambda x: (x["d"], x["id"]))},
                                                        ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def job(gid):
    if time.time() > DEADLINE:
        return gid, None
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


print("игроки: разбираю игр:", len(todo), flush=True)
with ThreadPoolExecutor(4) as ex:
    for n, (gid, rec) in enumerate(ex.map(job, todo), 1):
        if rec:
            store[gid] = rec
        if n % 20 == 0:
            save_players()
            print("игроки: готово", n, "из", len(todo), flush=True)
save_players()

bad = [f"{g}: {r.get('bad') or r.get('warn')}" for g, r in games.items() if r.get("bad") or r.get("warn")]
msg = (f"Матчей: {len(ms)}, последний: {ms[-1].date if ms else '–'}. Игр с составами: {len(store)}. "
       f"Не разобрано: {len(bad)} {bad[:10]}. Блокировка khl.ru: {'ДА' if blocked else 'нет'}")
print(msg)
if os.getenv("GITHUB_STEP_SUMMARY"):
    open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(msg + "\n")
sys.exit(0 if ms and not blocked else 1)
