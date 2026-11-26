"""Живая статистика матчей КХЛ.

Пока идут игры, раз в ~15 секунд качает текстовые трансляции sportmail.ru, разбирает их и публикует
live.json в ветку `live` репозитория (сайт читает его с raw.githubusercontent.com).
Запускается из .github/workflows/live.yml; вне игровых окон завершается сразу."""
import datetime as dt, json, logging, os, re, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
import live_sm as SM

log = logging.getLogger("live")
ROOT = Path(__file__).resolve().parent.parent
MSK = dt.timezone(dt.timedelta(hours=3))
INTERVAL = int(os.getenv("LIVE_INTERVAL", "15"))        # период обновления, сек
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
    for attempt in (1, 2, 3):                           # GitHub иногда отвечает 500 — пробуем ещё раз
        try:
            git("push", "-q", "-f", f"https://x-access-token:{tok}@github.com/{repo}.git", "live:live")
            break
        except RuntimeError:
            if attempt == 3:
                raise
            time.sleep(2 * attempt)
    log.info("live.json опубликован в ветку live (%d игр)", len(games))


def sched_times(today: str) -> dict:
    """Время начала по расписанию (МСК): на text.khl.ru время может быть местным по арене."""
    try:
        g = json.loads((ROOT / "site/data/schedule.json").read_text(encoding="utf-8"))["games"]
        return {(x["h"], x["a"]): x["t"] for x in g if x.get("d") == today}
    except Exception:
        return {}


def progress(r: dict) -> tuple:
    """Насколько «далеко» зашла игра по записи: нужно, чтобы устаревшая копия страницы не затирала свежую."""
    m = re.search(r"(\d)-й", r.get("status") or "")
    per = int(m.group(1)) if m else (4 if r.get("status") == "Овертайм" else 0)
    sc = r.get("score") or [0, 0]
    return (1 if r.get("finished") else 0, per, sc[0] + sc[1], len(r.get("events") or []), 1 if r.get("started") else 0)


def keep_newer(old: dict | None, new: dict) -> dict:
    """sportmail иногда отдаёт старую версию страницы (CDN): не даём записи откатиться назад."""
    if old and progress(new) < progress(old):
        log.info("sportmail %s: пришла устаревшая версия страницы %s — оставляю предыдущую", new.get("id"), progress(new))
        return old
    return new


def sm_scan(known, today):
    """Ищет на sportmail.ru матчи КХЛ за сегодня: список ссылок -> страницы матчей (параллельно)."""
    links = []
    for page in ("/hockey-khl/", "/hockey/", "/"):
        try:
            links += [l for l in SM.match_links(SM.get(page)) if l not in links]
        except Exception as e:
            log.warning("sportmail %s: %r", page, e)
    log.info("sportmail: найдено ссылок на матчи КХЛ: %d", len(links))

    def one(l):
        mid = l.rstrip("/").rsplit("/", 1)[1]
        try:
            rec = SM.parse_match(SM.get(l), mid, known, today)
        except Exception as e:
            log.warning("sportmail %s: %r", l, e)
            return mid, None
        if rec:
            rec["_url"] = l
        return mid, rec

    with ThreadPoolExecutor(max_workers=6) as ex:
        return {mid: rec for mid, rec in ex.map(one, links[:60]) if rec}


def main():
    if not schedule_busy():
        log.info("по расписанию сейчас игр нет — выходим")
        return
    try:
        known = sorted({t for m in json.loads((ROOT / "site/data/matches.json").read_text(encoding="utf-8"))["matches"]
                        for t in (m["home"], m["away"])})
    except Exception:
        known = []
    deadline = time.time() + RUN_MIN * 60
    sm: dict[str, dict] = {}
    was_live, seen = set(), {}
    next_scan, idle, last_sig, last_pub = 0.0, 0, None, 0.0
    chg: dict = {}                                      # когда запись игры реально менялась в последний раз

    def refresh(item):
        mid, rec = item
        if rec.get("_ajax"):                           # то, что страница сама опрашивает каждые 10 с (HTML кэшируется CDN)
            try:
                raw = SM.get(rec["_ajax"], {"X-Requested-With": "XMLHttpRequest", "Referer": rec["_url"],
                                            "Accept": "application/json, text/plain, */*"})
                new = SM.apply_ajax(rec, raw, mid, known)
                if new:
                    return mid, keep_newer(rec, new)
            except Exception as e:
                log.warning("sportmail %s: ajax %r", mid, e)
        try:
            new = SM.parse_match(SM.get(rec["_url"]), mid, known, today)
        except Exception as e:
            log.warning("sportmail %s: %r", mid, e)
            return mid, rec
        if not new:
            return mid, rec
        new["_url"] = rec["_url"]
        return mid, keep_newer(rec, new)

    while time.time() < deadline:
        t0 = time.time()
        today = dt.datetime.now(MSK).date().isoformat()
        try:
            if t0 >= next_scan:                         # раз в несколько минут ищем новые матчи
                for mid, rec in sm_scan(known, today).items():
                    if rec["live"] or (rec["finished"] and (rec["home"], rec["away"]) in was_live):
                        sm[mid] = keep_newer(sm.get(mid), rec)
                next_scan = t0 + RESCAN
            else:                                       # идущие матчи обновляем параллельно
                live_items = [(m, r) for m, r in sm.items() if r["live"]]
                with ThreadPoolExecutor(max_workers=6) as ex:
                    for mid, rec in ex.map(refresh, live_items):
                        sm[mid] = rec
        except Exception as e:
            log.warning("цикл не удался: %r", e)
        for key, rec in list(sm.items()):
            pair = (rec["home"], rec["away"])
            if rec["live"]:
                was_live.add(pair)
                seen.pop(key, None)
            else:                                       # законченные матчи показываем ещё 45 минут
                seen.setdefault(key, t0)
                if t0 - seen[key] > 2700:
                    sm.pop(key)
        out = sorted((dict(r) for r in sm.values()), key=lambda r: (r.get("time") or "", str(r["id"])))
        for g in out:
            for k in [k for k in g if k.startswith("_")]:
                g.pop(k)
        stt = sched_times(today)
        for g in out:
            if (g["home"], g["away"]) in stt:
                g["time"] = stt[(g["home"], g["away"])]
        for g in out:
            body = json.dumps({k: v for k, v in g.items() if k != "changed"}, sort_keys=True, ensure_ascii=False)
            pair = (g["home"], g["away"])
            if pair not in chg or chg[pair][0] != body:
                chg[pair] = (body, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
            g["changed"] = chg[pair][1]
        busy = any(g.get("live") for g in out)
        sig = json.dumps(out, sort_keys=True, ensure_ascii=False)
        # публикуем при изменениях, а пока идут игры — ещё и раз в минуту, чтобы «Данные на…» не застывало
        if sig != last_sig or (busy and t0 - last_pub >= 60):
            try:
                publish(out)
                last_sig, last_pub = sig, t0
            except Exception as e:
                log.warning("публикация не удалась: %r", e)
        log.info("идёт игр: %d, в live.json: %d, цикл %.1f с", sum(1 for g in out if g.get("live")), len(out), time.time() - t0)
        idle = 0 if (busy or schedule_busy()) else idle + 1
        if idle >= 3:
            break
        time.sleep(max(1, INTERVAL - (time.time() - t0)))
    publish([])


if __name__ == "__main__":
    main()
