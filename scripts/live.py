"""Живая статистика матчей КХЛ.

Пока идут игры, раз в ~30 секунд качает незавершённые протоколы khl.ru, разбирает их и публикует
live.json в ветку `live` репозитория (сайт читает его с raw.githubusercontent.com).
Запускается из .github/workflows/live.yml; вне игровых окон завершается сразу."""
import datetime as dt, json, logging, os, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
import protocols as P

log = logging.getLogger("live")
ROOT = Path(__file__).resolve().parent.parent
MSK = dt.timezone(dt.timedelta(hours=3))
INTERVAL = int(os.getenv("LIVE_INTERVAL", "30"))        # период обновления, сек
RUN_MIN = int(os.getenv("LIVE_RUN_MIN", "330"))         # максимум работы одного запуска, мин
RESCAN = int(os.getenv("LIVE_RESCAN", "240"))           # как часто искать новые начавшиеся игры, сек
SPAN = int(os.getenv("LIVE_SPAN", "45"))                # сколько id вокруг последнего сыгранного проверяем


def schedule_busy() -> bool:
    """Есть ли по расписанию игра, которая скоро начнётся или идёт (старт −25 мин … +4 ч)."""
    try:
        games = json.loads((ROOT / "site/data/schedule.json").read_text(encoding="utf-8"))["games"]
    except Exception:
        return True                                     # расписания нет — на всякий случай работаем
    now = dt.datetime.now(MSK)
    for g in games:
        try:
            st = dt.datetime.fromisoformat(f"{g['d']}T{g['t']}").replace(tzinfo=MSK)
        except Exception:
            continue
        if st - dt.timedelta(minutes=25) <= now <= st + dt.timedelta(hours=4):
            return True
    return False


def probe(gid: int):
    try:
        data = P.fetch_pdf(gid)
    except P.NotFound:
        return gid, None
    except Exception as e:
        log.warning("id %s: %r", gid, e)
        return gid, None
    rec, why = P.parse_text(P.pdf_to_text(data), partial=True)
    if not rec:
        return gid, None
    today = dt.datetime.now(MSK).date()
    try:
        if abs((dt.date.fromisoformat(rec["date"]) - today).days) > 1:
            return gid, None
    except Exception:
        return gid, None
    rec["id"] = gid
    return gid, rec


def probe_many(ids, workers=6):
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for gid, rec in ex.map(probe, ids):
            if rec:
                out[gid] = rec
    return out


def publish(games: list):
    payload = {"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "games": games}
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    tok, repo = os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPOSITORY")
    if not (tok and repo):                              # локальный запуск: просто файл рядом с сайтом
        (ROOT / "site/data/live.json").write_text(text, encoding="utf-8")
        return
    d = Path(tempfile.mkdtemp())
    (d / "live.json").write_text(text, encoding="utf-8")

    def git(*a):
        subprocess.run(["git", *a], cwd=d, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.name", "github-actions")
    git("config", "user.email", "actions@github.com")
    git("checkout", "-q", "-b", "live")
    git("add", "live.json")
    git("commit", "-q", "-m", "live")
    git("push", "-q", "-f", f"https://x-access-token:{tok}@github.com/{repo}.git", "live:live")


def main():
    if not schedule_busy():
        log.info("по расписанию сейчас игр нет — выходим")
        return
    finished = {int(k) for k, r in P.load_games().items() if not r.get("bad")}
    last = max(finished, default=P.FIRST_ID)
    cand = [g for g in range(last - SPAN, last + SPAN) if g not in finished]
    deadline = time.time() + RUN_MIN * 60
    live: dict[int, dict] = {}
    next_scan, idle, last_sig, seen = 0.0, 0, None, {}
    while time.time() < deadline:
        t0 = time.time()
        try:
            if t0 >= next_scan:                         # поиск новых игр среди «ещё не сыгранных» id
                live = probe_many(cand)
                next_scan = t0 + RESCAN
            else:
                live.update(probe_many(list(live)))
        except Exception as e:
            log.warning("цикл не удался: %r", e)
        for gid, r in list(live.items()):               # законченные матчи показываем ещё 45 минут
            if r.get("live"):
                seen.pop(gid, None)
            else:
                seen.setdefault(gid, t0)
                if t0 - seen[gid] > 2700:
                    live.pop(gid)
        games = sorted(live.values(), key=lambda r: (r.get("time") or "", r["id"]))
        busy = any(g.get("live") for g in games)
        sig = json.dumps(games, sort_keys=True, ensure_ascii=False)
        if busy or sig != last_sig:
            try:
                publish(games)
            except Exception as e:
                log.warning("публикация не удалась: %r", e)
            last_sig = sig
        log.info("идёт игр: %d, всего в live.json: %d", sum(1 for g in games if g.get("live")), len(games))
        idle = 0 if (busy or schedule_busy()) else idle + 1
        if idle >= 3:                                   # игр нет и не ожидается — завершаем
            break
        time.sleep(max(1, INTERVAL - (time.time() - t0)))
    publish([])


if __name__ == "__main__":
    main()
