"""Источник живых данных №2: sportmail.ru (страницы матчей КХЛ). Отдаёт счёт, периоды, ход матча
(голы и удаления) и командную статистику. Разбор идёт по тексту страницы, поэтому устойчив к вёрстке."""
import json, logging, re, urllib.request
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
    with urllib.request.urlopen(urllib.request.Request(url if url.startswith("http") else BASE + url, headers=HDR), timeout=25) as r:
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


def _mins(t: str) -> int:
    m, s = t.split(":")
    return int(m) * 60 + int(s)


def parse_match(html: str, mid: str, known: list[str], today: str) -> dict | None:
    """Страница матча -> запись для live.json (или None, если не сегодняшняя/не удалось разобрать)."""
    text, _ = text_of(html)
    tm = re.search(r"<title>\s*([^<]+?)\s+[—-]\s+([^<]+?)\s+[—-]\s+КХЛ", html)
    if not tm:
        return None
    home, away = norm(tm.group(1), known), norm(tm.group(2), known)
    if not home or not away:
        log.warning("sportmail %s: не знаю команды %r / %r", mid, tm.group(1), tm.group(2))
        return None
    dm = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
    date = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}" if dm else ""
    if date != today:
        return None
    started = "Матч не начался" not in text
    finished = "Матч завершен" in text or "Матч завершён" in text
    seg = text.split("мин.", 1)[1] if "мин." in text else text
    seg = seg.split("Стадион", 1)[0]
    sc = re.search(r"(?<![\d:])(\d+) : [^\d:]+? (\d+)(?![\d:])", seg)
    score = [int(sc.group(1)), int(sc.group(2))] if sc else [0, 0]
    per = None
    if sc:
        pm = re.match(r"\s*((?:\d+:\d+(?:, )?)+)", seg[sc.end():])
        if pm:
            per = [x.strip() for x in pm.group(1).strip(", ").split(",") if x.strip()]
    st = re.search(r"назначена на (\d{1,2}:\d{2})", text)
    rec = {"id": int(mid), "source": "sportmail", "home": home, "away": away, "date": date, "score": score,
           "live": started and not finished, "started": started, "finished": finished,
           "status": "" if not started else ("Матч завершён" if finished else "")}
    if st:
        rec["time"] = st.group(1)
    if per:
        rec["periods"] = per
    ar = re.search(r"Стадион: (.+?) (?:вместимость|Главный судья|\d)", text)
    if ar:
        rec["arena"] = ar.group(1).strip()
    # ход матча: от «Источник:» до «Обозначения»
    ev_txt = text
    if "Источник:" in ev_txt:
        ev_txt = ev_txt.split("Источник:", 1)[1].split("Обозначения", 1)[0]
        ev_txt = re.sub(r"^\s*\S+(?: \S+)?\s+(?=\d{1,2}:\d{2} )", "", ev_txt) if not re.match(r"\s*\d{1,2}:\d{2}", ev_txt) else ev_txt
    events = []
    pat = re.compile(r"(\d{1,3}:\d{2}) (?:Гол! ?\((\d+) ?: ?(\d+)\) ?Шайбу забросил (.+?)(?= \d{1,3}:\d{2} (?:Гол|Удаление|Командный|Буллит)|$)"
                     r"|Удаление (.+?) ?, ?(\d+) мин|Командный штраф)")
    for m in pat.finditer(ev_txt):
        t = m.group(1)
        if m.group(2) is not None:
            who, ast = m.group(4).strip(), ""
            am = re.match(r"(.+?) \((.*)\)$", who)
            if am:
                who, ast = am.group(1), am.group(2)
            events.append({"t": t, "k": "goal", "s": [int(m.group(2)), int(m.group(3))], "p": who, "a": ast})
        elif m.group(5):
            events.append({"t": t, "k": "pen", "p": m.group(5).strip(), "m": int(m.group(6))})
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
