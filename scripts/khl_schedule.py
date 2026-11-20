"""Календарь КХЛ -> site/data/schedule.json.

Источник — JSON, который khl.ru сам запрашивает на странице календаря (data.GAMES, data.TEAMS).
Адрес и способ запроса задаются переменными окружения:

    KHL_CALENDAR_URL   — адрес запроса (можно с {season}); без него используется файл ниже
    KHL_CALENDAR_POST  — если запрос POST: тело запроса (строка, как в DevTools -> Payload)
    KHL_CALENDAR_FILE  — запасной вариант: сохранённый вручную ответ (по умолчанию data/calendar_raw.json)

В schedule.json попадают все матчи сезона: {"d": дата, "t": время МСК, "h": хозяева, "a": гости}.
Сыгранные матчи сайт берёт из протоколов, из календаря ему нужны только будущие и время.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

import protocols as P

log = logging.getLogger("khl-bot")

URL = os.getenv("KHL_CALENDAR_URL", "").strip()
POST = os.getenv("KHL_CALENDAR_POST", "").strip()
RAW = Path(os.getenv("KHL_CALENDAR_FILE", "data/calendar_raw.json"))
OUT = Path("site/data/schedule.json")
ALIASES: dict[str, str] = {"Шанхайские Драконы": "Драконы"}   # «как на сайте»: «как в протоколах», если названия расходятся


def _games_of(obj: dict) -> tuple[list[dict], dict[str, str]]:
    """Достаёт из ответа календаря плоский список матчей и словарь id команды -> название."""
    data = obj.get("data", obj)
    teams = {str(k): v.get("NAME", "") for k, v in (data.get("TEAMS") or {}).items() if isinstance(v, dict)}
    out = []
    for week in data.get("GAMES") or []:                       # список недель; неделя = {дата: [матчи] | null}
        for day in (week.values() if isinstance(week, dict) else []):
            out.extend(g for g in (day or []) if isinstance(g, dict))
    return out, teams


def parse_calendar(obj: dict, known: list[str]) -> list[dict]:
    """JSON календаря -> [{"d","t","h","a"}]. Названия приводятся к тем, что в протоколах."""
    games, teams = _games_of(obj)
    cf = {k.casefold(): k for k in known}
    cf.update({a.casefold(): c for a, c in ALIASES.items()})

    def name(tid) -> str:
        n = teams.get(str(tid), "")
        return cf.get(n.casefold(), n)

    out, seen = [], set()
    for g in games:
        d, t = g.get("date"), g.get("time_format")
        h, a = name(g.get("teama")), name(g.get("teamb"))
        if not (d and h and a) or h == a:
            continue
        try:
            dt.date.fromisoformat(d)
        except ValueError:
            continue
        key = (d, h, a)
        if key in seen:
            continue
        seen.add(key)
        out.append({"d": d, "t": (t or "").zfill(5) if t else "", "h": h, "a": a})
    out.sort(key=lambda x: (x["d"], x["t"], x["h"]))
    return out

CAL_PAGES = ["https://www.khl.ru/calendar/{season}/00/", "https://www.khl.ru/calendar/"]
CAND_LOG = Path(os.getenv("PDF_DIR", "data")) / P.SEASON_ID / "_calendar_candidates.txt"

# ───────── запасной источник: championat.com (страница календаря отдаётся готовым HTML на весь сезон) ─────────
CHAMP_URL = os.getenv("KHL_CHAMPIONAT_URL", "https://www.championat.com/hockey/_superleague/tournament/7092/calendar/").strip()

from html.parser import HTMLParser


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.out.append(data.strip())


_DT = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}:\d{2})(?:\s+\d{2}\.\d{2}\.\d{4}\s+\d{1,2}:\d{2})?")
_SC = re.compile(r"\s+(?:\d+|–|-)\s*:\s*(?:\d+|–|-)(?:\s*(?:ОТ|Б))?\s*$")


def parse_championat(text: str, known: list[str]) -> list[dict]:
    """Текст страницы календаря championat ('ДД.ММ.ГГГГ ЧЧ:ММ Хозяева – Гости счёт') -> [{"d","t","h","a"}].
    Время на сайте московское. Названия приводятся к тем, что в протоколах."""
    text = re.sub(r"\s+", " ", text.replace("\xa0", " "))
    cf = {k.casefold(): k for k in known}
    cf.update({a.casefold(): c for a, c in ALIASES.items()})
    parts = _DT.split(text)                       # [до, дд, мм, гггг, чч:мм, хвост, дд, ...]
    out, seen = [], set()
    for i in range(1, len(parts) - 4, 5):
        dd, mm, yy, tm, tail = parts[i:i + 5]
        tail = _DT.split(tail)[0]                 # хвост до следующей даты
        tail = _SC.sub("", tail.strip()).strip()
        if " – " not in tail:
            continue
        h, a = [x.strip() for x in tail.split(" – ", 1)]
        h, a = cf.get(h.casefold(), h), cf.get(a.casefold(), a)
        if not h or not a or h == a or len(h) > 40 or len(a) > 40:
            continue
        d = f"{yy}-{mm}-{dd}"
        key = (d, h, a)
        if key in seen:
            continue
        seen.add(key)
        out.append({"d": d, "t": tm.zfill(5), "h": h, "a": a})
    out.sort(key=lambda x: (x["d"], x["t"], x["h"]))
    return out


def fetch_championat(known: list[str]) -> list[dict]:
    if not CHAMP_URL:
        return []
    req = urllib.request.Request(CHAMP_URL, headers={
        "User-Agent": P._HEADERS["User-Agent"], "Accept": "text/html,*/*;q=0.8", "Accept-Language": "ru-RU,ru;q=0.9"})
    with urllib.request.urlopen(req, timeout=60) as r:
        html = r.read().decode("utf-8", "replace")
    tp = _Text()
    tp.feed(html)
    games = parse_championat(" ".join(tp.out), known)
    log.info("календарь: championat.com -> %d матчей", len(games))
    return games


def _has_games(obj) -> bool:
    try:
        return bool(_games_of(obj)[0])
    except Exception:
        return False


def discover() -> dict | None:
    """Если KHL_CALENDAR_URL не задан: ищет на странице календаря и в её скриптах адреса, похожие на
    запрос расписания, пробует их и берёт первый ответ с матчами. Найденные кандидаты пишет в
    _calendar_candidates.txt — оттуда можно взять рабочий адрес для KHL_CALENDAR_URL."""
    cands: list[str] = []
    for page in CAL_PAGES:
        base = page.format(season=P.SEASON_ID)
        try:
            html = P._open(base, timeout=40).decode("utf-8", "replace")
        except Exception as e:
            log.warning("календарь: страница %s не открылась: %r", base, e)
            continue
        blobs = [html]
        for src in re.findall(r"<script[^>]+src=[\"']([^\"']+)[\"']", html)[:20]:
            u = urllib.parse.urljoin(base, src)
            if "khl.ru" not in u or "yandex" in u:
                continue
            try:
                blobs.append(P._open(u, timeout=40).decode("utf-8", "replace"))
            except Exception:
                pass
        for txt in blobs:
            for m in re.finditer(r"[\"'`](/[^\"'`\s<>]*(?:calendar|schedule|games)[^\"'`\s<>]*)[\"'`]", txt, re.I):
                u = urllib.parse.urljoin(base, m.group(1))
                if u not in cands and not re.search(r"\.(?:css|png|jpe?g|svg|ico|woff2?)(?:\?|$)", u):
                    cands.append(u)
    try:
        CAND_LOG.parent.mkdir(parents=True, exist_ok=True)
        CAND_LOG.write_text("\n".join(cands), encoding="utf-8")
    except Exception:
        pass
    for u in cands[:25]:
        for url in dict.fromkeys([u.replace("{season}", P.SEASON_ID), u]):
            try:
                obj = json.loads(P._open(url, timeout=40).decode("utf-8", "replace"))
            except Exception:
                continue
            if _has_games(obj):
                log.info("календарь: найден рабочий адрес %s — впиши его в KHL_CALENDAR_URL", url)
                return obj
    return None


def fetch() -> dict | None:
    """Скачивает календарь; если адреса нет или он не отвечает — берёт сохранённый файл."""
    if URL:
        try:
            try:
                P._open("https://www.khl.ru/")                  # прогрев: сайт выдаёт cookie
            except Exception:
                pass
            url = URL.format(season=P.SEASON_ID)
            h = dict(P._HEADERS)
            h.update({"Accept": "application/json, text/plain, */*", "X-Requested-With": "XMLHttpRequest"})
            if os.getenv("KHL_COOKIE"):
                h["Cookie"] = os.environ["KHL_COOKIE"]
            body = None
            if POST:
                body = POST.encode("utf-8")
                h["Content-Type"] = "application/json" if POST.lstrip().startswith("{") \
                    else "application/x-www-form-urlencoded; charset=UTF-8"
            req = urllib.request.Request(url, data=body, headers=h)
            with P._opener.open(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            log.warning("календарь: запрос %s не удался: %r", URL, e)
    if RAW.exists():
        try:
            return json.loads(RAW.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("календарь: %s не прочитан: %r", RAW, e)
    try:
        return discover()                               # адрес не задан: пробуем найти его самим
    except Exception as e:
        log.warning("календарь: автопоиск не удался: %r", e)
    return None


def update(teams: list[str], season: str | None = None) -> int:
    """Обновляет site/data/schedule.json. Возвращает число матчей в календаре.
    Если разобрать не удалось, прежний файл остаётся как был."""
    games = []
    try:
        games = fetch_championat(teams)
    except Exception as e:
        log.warning("календарь: championat.com не отдал расписание: %r", e)
    if len(games) < 50:                                 # слишком мало — похоже на сбой разбора, пробуем khl.ru
        obj = fetch()
        games = parse_calendar(obj, teams) if obj else games
    if not games:
        log.warning("календарь: матчи не найдены (нет KHL_CALENDAR_URL или %s), оставляю прежний файл", RAW)
        return len(P._load_json(OUT, {}).get("games", []))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
                               "games": games}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return len(games)
