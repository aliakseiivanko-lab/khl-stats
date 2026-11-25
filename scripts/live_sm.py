"""Источник живых данных №2: sportmail.ru (страницы матчей КХЛ). Отдаёт счёт, периоды, ход матча
(голы и удаления) и командную статистику. Разбор идёт по тексту страницы, поэтому устойчив к вёрстке."""
import datetime as dt, json, logging, re, time, urllib.request
from html.parser import HTMLParser
from pathlib import Path

log = logging.getLogger("live")
BASE = "https://sportmail.ru"
HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
       "Accept": "text/html,*/*;q=0.8", "Accept-Language": "ru-RU,ru;q=0.9"}
ALIAS = {"торпедо нн": "Торпедо", "шанхай дрэгонс": "Драконы", "шанхайские драконы": "Драконы", "сочи": "ХК Сочи",
         "динамо москва": "Динамо М", "динамо минск": "Динамо Мн", "динамо мск": "Динамо М", "динамо мн": "Динамо Мн",
         "металлург": "Металлург Мг", "металлург магнитогорск": "Металлург Мг", "металлург мг": "Металлург Мг",
         "салават юлаев": "Салават Юлаев", "ак барс": "Ак Барс", "хк сочи": "ХК Сочи", "динамо м": "Динамо М"}


def get(url: str) -> str:
    u = url if url.startswith("http") else BASE + url
    u += ("&" if "?" in u else "?") + f"_={int(time.time())}"       # обход кэша CDN: страница матча должна быть свежей
    hdr = {**HDR, "Cache-Control": "no-cache", "Pragma": "no-cache"}
    with urllib.request.urlopen(urllib.request.Request(u, headers=hdr), timeout=25) as r:
        return r.read().decode("utf-8", "replace")


