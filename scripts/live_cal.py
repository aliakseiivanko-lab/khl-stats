"""Источник №3: страница календаря khl.ru (на ней у идущих матчей блок «Сейчас» с периодом и счётом).
Даёт счёт и период, но не голы — они берутся из других источников."""
import http.cookiejar, logging, os, re, urllib.error, urllib.request, zlib

import live_khl as K
import live_sm as SM

log = logging.getLogger("live")
URL = os.getenv("KHL_CAL_URL", "https://www.khl.ru/calendar/")
UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Mobile Safari/537.36")
PERIOD = re.compile(r"(\d)-й (период|перерыв)|Овертайм|Буллиты|Перерыв", re.I)


def fetch() -> str:
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    hdr = {"User-Agent": UA, "Accept": "text/html,*/*;q=0.8", "Accept-Language": "ru-RU,ru;q=0.9"}
    if os.getenv("KHL_COOKIE"):
        hdr["Cookie"] = os.getenv("KHL_COOKIE")
    with op.open(urllib.request.Request(URL, headers=hdr), timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def parse(html: str, known: list[str], today: str) -> list[dict]:
    L = K.lines_of(html)
    idx = [i for i, l in enumerate(L) if re.fullmatch(r"Сейчас", l, re.I)]
    log.info("khl.ru/calendar: %d строк текста, блоков «Сейчас»: %d", len(L), len(idx))
    out = []
    for n, i in enumerate(idx):
        end = idx[n + 1] if n + 1 < len(idx) else min(len(L), i + 25)
        chunk = L[i + 1:min(end, i + 25)]
        teams, score, period = [], None, ""
        for l in chunk:
            t = SM.norm(l, known)
            if t and t not in teams and len(l) < 25:
                teams.append(t)
            m = re.fullmatch(r"(\d+)\s*[–—-]\s*(\d+)", l)
            if m and score is None:
                score = [int(m.group(1)), int(m.group(2))]
            m = PERIOD.search(l)
            if m and not period and len(l) < 25:
                period = l.strip()
            if re.fullmatch(r"\d{1,2}:\d{2}", l) and teams:      # начался блок следующего матча (время начала)
                break
        if len(teams) < 2 or score is None:
            log.info("khl.ru/calendar: блок «Сейчас» не разобран: %r", chunk[:10])
            continue
        h, a = teams[0], teams[1]
        out.append({"id": 9000000 + zlib.crc32(f"{h}|{a}".encode()) % 1000000, "source": "calendar", "home": h, "away": a,
                    "date": today, "score": score, "live": True, "started": True, "finished": False,
                    "status": ("Перерыв" if "перерыв" in period.lower() else period) if period else "Идёт матч"})
        log.info("khl.ru/calendar: %s — %s %d:%d, %s", h, a, score[0], score[1], period)
    return out
