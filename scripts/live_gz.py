"""Источник живых данных: Газета.Ру (gazeta.ru) — текстовая трансляция «Чемпионата» на странице матча.
Страницы отдаются готовым HTML: счёт, период, голы с минутой и автором, удаления, статистика матча.
Список матчей по датам — на https://www.gazeta.ru/sport/hockey/khl/"""
import datetime as dt, logging, re, time, urllib.request
from html.parser import HTMLParser

import live_khl as K
import live_sm as SM

log = logging.getLogger("live")
LIST_URL = "https://www.gazeta.ru/sport/hockey/khl/"
BASE = "https://www.gazeta.ru"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MONTHS = {m: i + 1 for i, m in enumerate("января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split())}
HREF = re.compile(r"/sport/matches/hockey/khl/(\d{4}-\d{4})/(\d+)\.shtml")


def get(url: str) -> str:
    u = url if url.startswith("http") else BASE + url
    u += ("&" if "?" in u else "?") + f"_={int(time.time())}"
    req = urllib.request.Request(u, headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.8",
                                             "Accept-Language": "ru-RU,ru;q=0.9", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


class _Tok(HTMLParser):
    """Поток «текст / ссылка» в порядке документа."""
    def __init__(self):
        super().__init__()
        self.out, self.skip, self.a = [], 0, None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        if tag == "a":
            self.a = [dict(attrs).get("href") or "", []]

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1
        if tag == "a" and self.a is not None:
            self.out.append(("a", self.a[0], re.sub(r"\s+", " ", " ".join(self.a[1])).strip()))
            self.a = None

    def handle_data(self, d):
        if self.skip or not d.strip():
            return
        if self.a is not None:
            self.a[1].append(d.strip())
        else:
            self.out.append(("t", d.strip()))


def list_matches(html: str, today: str, known: list[str]) -> list[dict]:
    """Матчи сегодняшнего дня из общего списка: [{mid, home, away, time}]."""
    tp = _Tok()
    tp.feed(html)
    ty = int(today[:4])
    cur, out = None, []
    for tok in tp.out:
        if tok[0] == "t":
            m = re.fullmatch(r"(\d{1,2}) (%s)" % "|".join(MONTHS), tok[1])
            if m:
                mo = MONTHS[m.group(2)]
                cur = f"{ty + (1 if mo < 6 and int(today[5:7]) > 8 else 0)}-{mo:02d}-{int(m.group(1)):02d}"
        elif cur == today:
            hm = HREF.search(tok[1])
            if not hm:
                continue
            txt = re.sub(r"\s+(Live|LIVE)\b", "", tok[2])
            m = re.match(r"(.+?) [—-] (.+?)(?: (\d+ : \d+|\d{1,2}:\d{2}))?$", txt)
            if not m:
                continue
            h, a = SM.norm(m.group(1), known), SM.norm(m.group(2), known)
            if h and a:
                tm = m.group(3) if m.group(3) and re.fullmatch(r"\d{1,2}:\d{2}", m.group(3)) else ""
                out.append({"mid": hm.group(2), "href": tok[1], "home": h, "away": a, "time": tm.zfill(5) if tm else "",
                            "live": bool(re.search(r"\bLive\b", tok[2], re.I))})
    seen, uniq = set(), []
    for x in out:
        if x["mid"] not in seen:
            seen.add(x["mid"])
            uniq.append(x)
    return uniq


def _per(minute: int) -> int:
    return 1 if minute <= 20 else 2 if minute <= 40 else 3 if minute <= 60 else 4


def _team_of(gen: str, home: str, away: str) -> str | None:
    g = gen.strip("«» ").casefold()
    for t in (home, away):
        stem = t.casefold()[:max(4, len(t) - 2)]
        if g.startswith(stem):
            return t
    return None


def parse_match(html: str, item: dict) -> dict | None:
    L = K.lines_of(html)
    home, away = item["home"], item["away"]
    si = next((i for i, l in enumerate(L) if re.fullmatch(r"\d+ : \d+", l)), None)
    if si is None:
        return None
    score = [int(x) for x in L[si].split(" : ")]
    status_line = L[si - 1] if si > 0 else ""
    ti = next((i for i, l in enumerate(L) if l == "Трансляция"), len(L))
    head = L[:ti]
    full = "\n".join(L)
    finished = "Окончен" in head[max(0, si - 3):si + 1] or "Игра окончена" in full
    # блоки ленты: минута -> строки до следующей минуты
    feed, cur = [], None
    for l in L[ti:]:
        m = re.fullmatch(r"(\d{1,3})'", l)
        if m:
            cur = [int(m.group(1)), []]
            feed.append(cur)
        elif cur is not None:
            cur[1].append(l)
    started = any(re.search(r"стартовала|Первый период начался|Начался первый период", " ".join(b[1])) for b in feed) or len(feed) > 2 or score != [0, 0]
    # статус
    status = ""
    for b in feed:                                      # самое свежее событие периода — первое сверху
        txt = " ".join(b[1])
        m = re.search(r"(Перв|Втор|Трет)\w+ (?:период|двадцатиминутка)\w* (?:начал|окончен)", txt) or \
            re.search(r"(первый|второй|третий) (?:перерыв|период)", txt, re.I)
        if finished:
            status = "Матч завершён"
            break
        if re.search(r"Период окончен|период окончен|Первый период окончен|перерыв", txt):
            n = 1 if b[0] <= 20 else 2 if b[0] <= 40 else 3
            status = f"Перерыв после {n}-го периода"
            break
        if re.search(r"период начался|Овертайм начался|начался", txt, re.I) and b[0] > 0:
            status = "Овертайм" if b[0] >= 60 else f"{_per(max(b[0], 1))}-й период"
            break
    if not status and feed and not finished:
        status = f"{_per(max(feed[0][0], 1))}-й период" if feed[0][0] <= 60 else "Овертайм"
    # точные минуты голов из шапки: «28:06' Окулов»
    hg = [(int(m.group(1)), int(m.group(2)), m.group(3)) for l in head for m in [re.fullmatch(r"(\d{1,2}):(\d{2})' (.+)", l)] if m]
    events, prev = [], [0, 0]
    for minute, lines in reversed(feed):               # от старых к новым
        txt = " ".join(lines)
        g = next((re.match(r"Шайба! (\d+):(\d+)", l) for l in lines if re.match(r"Шайба! (\d+):(\d+)", l)), None)
        if g:
            s = [int(g.group(1)), int(g.group(2))]
            side = 0 if s[0] > prev[0] else 1
            prev = s
            who = next((l for l in lines if not l.startswith(("Шайба", "ГОЛ")) and len(l) < 40), "")
            desc = next((l[4:].lstrip("! ").strip() for l in lines if l.startswith("ГОЛ")), "")
            t = None
            sur = who.split()[-1] if who else ""
            for mm, ss, nm in hg:
                if mm + 1 == minute and sur and sur in nm:
                    t = f"{mm:02d}:{ss:02d}"
                    break
            events.append({"k": "goal", "s": s, "h": side, "p": who or "?", "a": "", "d": desc[:260], "per": _per(minute), "t": t})
            continue
        pm = next((re.match(r"Удаление у (.+?)\.", l) for l in lines if re.match(r"Удаление у", l)), None)
        if pm:
            who = next((l for l in lines if not l.startswith("Удаление") and len(l) < 40), "")
            mins = 2
            if re.search(r"пять минут|5 минут", txt):
                mins = 5
            elif re.search(r"десять|10 минут", txt):
                mins = 10
            events.append({"k": "pen", "p": who or "?", "m": mins, "team": _team_of(pm.group(1), home, away),
                           "d": txt.split(".", 1)[-1].strip()[:200], "per": _per(minute), "t": None})
    # статистика матча (самый свежий блок)
    stats = []
    si2 = next((i for i, l in enumerate(L) if l.startswith("Статистика матча")), None)
    if si2 is not None:
        for l in L[si2 + 1:si2 + 14]:
            m = re.fullmatch(r"(.+?): (\d+(?::\d+)?)-(\d+(?::\d+)?);?", l)
            if m:
                stats.append([m.group(1).strip(), m.group(2), m.group(3)])
            elif stats:
                break
    arena = next((re.sub(r"^Стадион\s*", "", l) for l in L if l.startswith("Стадион")), "")
    rec = {"id": int(item["mid"]), "source": "gazeta", "home": home, "away": away, "score": score,
           "live": started and not finished, "started": started, "finished": finished, "status": status,
           "events": events}
    if item.get("time"):
        rec["time"] = item["time"]
    if stats:
        rec["stats"] = stats
    if arena:
        rec["arena"] = arena[:80]
    log.info("gazeta %s: %s — %s %d:%d, %s, событий %d, начался=%s, завершён=%s", item["mid"], home, away, score[0], score[1],
             status or "-", len(events), started, finished)
    return rec
