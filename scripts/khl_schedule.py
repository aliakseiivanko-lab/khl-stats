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
import urllib.request
from pathlib import Path

import protocols as P

log = logging.getLogger("khl-bot")

URL = os.getenv("KHL_CALENDAR_URL", "").strip()
POST = os.getenv("KHL_CALENDAR_POST", "").strip()
RAW = Path(os.getenv("KHL_CALENDAR_FILE", "data/calendar_raw.json"))
OUT = Path("site/data/schedule.json")
ALIASES: dict[str, str] = {}      # «как на khl.ru»: «как в протоколах», если названия команд расходятся


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
    return None


def update(teams: list[str], season: str | None = None) -> int:
    """Обновляет site/data/schedule.json. Возвращает число матчей в календаре.
    Если разобрать не удалось, прежний файл остаётся как был."""
    obj = fetch()
    games = parse_calendar(obj, teams) if obj else []
    if not games:
        log.warning("календарь: матчи не найдены (нет KHL_CALENDAR_URL или %s), оставляю прежний файл", RAW)
        return len(P._load_json(OUT, {}).get("games", []))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
                               "games": games}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return len(games)
