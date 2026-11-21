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
import live_sm as SM

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
        if os.getenv("GITHUB_ACTIONS"):
            log.error("НЕТ GITHUB_TOKEN: live.json не публикуется. Добавь в live.yml env: GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}")
        (ROOT / "site/data/live.json").write_text(text, encoding="utf-8")
        return
    d = Path(tempfile.mkdtemp())
    (d / "live.json").write_text(text, encoding="utf-8")

    def git(*a):
        r = subprocess.run(["git", *a], cwd=d, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"git {a[0]}: {(r.stderr or r.stdout).replace(tok, '***')[:300]}")
    git("init", "-q")
    git("config", "user.name", "github-actions")
    git("config", "user.email", "actions@github.com")
    git("checkout", "-q", "-b", "live")
    git("add", "live.json")
    git("commit", "-q", "-m", "live")
    git("push", "-q", "-f", f"https://x-access-token:{tok}@github.com/{repo}.git", "live:live")
    log.info("live.json опубликован в ветку live (%d игр)", len(games))


STAT_KEYS = ("sog", "fo", "fo_all", "pen", "pim", "pp_goals", "pp_opps", "shg", "blk", "hit", "extra")


def sm_scan(known, today):
    """Ищет на sportmail.ru матчи КХЛ за сегодня: список ссылок -> страницы матчей."""
    links = []
    for page in ("/hockey-khl/", "/hockey/", "/"):
        try:
            links += [l for l in SM.match_links(SM.get(page)) if l not in links]
        except Exception as e:
            log.warning("sportmail %s: %r", page, e)
    log.info("sportmail: найдено ссылок на матчи КХЛ: %d", len(links))
    out = {}
    for l in links[:60]:
        mid = l.rstrip("/").rsplit("/", 1)[1]
        try:
            rec = SM.parse_match(SM.get(l), mid, known, today)
        except Exception as e:
            log.warning("sportmail %s: %r", l, e)
            continue
        if rec:
            rec["_url"] = l
            out[mid] = rec
    return out


def main():
    if not schedule_busy():
        log.info("по расписанию сейчас игр нет — выходим")
        return
    try:
        known = sorted({t for m in json.loads((ROOT / "site/data/matches.json").read_text(encoding="utf-8"))["matches"]
                        for t in (m["home"], m["away"])})
    except Exception:
        known = []
    use_khl = os.getenv("LIVE_KHL", "1") != "0"
    finished_ids = {int(k) for k, r in P.load_games().items() if not r.get("bad")}
    last = max(finished_ids, default=P.FIRST_ID)
    cand = [g for g in range(last - SPAN, last + SPAN) if g not in finished_ids]
    deadline = time.time() + RUN_MIN * 60
    games: dict[str, dict] = {}
    khl: dict[int, dict] = {}
    was_live, seen = set(), {}
    next_scan, idle, last_sig = 0.0, 0, None
    while time.time() < deadline:
        t0 = time.time()
        today = dt.datetime.now(MSK).date().isoformat()
        try:
            if t0 >= next_scan:                         # раз в несколько минут ищем новые матчи
                found = sm_scan(known, today)
                for mid, rec in found.items():
                    if rec["live"]:
                        was_live.add(mid)
                    if rec["live"] or (rec["finished"] and mid in was_live):
                        games[mid] = rec
                if use_khl:
                    khl = probe_many(cand)
                next_scan = t0 + RESCAN
            else:
                for mid, rec in list(games.items()):
                    if not rec["live"]:
                        continue
                    new = SM.parse_match(SM.get(rec["_url"]), mid, known, today)
                    if new:
                        new["_url"] = rec["_url"]
                        games[mid] = new
                if use_khl and khl:
                    khl.update(probe_many(list(khl)))
        except Exception as e:
            log.warning("цикл не удался: %r", e)
        for mid, rec in list(games.items()):            # законченные матчи показываем ещё 45 минут
            if rec["live"]:
                seen.pop(mid, None)
            else:
                seen.setdefault(mid, t0)
                if t0 - seen[mid] > 2700:
                    games.pop(mid)
        by = {(r["home"], r["away"]): r for r in khl.values()}
        out = []
        for rec in sorted(games.values(), key=lambda r: (r.get("time") or "", r["id"])):
            g = {k: v for k, v in rec.items() if not k.startswith("_")}
            k = by.get((g["home"], g["away"]))
            if k:                                       # статистика из протокола khl.ru, если он нашёлся
                for f in STAT_KEYS:
                    if k.get(f) is not None:
                        g[f] = k[f]
            out.append(g)
        busy = any(g.get("live") for g in out)
        sig = json.dumps(out, sort_keys=True, ensure_ascii=False)
        if busy or sig != last_sig:
            try:
                publish(out)
            except Exception as e:
                log.warning("публикация не удалась: %r", e)
            last_sig = sig
        log.info("идёт игр: %d, в live.json: %d", sum(1 for g in out if g.get("live")), len(out))
        idle = 0 if (busy or schedule_busy()) else idle + 1
        if idle >= 3:
            break
        time.sleep(max(1, INTERVAL - (time.time() - t0)))
    publish([])


if __name__ == "__main__":
    main()
