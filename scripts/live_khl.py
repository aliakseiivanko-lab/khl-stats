"""Источник живых данных №1: официальная текстовая трансляция КХЛ (text.khl.ru/en/<id>.html).
Номер игры тот же, что у протоколов khl.ru. Страница обновляется по ходу матча: голы с ассистентами,
начало/конец периодов, окончание игры. Время гола на странице не разобрать надёжно — показываем периоды."""
import datetime as dt, logging, re, urllib.error, urllib.request
from html.parser import HTMLParser

log = logging.getLogger("live")
URL = "https://text.khl.ru/en/{}.html"
HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
       "Accept": "text/html,*/*;q=0.8", "Accept-Language": "en,ru;q=0.8"}
EN = {"avangard": "Авангард", "avtomobilist": "Автомобилист", "admiral": "Адмирал", "ak bars": "Ак Барс", "amur": "Амур",
      "barys": "Барыс", "dynamo msk": "Динамо М", "dynamo moscow": "Динамо М", "dinamo msk": "Динамо М",
      "dynamo mn": "Динамо Мн", "dynamo minsk": "Динамо Мн", "dinamo mn": "Динамо Мн", "dinamo minsk": "Динамо Мн",
      "shanghai dragons": "Драконы", "dragons": "Драконы", "lada": "Лада", "lokomotiv": "Локомотив",
      "metallurg mg": "Металлург Мг", "metallurg": "Металлург Мг", "neftekhimik": "Нефтехимик", "ska": "СКА",
      "salavat yulaev": "Салават Юлаев", "severstal": "Северсталь", "sibir": "Сибирь", "spartak": "Спартак",
      "torpedo": "Торпедо", "traktor": "Трактор", "hc sochi": "ХК Сочи", "sochi": "ХК Сочи", "cska": "ЦСКА"}
MON = {m: i + 1 for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}
_BLOCK = {"div", "p", "li", "tr", "br", "h1", "h2", "h3", "h4", "section", "table", "ul", "td", "article"}


class NotFound(Exception):
    pass


import http.cookiejar, os, threading

_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_JAR))
_LOCK = threading.Lock()


def _get(url: str, extra: dict | None = None) -> str:
    hdr = {**HDR, "Referer": "https://text.khl.ru/", **(extra or {})}
    with _OPENER.open(urllib.request.Request(url, headers=hdr), timeout=25) as r:
        return r.read().decode("utf-8", "replace")


class Blocked(Exception):
    pass


def fetch(gid: int) -> str:
    """Страница трансляции. Сервер сначала отвечает 307 с cookie и только потом отдаёт страницу — нужна «банка» куки."""
    import http.cookiejar, os
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    hdr = {**HDR, "Referer": "https://www.khl.ru/"}
    if os.getenv("KHL_COOKIE"):
        hdr["Cookie"] = os.getenv("KHL_COOKIE")
    try:
        with op.open(urllib.request.Request(URL.format(gid), headers=hdr), timeout=25) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            raise NotFound(gid)
        raise Blocked(f"HTTP {e.code}")
    except urllib.error.URLError as e:
        raise Blocked(str(e.reason)[:120])


class _L(HTMLParser):
    def __init__(self):
        super().__init__()
        self.lines, self.cur, self.skip = [], [], 0

    def _flush(self):
        t = re.sub(r"\s+", " ", "".join(self.cur).replace("\xa0", " ")).strip()
        if t:
            self.lines.append(t)
        self.cur = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        elif tag in _BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1
        elif tag in _BLOCK:
            self._flush()

    def handle_data(self, d):
        if not self.skip:
            self.cur.append(d)


def lines_of(html: str) -> list[str]:
    p = _L()
    p.feed(html)
    p._flush()
    return p.lines


def ru(name: str, sched_today: list[tuple[str, str]], other: str | None = None) -> str | None:
    n = name.strip().casefold()
    if n in EN:
        return EN[n]
    for k, v in EN.items():                           # «KHL Ural Stars» не подойдёт, а «Dynamo Msk.» подойдёт
        if n.startswith(k) or k.startswith(n):
            return v
    return None


