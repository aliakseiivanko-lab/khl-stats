"""Живая статистика матчей КХЛ.

Пока идут игры, раз в ~30 секунд качает незавершённые протоколы khl.ru, разбирает их и публикует
live.json в ветку `live` репозитория (сайт читает его с raw.githubusercontent.com).
Запускается из .github/workflows/live.yml; вне игровых окон завершается сразу."""
import datetime as dt, json, logging, os, re, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
import protocols as P
import live_sm as SM
import live_khl as KT
import live_cal as CAL
import live_gz as GZ

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


def sched_today(today: str) -> list[tuple[str, str]]:
    try:
        g = json.loads((ROOT / "site/data/schedule.json").read_text(encoding="utf-8"))["games"]
        return [(x["h"], x["a"]) for x in g if x.get("d") == today]
    except Exception:
        return []


def sched_times(today: str) -> dict:
    """Время начала по расписанию (МСК): на text.khl.ru время может быть местным по арене."""
    try:
        g = json.loads((ROOT / "site/data/schedule.json").read_text(encoding="utf-8"))["games"]
        return {(x["h"], x["a"]): x["t"] for x in g if x.get("d") == today}
    except Exception:
        return {}


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


_WARNED: set = set()


def khl_probe(gid, today, sched):
    try:
        html = KT.fetch(gid)
        rec = KT.parse(html, gid, today, sched)
        if rec is None and not getattr(khl_probe, "sample", False):
            khl_probe.sample = True
            log.info("text.khl.ru %s: страница получена, но игра не распознана; начало текста: %r", gid, KT.lines_of(html)[:10])
        return gid, rec
    except KT.NotFound:
        return gid, None
    except Exception as e:
        key = type(e).__name__ + str(e)[:40]
        if key not in _WARNED:                          # одну и ту же ошибку пишем в лог один раз
            _WARNED.add(key)
            log.warning("text.khl.ru %s: %r", gid, e)
        return gid, None


def khl_many(ids, today, sched, workers=6):
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for gid, rec in ex.map(lambda g: khl_probe(g, today, sched), ids):
            if rec:
                out[gid] = rec
    return out


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


def merge(khl_recs: list[dict], sm_recs: list[dict]) -> list[dict]:
    """Официальная трансляция КХЛ — основа; sportmail дополняет (русские имена и минуты голов, штрафы, статистика)."""
    out, used = [], set()
    for k in khl_recs:
        g = {kk: v for kk, v in k.items() if not kk.startswith("_")}
        m = next((x for x in sm_recs if (x["home"], x["away"]) == (k["home"], k["away"])), None)
        if m and not m.get("nodata"):
            used.add(m["id"])
            kg = [e for e in k.get("events", []) if e["k"] == "goal"]
            sg = [e for e in m.get("events", []) if e["k"] == "goal"]
            if sg and len(sg) == len(kg):               # то же число голов — берём события sportmail (по-русски, с минутами)
                g["events"] = [{kk: v for kk, v in e.items()} for e in m["events"]]
            elif m.get("events"):
                g["events_sm"] = m["events"]
            for f in ("periods", "stats"):
                if m.get(f):
                    g[f] = m[f]
            if not g.get("arena") and m.get("arena"):
                g["arena"] = m["arena"]
        out.append(g)
    for m in sm_recs:
        if m["id"] not in used and not any((m["home"], m["away"]) == (g["home"], g["away"]) for g in out):
            out.append({kk: v for kk, v in m.items() if not kk.startswith("_")})
    return out


def add_cal(out: list[dict], cal: list[dict]) -> list[dict]:
    """Блок «Сейчас» на календаре khl.ru — самый надёжный источник счёта и периода: им поправляем остальные."""
    for c in cal:
        g = next((x for x in out if (x["home"], x["away"]) == (c["home"], c["away"])), None)
        if g is None:
            out.append(dict(c))
        elif g.get("source") not in ("khl", "gazeta"):
            g.update(score=c["score"], live=True, started=True, finished=False, status=c["status"])
            g.pop("nodata", None)
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
    deadline = time.time() + RUN_MIN * 60
    sm: dict[str, dict] = {}
    gz: dict[str, dict] = {}
    gz_items: list[dict] = []
    next_list = 0.0
    was_live, seen, cl = set(), {}, {}
    next_scan, idle, last_sig = 0.0, 0, None
    while time.time() < deadline:
        t0 = time.time()
        today = dt.datetime.now(MSK).date().isoformat()
        try:
            cal_now = {(c["home"], c["away"]): c for c in CAL.parse(CAL.fetch(), known, today)}
        except Exception as e:
            cal_now = {}
            if not getattr(main, "cal_warned", False):
                main.cal_warned = True
                log.warning("khl.ru/calendar недоступен: %r", e)
        for pair, c in cal_now.items():
            cl[pair] = c
            was_live.add(pair)
        for pair, c in cl.items():                       # матч пропал из блока «Сейчас» — значит закончился
            if pair not in cal_now and c["live"]:
                c.update(live=False, finished=True, status="Матч завершён")
        try:
            if t0 >= next_scan:                         # раз в несколько минут ищем новые матчи
                for mid, rec in sm_scan(known, today).items():
                    if rec["live"] or (rec["finished"] and (rec["home"], rec["away"]) in was_live):
                        sm[mid] = keep_newer(sm.get(mid), rec)
                next_scan = t0 + RESCAN
            else:
                for mid, rec in list(sm.items()):
                    if rec["live"]:
                        new = SM.parse_match(SM.get(rec["_url"]), mid, known, today)
                        if new:
                            new["_url"] = rec["_url"]
                            sm[mid] = keep_newer(rec, new)
        except Exception as e:
            log.warning("цикл не удался: %r", e)
        try:                                            # Газета.Ру: страницы матчей с ходом игры
            if t0 >= next_list:
                gz_items = GZ.list_matches(GZ.get(GZ.LIST_URL), today, known)
                next_list = t0 + 120
                log.info("gazeta: матчей сегодня в списке: %d (с меткой Live: %d)", len(gz_items), sum(1 for x in gz_items if x.get("live")))
            nowm = dt.datetime.now(MSK)
            for it in gz_items:
                pair = (it["home"], it["away"])
                due = bool(it.get("live")) or pair in was_live
                if not due and it.get("time"):
                    try:
                        st = dt.datetime.fromisoformat(f"{today}T{it['time']}").replace(tzinfo=MSK)
                        due = st - dt.timedelta(minutes=10) <= nowm <= st + dt.timedelta(hours=4)
                    except Exception:
                        pass
                if not due:
                    continue
                rec = GZ.parse_match(GZ.get(it["href"]), it)
                if rec and (rec["live"] or (rec["finished"] and pair in was_live)):
                    gz[it["mid"]] = rec
        except Exception as e:
            if not getattr(main, "gz_warned", False):
                main.gz_warned = True
                log.warning("gazeta.ru недоступна: %r", e)
        for src in (gz, sm, cl):
            for key, rec in list(src.items()):
                pair = (rec["home"], rec["away"])
                if rec["live"]:
                    was_live.add(pair)
                    seen.pop((id(src), key), None)
                else:                                   # законченные матчи показываем ещё 45 минут
                    seen.setdefault((id(src), key), t0)
                    if t0 - seen[(id(src), key)] > 2700:
                        src.pop(key)
        out = merge(sorted(gz.values(), key=lambda r: (r.get("time") or "", r["id"])), list(sm.values()))
        out = add_cal(out, [c for c in cl.values()])
        stt = sched_times(today)
        for g in out:
            if (g["home"], g["away"]) in stt:
                g["time"] = stt[(g["home"], g["away"])]
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
