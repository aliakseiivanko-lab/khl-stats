"""Календарь КХЛ: скачивает страницу(ы) календаря khl.ru и пишет site/data/schedule.json
(дата, время МСК и пара команд для будущих матчей).

Парсер не привязан к вёрстке сайта: страница превращается в текст, а в нём ищутся даты, время
и названия команд (названия берутся из уже разобранных протоколов, поэтому совпадают с ними).

Настройка через переменные окружения:
    KHL_CALENDAR_URLS  — адреса страниц календаря через запятую ({season} подставится id сезона)
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import os
import re
from pathlib import Path

import protocols as P

log = logging.getLogger("khl-bot")

URLS = [u.strip() for u in os.getenv("KHL_CALENDAR_URLS", "https://www.khl.ru/calendar/{season}/00/").split(",")
        if u.strip()]
OUT = Path("site/data/schedule.json")
ALIASES: dict[str, str] = {}      # «как называется на странице календаря»: «как в протоколах», если названия расходятся


def page_text(raw: str) -> str:
    s = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", raw)
    s = re.sub(r"(?s)<!--.*?-->", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s)


def _date(m: re.Match, start: int) -> str | None:
    k = m.lastgroup
    try:
        if k == "d1":
            d, mo, y = (int(x) for x in m.group().split("."))
        elif k == "d2":
            y, mo, d = (int(x) for x in m.group().split("-"))
        else:
            parts = m.group().split()
            d, mo = int(parts[0]), P._MONTHS[parts[1].lower()]
            y = int(parts[2]) if len(parts) > 2 else (start if mo >= 7 else start + 1)
        return dt.date(y, mo, d).isoformat()
    except (ValueError, KeyError):
        return None


def parse(text: str, teams: list[str], start: int) -> dict[tuple[str, str, str], str]:
    """Текст страницы -> {(дата, хозяева, гости): время}. Матчи без времени не берём (чтобы не ловить списки команд)."""
    names = {t.casefold(): t for t in teams}
    names.update({a.casefold(): c for a, c in ALIASES.items() if c in teams})
    if not names:
        return {}
    team_re = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    mon_re = "|".join(P._MONTHS)
    rx = re.compile(
        rf"(?P<d1>\b\d{{1,2}}\.\d{{1,2}}\.\d{{4}}\b)|(?P<d2>\b\d{{4}}-\d{{2}}-\d{{2}}\b)"
        rf"|(?P<d3>\b\d{{1,2}}\s+(?:{mon_re})(?:\s+\d{{4}})?)"
        rf"|(?P<t>\b(?:[01]?\d|2[0-3]):[0-5]\d\b)|(?P<n>(?<!\w)(?:{team_re})(?!\w))", re.I)
    lo, hi = f"{start}-08-01", f"{start + 1}-07-01"
    out: dict[tuple[str, str, str], str] = {}
    cur, tm, pair = None, None, []

    def emit():
        nonlocal tm, pair
        if cur and tm and len(pair) == 2 and pair[0] != pair[1] and lo <= cur <= hi:
            out[(cur, pair[0], pair[1])] = tm.zfill(5)
        tm, pair = None, []

    for m in rx.finditer(text):
        k = m.lastgroup
        if k in ("d1", "d2", "d3"):
            cur, tm, pair = _date(m, start), None, []
        elif k == "t":
            if len(pair) == 2 and tm is None:       # время после пары команд
                tm = m.group()
                emit()
            else:                                   # время перед парой команд
                tm, pair = m.group(), []
        else:
            if len(pair) == 2:
                pair = []
            pair.append(names[m.group().casefold()])
            if len(pair) == 2 and tm:
                emit()
    return out


def fetch_texts() -> list[str]:
    try:
        P._open("https://www.khl.ru/")              # прогрев: сайт выдаёт cookie
    except Exception:
        pass
    texts = []
    for u in URLS:
        url = u.format(season=P.SEASON_ID)
        try:
            texts.append(page_text(P._open(url, timeout=40).decode("utf-8", "replace")))
        except Exception as e:
            log.warning("календарь: %s не скачался: %r", url, e)
    return texts


def update(teams: list[str], season: str | None) -> int:
    """Обновляет site/data/schedule.json. Возвращает число игр в расписании.
    Если страница не разобралась, старый файл остаётся как был."""
    start = int(season[:4]) if season else dt.date.today().year - (0 if dt.date.today().month >= 7 else 1)
    texts = fetch_texts()
    found: dict[tuple[str, str, str], str] = {}
    for t in texts:
        found.update(parse(t, teams, start))
    old = P._load_json(OUT, {}).get("games", [])
    if not found:
        dbg = P.PDF_DIR / "_debug_calendar.txt"       # чтобы можно было подогнать парсер под вёрстку
        if texts:
            P.PDF_DIR.mkdir(parents=True, exist_ok=True)
            dbg.write_text(texts[0][:6000], encoding="utf-8")
        log.warning("календарь: матчи не найдены (страниц скачано: %d), оставляю прежний файл", len(texts))
        return len(old)
    lo, hi = min(k[0] for k in found), max(k[0] for k in found)
    keep = [g for g in old if g["d"] < lo or g["d"] > hi]      # что вне диапазона новой выгрузки — не трогаем
    games = keep + [{"d": d, "t": t, "h": h, "a": a} for (d, h, a), t in found.items()]
    games.sort(key=lambda g: (g["d"], g["t"], g["h"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
                               "games": games}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return len(games)