def parse(html: str, gid: int, today: str, sched_today: list[tuple[str, str]]) -> dict | None:
    L = lines_of(html)
    di = next((i for i, l in enumerate(L) if re.fullmatch(r"\d{1,2} [A-Z][a-z]{2} \d{4}, \d{1,2}:\d{2}", l)), None)
    if di is None:
        return None
    dm = re.match(r"(\d{1,2}) ([A-Z][a-z]{2}) (\d{4}), (\d{1,2}:\d{2})", L[di])
    date = f"{dm.group(3)}-{MON.get(dm.group(2), 0):02d}-{int(dm.group(1)):02d}"
    if abs((dt.date.fromisoformat(date) - dt.date.fromisoformat(today)).days) > 1:
        return None
    names = [l for l in L[max(0, di - 6):di] if not re.fullmatch(r"[\d\s:–-]*", l) and l.lower() not in ("finished", "live", "online")][-2:]
    if len(names) < 2:
        return None
    en_h, en_a = names
    home, away = ru(en_h, sched_today), ru(en_a, sched_today)
    if not home or not away:                           # неизвестное английское название — достроим по расписанию на сегодня
        for h, a in sched_today:
            if home == h and not away:
                away = a
            elif away == a and not home:
                home = h
    if not home or not away:
        log.warning("text.khl.ru %s: не знаю команды %r / %r", gid, en_h, en_a)
        return None
    full = "\n".join(L)
    began = "Begin of game" in full
    finished = "End of game" in full
    arena = L[di + 1] if di + 1 < len(L) and not L[di + 1].startswith("Officials") else None
    # события идут от новых к старым: идём с конца, чтобы знать период и копить счёт
    per, score, events, status = 0, [0, 0], [], ""
    for l in reversed(L):
        m = re.match(r"Begin of (\d) period", l)
        if m:
            per = int(m.group(1))
            continue
        if re.match(r"Begin of (overtime|OT)", l, re.I):
            per = 4
            continue
        g = re.match(r"Goal: (.+?)\. (\d+)\. (.+?)(?:, assists: (.+))?$", l)
        if not g:
            continue
        side = 0 if g.group(1).strip().casefold() == en_h.strip().casefold() else 1 if g.group(1).strip().casefold() == en_a.strip().casefold() else None
        if side is None:
            continue
        score[side] += 1
        ast = [a.strip() for a in re.findall(r"\d+\. (.+?)(?=, \d+\. |$)", g.group(4) or "")]
        events.append({"k": "goal", "s": list(score), "h": side, "p": g.group(3).strip(), "a": ", ".join(ast), "per": per or 1})
    for l in L:                                        # самый свежий маркер — первый сверху
        m = re.match(r"(Begin|End) of (\d) period", l)
        if finished:
            status = "Матч завершён"
            break
        if m:
            status = f"{m.group(2)}-й период" if m.group(1) == "Begin" else f"Перерыв после {m.group(2)}-го периода"
            break
        if re.match(r"(Begin|End) of (overtime|OT)", l, re.I):
            status = "Овертайм"
            break
        if l.startswith("Shootout"):
            status = "Буллиты"
            break
    ad = next((re.match(r"(\d+) \(audience\)", l).group(1) for l in L if re.match(r"\d+ \(audience\)", l)), None)
    rec = {"id": gid, "source": "khl", "home": home, "away": away, "date": date, "time": dm.group(4).zfill(5),
           "score": score, "live": began and not finished, "started": began, "finished": finished, "status": status,
           "events": events}
    if arena:
        rec["arena"] = arena
    if ad:
        rec["att"] = int(ad)
    log.info("text.khl.ru %s: %s — %s, голов %d, начался=%s, завершён=%s, %s", gid, home, away, len(events), began, finished, status)
    return rec