class _T(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out, self.skip, self.links = [], 0, []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        if tag == "a":
            h = dict(attrs).get("href") or ""
            if re.search(r"/hockey-khl/\d+/match/\d+", h):
                self.links.append(h)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1

    def handle_data(self, d):
        if not self.skip and d.strip():
            self.out.append(d.strip())


def text_of(html: str):
    p = _T()
    p.feed(html)
    return re.sub(r"\s+", " ", " ".join(p.out).replace("\xa0", " ")), p.links


def match_links(html: str) -> list[str]:
    _, links = text_of(html)
    html_links = re.findall(r'href=["\']([^"\']*/hockey-khl/\d+/match/\d+/?)["\']', html)
    out, seen = [], set()
    for h in links + html_links:
        m = re.search(r"/hockey-khl/(\d+)/match/(\d+)", h)
        if m and m.group(2) not in seen:
            seen.add(m.group(2))
            out.append(f"/hockey-khl/{m.group(1)}/match/{m.group(2)}/")
    return out


def norm(name: str, known: list[str]) -> str | None:
    n = re.sub(r"\s+", " ", name.strip().strip("«»\"")).casefold()
    if n in ALIAS:
        return ALIAS[n]
    for k in known:
        if k.casefold() == n:
            return k
    for k in known:                                   # «Динамо Минск» ~ «Динамо Мн» и т.п.
        if n.startswith(k.casefold()) or k.casefold().startswith(n):
            return k
    return None


_ROSTER: dict | None = None


def _key(name: str):
    w = name.replace("ё", "е").replace("Ё", "Е").replace("..", ".").split()
    return (w[0].casefold(), w[1][0].casefold()) if len(w) >= 2 else None


def roster() -> dict:
    """{команда: {(фамилия, инициал)}} по протоколам сезона (data/*/players.json) — чтобы понять, чей игрок получил удаление."""
    global _ROSTER
    if _ROSTER is None:
        _ROSTER = {}
        try:
            for f in (Path(__file__).resolve().parent.parent / "data").glob("*/players.json"):
                for g in json.loads(f.read_text(encoding="utf-8")).values():
                    for row in g.get("p", []):
                        k = _key(row[2]) if len(row) > 2 and isinstance(row[2], str) else None
                        if k:
                            _ROSTER.setdefault(g["h"] if row[0] == 0 else g["a"], set()).add(k)
        except Exception as e:
            log.warning("sportmail: ростеры не загружены: %r", e)
    return _ROSTER


def team_of(player: str, home: str, away: str):
    k = _key(player)
    r = roster()
    inh, ina = bool(k and k in r.get(home, ())), bool(k and k in r.get(away, ()))
    return home if inh and not ina else away if ina and not inh else None


def _mins(t: str) -> int:
    m, s = t.split(":")
    return int(m) * 60 + int(s)


def parse_match(html: str, mid: str, known: list[str], today: str) -> dict | None:
    """Страница матча -> запись для live.json (или None, если не сегодняшняя/не удалось разобрать)."""
    text, _ = text_of(html)
    names = []                                        # команды — по ссылкам /team/<id>/ в шапке матча (не зависит от <title>)
    for m in re.finditer(r'<a[^>]+href="[^"]*/team/\d+/?"[^>]*>(.*?)</a>', html, re.S):
        n = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if n and n not in names:
            names.append(n)
        if len(names) == 2:
            break
    if len(names) < 2:                                 # запасной вариант: заголовок страницы
        tm = (re.search(r"<title>\s*Матч\s+(.+?)\s+[-—]\s+(.+?),\s*\d", html)
              or re.search(r"<title>\s*([^<]+?)\s+[—-]\s+([^<]+?)\s+[—-]\s+КХЛ", html))
        if not tm:
            log.info("sportmail %s: не нашёл названия команд", mid)
            return None
        names = [tm.group(1), tm.group(2)]
    home, away = norm(names[0], known), norm(names[1], known)
    if not home or not away:
        log.warning("sportmail %s: не знаю команды %r / %r", mid, names[0], names[1])
        return None
    dm = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
    date = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}" if dm else ""
    if date != today:
        log.info("sportmail %s: %s — %s, дата %s (не сегодня)", mid, home, away, date or "?")
        return None
    started = "Матч не начался" not in text
    finished = "Матч завершен" in text or "Матч завершён" in text
    seg = text.split("мин.", 1)[1] if "мин." in text else text
    seg = seg.split("Стадион", 1)[0]
    log.info("sportmail %s: шапка: %s", mid, seg[:140])
    sc = re.search(r"(?<![\d:])(\d+) : [^\d:]+? (\d+)(?![\d:])", seg)
    score = [int(sc.group(1)), int(sc.group(2))] if sc else [0, 0]
    per = None
    if sc:
        pm = re.match(r"\s*((?:\d+:\d+(?:, )?)+)", seg[sc.end():])
        if pm:
            per = [x.strip() for x in pm.group(1).strip(", ").split(",") if x.strip()]
    st = re.search(r"назначена на (\d{1,2}:\d{2})", text)
    log.info("sportmail %s: %s — %s, начался=%s, завершён=%s", mid, home, away, started, finished)
    if not started and sc:                            # в шапке уже есть счёт, хотя надпись «не начался» не поменялась
        started = True
    elapsed = None                                       # сколько минут прошло с начала по расписанию
    if st and date:
        try:
            t0_ = dt.datetime.fromisoformat(f"{date}T{st.group(1).zfill(5)}").replace(tzinfo=dt.timezone(dt.timedelta(hours=3)))
            elapsed = (dt.datetime.now(dt.timezone(dt.timedelta(hours=3))) - t0_).total_seconds() / 60
        except Exception:
            pass
    if sc and not finished:                              # на странице есть счёт — игра началась, что бы ни писал статус
        started = True
    if started and not finished and elapsed is not None and (elapsed >= 210 or (per and len(per) >= 3 and elapsed >= 150)):
        finished = True                                  # статус не обновился, но по времени и периодам игра уже закончена
    stale = False
    if not started and st:                             # страница не обновилась, хотя по времени игра уже идёт
        try:
            t0 = dt.datetime.fromisoformat(f"{date}T{st.group(1).zfill(5)}").replace(tzinfo=dt.timezone(dt.timedelta(hours=3)))
            now = dt.datetime.now(dt.timezone(dt.timedelta(hours=3)))
            if t0 + dt.timedelta(minutes=3) <= now <= t0 + dt.timedelta(hours=3, minutes=30):
                started, stale = True, True
                log.warning("sportmail %s: по времени игра идёт, а страница говорит «не начался» — источник отстаёт", mid)
        except Exception:
            pass
    if started and not finished and st:
        try:
            t1 = dt.datetime.fromisoformat(f"{date}T{st.group(1).zfill(5)}").replace(tzinfo=dt.timezone(dt.timedelta(hours=3)))
            if dt.datetime.now(dt.timezone(dt.timedelta(hours=3))) > t1 + dt.timedelta(hours=3, minutes=15):
                finished = True                       # статус на странице не меняется — считаем по времени
        except Exception:
            pass
    rec = {"id": int(mid), "source": "sportmail", "home": home, "away": away, "date": date, "score": score,
           "live": started and not finished, "started": started, "finished": finished,
           "status": "" if not started else ("Матч завершён" if finished else "")}
    if st:
        rec["time"] = st.group(1)
    if stale and not sc:
        rec["nodata"] = True
        rec["status"] = "Идёт матч"
    if per:
        rec["periods"] = per
    ar = re.search(r"Стадион: (.+?) (?:вместимость|Главный судья|Информация о матче|Все коэффициенты|Превью|Трансляция|Протокол)", text)
    if ar:
        rec["arena"] = ar.group(1).strip()
    # ход матча: от «Источник:» до «Обозначения»
    ev_txt = text
    if "Источник:" in ev_txt:
        ev_txt = ev_txt.split("Источник:", 1)[1].split("Обозначения", 1)[0]
        ev_txt = re.sub(r"^\s*\S+(?: \S+)?\s+(?=\d{1,2}:\d{2} )", "", ev_txt) if not re.match(r"\s*\d{1,2}:\d{2}", ev_txt) else ev_txt
    events = []
    pat = re.compile(r"(\d{1,3}:\d{2}) (?:Гол! ?\( ?(\d+) ?: ?(\d+) ?\) ?Шайбу забросил (.+?)(?= \d{1,3}:\d{2} (?:Гол|Удаление|Командный|Буллит)|$)"
                     r"|Удаление (.+?) ?, ?(\d+) мин|Командный штраф)")
    for m in pat.finditer(ev_txt):
        t = m.group(1)
        if m.group(2) is not None:
            who, ast = m.group(4).strip(), ""
            am = re.match(r"(.+?) \((.*)\)$", who)
            if am:
                who, ast = am.group(1), am.group(2).replace("..", ".")
            ev = {"t": t, "k": "goal", "s": [int(m.group(2)), int(m.group(3))], "p": who, "a": ast}
            tm_ = team_of(who, home, away)
            if tm_:
                ev["h"] = 0 if tm_ == home else 1
            events.append(ev)
        elif m.group(5):
            events.append({"t": t, "k": "pen", "p": m.group(5).strip(), "m": int(m.group(6)),
                           "team": team_of(m.group(5).strip(), home, away)})
        else:
            events.append({"t": t, "k": "pen", "p": "Командный штраф", "m": 2})
    events.sort(key=lambda e: _mins(e["t"]))
    if events:
        rec["events"] = events[-40:]
        if score == [0, 0] and any(e["k"] == "goal" for e in events):
            g = [e for e in events if e["k"] == "goal"][-1]
            rec["score"] = g["s"]
        last = _mins(events[-1]["t"]) // 60
        if rec["live"]:
            rec["status"] = f"{min(last // 20 + 1, 3)}-й период" if last < 60 else "Овертайм"
    if rec["live"]:
        ps = re.search(r"(\d-й период|Перерыв|Овертайм|Буллиты)", seg)
        if ps:
            rec["status"] = ps.group(1)
    pens = [e for e in events if e["k"] == "pen"]               # на странице нет командной статистики — считаем из хода матча
    if pens and all(e.get("team") for e in pens):
        rec["pen"] = [sum(1 for e in pens if e["team"] == x) for x in (home, away)]
        rec["pim"] = [sum(e["m"] for e in pens if e["team"] == x) for x in (home, away)]
    # командная статистика
    stats = []
    sm = re.search(r"Статистика (.+?)(?: История личных встреч| Последние игры|$)", text)
    if sm:
        body = sm.group(1)
        body = re.sub(r"^.{0,80}?(?= [А-ЯЁ][а-яё ]+ \d+ \d+)", "", body, count=1)
        for m in re.finditer(r"([А-ЯЁA-Z][^\d]{2,40}?) (\d+(?:\.\d+)?%?) (\d+(?:\.\d+)?%?)(?= [А-ЯЁA-Z]|$)", body):
            stats.append([m.group(1).strip(), m.group(2), m.group(3)])
    if stats:
        rec["stats"] = stats
    return rec
