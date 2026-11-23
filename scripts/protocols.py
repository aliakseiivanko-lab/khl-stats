"""Протоколы КХЛ в три этапа:

  1. download — скачать все PDF в папку protocols/<сезон>/game-<id>-ru.pdf
  2. parse    — разобрать СКАЧАННЫЕ файлы (интернет не нужен) в protocols/<сезон>/games.json
  3. stats    — games.json -> список Match -> обычная статистика бота

Командная строка:
    python protocols.py sync             # скачать новые + разобрать
    python protocols.py download         # только скачать
    python protocols.py parse            # только разобрать (--force: заново всё)
    python protocols.py inspect 901978   # что разобралось из одного протокола (--text: ещё и текст PDF)
    python protocols.py report Локомотив # статистика команды в консоли
    python protocols.py csv              # выгрузить matches.csv
    python protocols.py check Минск      # найти потерянные игры (дыры в id, неразобранные, пропущенные)
    python protocols.py download --retry # заново пробовать id, на которые раньше был 404

PDF можно положить в папку и вручную (любое имя, в котором есть id игры из 6+ цифр).
"""
from __future__ import annotations

import csv
import http.cookiejar
import io
import json
import logging
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from stats import Match

log = logging.getLogger("khl-bot")

SEASON_ID = os.getenv("KHL_SEASON_ID", "1436")
FIRST_ID = int(os.getenv("KHL_FIRST_ID", "901978"))      # id первой игры сезона
LOOKAHEAD = int(os.getenv("KHL_LOOKAHEAD", "40"))         # сколько id подряд пробуем после последнего найденного
LAST_ID = int(os.getenv("KHL_LAST_ID", "904000"))         # пробуем id минимум до этого значения (с запасом)
MISS_RETRY_H = int(os.getenv("KHL_MISS_RETRY_H", "48"))   # через сколько часов повторять «дыры» (404)
PDF_DIR = Path(os.getenv("PDF_DIR", "protocols")) / SEASON_ID
GAMES_PATH = PDF_DIR / "games.json"
MISS_PATH = PDF_DIR / "_misses.json"
PDF_URL = "https://www.khl.ru/pdf/{season}/{gid}/game-{gid}-ru.pdf"
PARSER_VERSION = 4


# ═════════════════════════ 1. СКАЧИВАНИЕ ═════════════════════════

class NotFound(Exception):
    pass


class Blocked(Exception):
    pass


_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/pdf,application/xhtml+xml,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://www.khl.ru/",
}
_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))
_warmed = False


def _open(url: str, timeout: int = 30) -> bytes:
    h = dict(_HEADERS)
    if os.getenv("KHL_COOKIE"):                 # cookie из браузера, если сайт не пускает бота
        h["Cookie"] = os.environ["KHL_COOKIE"]
    with _opener.open(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return r.read()


def fetch_pdf(gid: int) -> bytes:
    """Скачивает один протокол. 404 -> NotFound. Любая другая беда -> исключение."""
    global _warmed
    if not _warmed:                             # сайт отдаёт 307, пока не выдал cookie
        _warmed = True
        try:
            _open("https://www.khl.ru/")
        except Exception:
            pass
    url = PDF_URL.format(season=SEASON_ID, gid=gid)
    last: Exception | None = None
    for attempt in range(2):
        u = url if attempt == 0 else f"{url}?{random.getrandbits(128):032x}"   # как в ссылке: хэш в конце
        try:
            data = _open(u)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(gid)
            last = e
        except Exception as e:
            last = e
        else:
            if data[:5] == b"%PDF-":
                return data
            last = ValueError(f"ответ не PDF: {data[:60]!r}")
        time.sleep(1.0)
    raise last  # type: ignore[misc]


def _id_of(p: Path) -> int | None:
    m = re.search(r"(\d{6,})", p.name)
    return int(m.group(1)) if m else None


def have_ids() -> set[int]:
    return {i for p in PDF_DIR.glob("*.pdf") if (i := _id_of(p)) is not None}


def _atomic_write(path: Path, data: bytes):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, obj):
    _atomic_write(path, json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def download_all(on_batch=None, workers: int = 4, delay: float = 0.2) -> int:
    """Качает PDF по id подряд от FIRST_ID, пока находятся новые. Уже скачанные пропускает.
    Возвращает, сколько файлов скачано. on_batch() вызывается после каждой пачки."""
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    have = have_ids()
    misses: dict = _load_json(MISS_PATH, {})            # {"id": когда получили 404}
    total = 0
    while True:
        now = time.time()
        last = max(have, default=FIRST_ID - 1)
        todo = [g for g in range(FIRST_ID, max(last + LOOKAHEAD, LAST_ID) + 1)
                if g not in have and now - misses.get(str(g), 0) > MISS_RETRY_H * 3600]
        if not todo:
            break
        log.info("khl.ru: пробую %d id (%d–%d), уже есть %d", len(todo), todo[0], todo[-1], len(have))

        def job(gid):
            try:
                return gid, fetch_pdf(gid), None
            except NotFound:
                return gid, None, "404"
            except Exception as e:
                return gid, None, e
            finally:
                time.sleep(delay)

        found, errors, new_miss = 0, [], []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for gid, data, err in ex.map(job, todo):
                if data:
                    _atomic_write(PDF_DIR / f"game-{gid}-ru.pdf", data)
                    have.add(gid)
                    found += 1
                elif err == "404":
                    new_miss.append(gid)
                else:
                    errors.append((gid, err))
        if not found and not have and len(errors) >= 10:
            gid, err = errors[0]
            raise Blocked(f"khl.ru не отдаёт PDF (id {gid}: {err!r}). Запусти diag_khl.py, задай KHL_COOKIE "
                          f"(cookie из браузера) или положи PDF в {PDF_DIR} вручную.")
        if errors:
            log.warning("khl.ru: ошибок %d, напр. id %s: %r (повторю при следующем обновлении)",
                        len(errors), errors[0][0], errors[0][1])
        top = max(have, default=0)
        for g in new_miss:
            if g < top:                                  # дыры внутри диапазона; хвост не запоминаем
                misses[str(g)] = now
        _save_json(MISS_PATH, misses)
        total += found
        log.info("khl.ru: скачано в этой пачке %d", found)
        if found and on_batch:
            on_batch()
        if not found:
            break
    return total


def refresh_stale(recent_days: int = 3, workers: int = 4) -> list[int]:
    """Перекачивает протоколы, в которых счёт мог остаться промежуточным.

    Это (1) записи, разобранные старой версией парсера (без проверки «Окончание матча»), и
    (2) игры за последние `recent_days` дней — на случай, если PDF изменился после первой загрузки.
    Если PDF не поменялся — запись остаётся. Если поменялся (или матч ещё не окончен) — запись
    удаляется и разбирается заново. Возвращает id изменившихся игр."""
    import datetime as dt
    games = load_games()
    limit = (dt.date.today() - dt.timedelta(days=recent_days)).isoformat()
    todo = [int(k) for k, r in games.items()
            if not r.get("bad") and (r.get("v") != PARSER_VERSION or r.get("date", "") >= limit)]
    if not todo:
        return []
    log.info("khl.ru: перепроверяю %d протоколов (старый парсер или последние дни)", len(todo))

    def job(gid):
        try:
            return gid, fetch_pdf(gid)
        except Exception as e:                      # сеть/404: оставляем как есть, проверим в следующий раз
            log.warning("перепроверка %s не удалась: %r", gid, e)
            return gid, None
        finally:
            time.sleep(0.2)

    changed = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for gid, data in ex.map(job, todo):
            if not data:
                continue
            f = PDF_DIR / f"game-{gid}-ru.pdf"
            rec = games[str(gid)]
            if rec.get("v") == PARSER_VERSION and f.exists() and f.read_bytes() == data:
                continue                            # без изменений
            _atomic_write(f, data)
            games.pop(str(gid), None)               # parse_new разберёт заново (или удалит, если не окончен)
            changed.append(gid)
    if changed:
        _save_json(GAMES_PATH, games)
        log.info("khl.ru: обновлено протоколов: %d", len(changed))
    return changed


# ═════════════════════════ 2. РАЗБОР PDF ═════════════════════════

_MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
           "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}


def pdf_to_text(data: bytes, layout: bool = False) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("не установлен pypdf: pip install pypdf")
    pages = PdfReader(io.BytesIO(data)).pages
    if layout:
        txt = "\n".join((pg.extract_text(extraction_mode="layout") or "") for pg in pages)
    else:
        txt = "\n".join((pg.extract_text() or "") for pg in pages)
    txt = txt.replace("\xa0", " ")
    return "\n".join(re.sub(r"[ \t]+", " ", ln).strip() for ln in txt.splitlines())


def _two(m, a=1, b=3):
    return [int(m.group(a)), int(m.group(b))]


def _parse_text_core(text: str, partial: bool = False):
    """Текст протокола -> (запись | None, причина). Пары везде (хозяева, гости).
    partial=True — режим «лайв»: разбираем и незаконченный протокол (rec[\"live\"]=True)."""
    t = text.replace("\xa0", " ")
    fin = re.search(r"Окончание матча:\s*(\d{1,2}:\d{2})", t)

    md = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})\s*г", t, re.I)
    if not md or md.group(2).lower() not in _MONTHS:
        return None, "не найдена дата"
    mh_ = re.search(r"Игра\s+\d+\s+Домашняя\s+игра\s+\d+", t)
    ma_ = re.search(r"Игра\s+\d+\s+Гостевая\s+игра", t)
    if not (mh_ and ma_ and mh_.start() < ma_.start()):
        return None, "не найдены метки «Домашняя игра»/«Гостевая игра»"
    # гости — между «Домашняя игра N» и «Игра N Гостевая игра»
    away = t[mh_.end():ma_.start()].strip()
    # хозяева — последний фрагмент перед «Игра N Домашняя игра» (после счёта/статуса)
    pre = t[:mh_.start()].rstrip()
    home = pre.split("\n")[-1] if "\n" in pre else pre
    home = re.split(r"\d+\s+", home)[-1].strip()
    if not home or not away or len(home) > 40 or len(away) > 40:
        return None, f"не удалось выделить команды (хозяева={home!r}, гости={away!r})"
    mh = type("M", (), {"group": lambda self, i, v=home: v})()
    ma = type("M", (), {"group": lambda self, i, v=away: v})()

    # Пока матч идёт, khl.ru уже отдаёт PDF с промежуточным счётом и блоком БРОСКИ.
    # Протокол считается готовым только после строки «Окончание матча: ЧЧ:ММ».
    if not fin and not partial:
        return None, "not_finished"
    msog = re.search(r"БРОСКИ.*?ВСЕГО\s+ВСЕГО\s+(\d+)\s*-\s*(\d+)\s+(\d+)\s*-\s*(\d+)", t, re.S)
    if not msog and not partial:
        return None, "нет блока БРОСКИ"
    reg = [int(msog.group(1)), int(msog.group(3))] if msog else [0, 0]       # голы игры (без победного буллита?)
    sog = [int(msog.group(2)), int(msog.group(4))] if msog else None         # броски в створ

    # итоговый счёт и статус («Основное время» / овертайм / буллиты) — из шапки
    mtop = re.search(r"ХОЗЯЕВА.*?ГОСТИ\s+(\d+)\s", t, re.S)
    mst = re.search(r"Игра\s*№\s*\d+\s+(\D*?)(\d+)\s", t)
    status = mst.group(1).strip() if mst else ""
    score = [int(mtop.group(1)), int(mst.group(2))] if (mtop and mst) else reg[:]

    goals = re.findall(r"^[ \t]*\d+[ \t]+\d+[ \t]*[–-][ \t]*\d+[ \t]+(\d{1,3}):(\d{2})[ \t]+(\S+)", t, re.M)
    so = bool(re.search(r"буллит", status, re.I)) or any(s.lower().startswith("бул") for _, _, s in goals)
    ot = (so or bool(re.search(r"овертайм|дополнит|продлен|\bОТ\b", status, re.I))
          or any(int(mm) >= 60 for mm, _, _ in goals))

    d = f"{md.group(3)}-{_MONTHS[md.group(2).lower()]:02d}-{int(md.group(1)):02d}"
    rec = {"date": d, "home": mh.group(1).strip(), "away": ma.group(1).strip(),
           "score": score, "reg_goals": reg, "status": status, "ot": ot, "so": so,
           "sog": sog, "fin": fin.group(1) if fin else None, "live": not fin}
    if so and score[0] == score[1]:
        rec["warn"] = "буллиты, но счёт ничейный: неизвестно, кто выиграл"

    m = re.search(r"Начало матча:\s*(\d{1,2}:\d{2})", t)
    if m:
        rec["time"] = m.group(1)
    m = re.search(r"(\d[\d ]*?)\s+зрител\w*,\s*([^\n]+)", t)
    if m:
        rec["att"], rec["arena"] = int(m.group(1).replace(" ", "")), m.group(2).strip()

    m = re.search(r"ВБРАСЫВАНИЯ.*?ВСЕГО\s+ВСЕГО\s+(\d+)\s*-\s*(\d+)\s*/\s*\d+\s*%\s+(\d+)\s*-\s*(\d+)\s*/\s*\d+\s*%",
                  t, re.S)
    if m:
        rec["fo"] = _two(m, 1, 3)                        # выиграно
        rec["fo_all"] = _two(m, 2, 4)                    # всего

    m = re.search(r"Итого\s*\(У-Штр\)\s*(\d+)\s*/\s*(\d+)\s+Итого\s*\(У-Штр\)\s*(\d+)\s*/\s*(\d+)", t)
    if m:
        rec["pen"] = _two(m, 1, 3)                       # число удалений
        rec["pim"] = _two(m, 2, 4)                       # штрафные минуты

    m = re.search(r"Большинство\s*\(Голы-Попытки/Время\)\s*(\d+)\s*-\s*(\d+)\s*/\s*[\d:]+\s+"
                  r"Большинство\s*\(Голы-Попытки/Время\)\s*(\d+)\s*-\s*(\d+)\s*/", t)
    if m:
        rec["pp_goals"] = _two(m, 1, 3)
        rec["pp_opps"] = _two(m, 2, 4)

    m = re.search(r"МЕНЬШИНСТВО\s*\(Голы\).*?((?:\d+\s+){7}\d+)", t, re.S)
    if m:
        n = [int(x) for x in m.group(1).split()]
        rec["shg"] = [n[3], n[7]]                        # голы, забитые в меньшинстве

    m = re.search(r"КОМАНДНЫЕ ПОКАЗАТЕЛИ.*?(\d+)\s+(\d+)\s+\d+:\d+\s+(\d+)\s+(\d+)\s+(\d+)\s+"
                  r"(\d+)\s+(\d+)\s+\d+:\d+\s+(\d+)\s+(\d+)\s+(\d+)", t, re.S)
    if m:
        g = [int(x) for x in m.groups()]
        rec["blk"] = [g[0], g[5]]                        # БлБ — блокированные броски
        rec["hit"] = [g[1], g[6]]                        # СПр — силовые
        rec["extra"] = {"ОТБ": [g[2], g[7]], "ПХТ": [g[3], g[8]], "ПТР": [g[4], g[9]]}
    return rec, ""


def load_games() -> dict:
    return _load_json(GAMES_PATH, {})


_NAME = r"[А-ЯЁA-Z][^\d,]*?"


def _parse_events(text: str, rec: dict):
    """Таблицы «ГОЛЫ» и «УДАЛЕНИЯ» протокола -> rec['gl'] (голы с ассистентами) и rec['pl'] (штрафы)."""
    home, away = rec["home"], rec["away"]
    gl, pl = [], []
    m = re.search(r"\nГОЛЫ\n(.*?)(?=\nУДАЛЕНИЯ|\nВРАТАР|\Z)", text, re.S)
    if m:
        for ln in m.group(1).split("\n"):
            g = re.match(r"^(\d+) (\d+) [–-] (\d+) (\d{1,3}:\d{2}) (\S+(?: \([^)]*\))?) (.+)$", ln.strip())
            if not g:
                continue
            rest = g.group(6)
            side = 0 if rest.startswith(home) else 1 if rest.startswith(away) else None
            if side is None:
                continue
            rest = rest[len(home if side == 0 else away):].strip()
            toks = rest.split()
            while toks and re.fullmatch(r"\d+(,\d+)+", toks[-1]):      # списки игроков на льду
                toks.pop()
            parts = re.split(r"(?:(?<=\s)|^)(?=\d{1,2}\s+[А-ЯЁA-Z])", " ".join(toks))
            names = []
            for part in parts:
                nm = re.match(r"^\d{1,2}\s+(.+)$", part.strip())
                if nm:
                    names.append(nm.group(1).strip())
            if not names:
                continue
            gl.append({"t": g.group(4), "s": [int(g.group(2)), int(g.group(3))], "k": g.group(5), "h": side,
                       "p": names[0], "a": names[1:3]})
    m = re.search(r"\nУДАЛЕНИЯ\n(.*?)(?=\nВРАТАР|\nВСЕГО|\nОбновлено|\Z)", text, re.S)
    if m:
        side = -1
        for ln in m.group(1).split("\n"):
            ln = ln.strip()
            if ln.startswith("# Период"):
                side += 1
                continue
            r = re.match(r"^(\d+) (\d) (\d{1,2}:\d{2}) (?:(\d{1,2}) )?(.+?) (\d{1,2}) (.+)$", ln)
            if r and side in (0, 1):
                pl.append({"t": r.group(3), "pe": int(r.group(2)), "h": side, "p": r.group(5).strip(),
                           "m": int(r.group(6)), "r": r.group(7).strip()})
    rec["gl"], rec["pl"] = gl, pl


def parse_text(text: str, partial: bool = False):
    rec, why = _parse_text_core(text, partial)
    if rec:
        try:
            _parse_events(text, rec)
        except Exception as e:                       # события — приятное дополнение, не должны ломать разбор
            log.warning("события не разобраны: %r", e)
    return rec, why


def parse_new(force: bool = False) -> dict:
    """Разбирает PDF из папки, которых ещё нет в games.json (или разобраны старой версией парсера)."""
    games = load_games()
    changed = False
    for p in sorted(PDF_DIR.glob("*.pdf")):
        gid = _id_of(p)
        if gid is None:
            continue
        old = games.get(str(gid))
        if old and old.get("v") == PARSER_VERSION and not force:
            continue
        rec, why = None, ""
        try:
            data = p.read_bytes()
            for layout in (False, True):
                txt = pdf_to_text(data, layout)
                rec, why = parse_text(txt)
                if rec or why == "not_finished":
                    break
            if not rec and why != "not_finished":
                dbg = PDF_DIR / "_debug"
                if len(list(dbg.glob("*.txt"))) < 6:     # сохраняем текст, чтобы можно было починить парсер
                    dbg.mkdir(exist_ok=True)
                    (dbg / f"{gid}_plain.txt").write_text(pdf_to_text(data, False), encoding="utf-8")
                    (dbg / f"{gid}_layout.txt").write_text(pdf_to_text(data, True), encoding="utf-8")
        except RuntimeError:
            raise
        except Exception as e:
            why = f"ошибка чтения: {e}"
        if rec:
            rec.update(id=gid, v=PARSER_VERSION)
            games[str(gid)] = rec
        elif why == "not_finished":                      # игра ещё не сыграна: файл удаляем, попробуем позже
            p.unlink(missing_ok=True)
            games.pop(str(gid), None)
        else:
            games[str(gid)] = {"id": gid, "v": PARSER_VERSION, "bad": why}
            log.warning("протокол %s не разобран: %s", p.name, why)
        changed = True
    if changed:
        PDF_DIR.mkdir(parents=True, exist_ok=True)
        _save_json(GAMES_PATH, games)
    return games


# ═════════════════════════ 3. СТАТИСТИКА ═════════════════════════

def _pair(r, k):
    v = r.get(k)
    return (v[0], v[1]) if v else (None, None)


def to_match(r: dict) -> Match | None:
    if r.get("bad") or r.get("warn"):
        return None
    y, mo = int(r["date"][:4]), int(r["date"][5:7])
    start = y if mo >= 7 else y - 1
    hs, as_ = _pair(r, "sog")
    hpg, apg = _pair(r, "pp_goals")
    hpo, apo = _pair(r, "pp_opps")
    hpim, apim = _pair(r, "pim")
    hsh, ash = _pair(r, "shg")
    hfo, afo = _pair(r, "fo")
    hh, ah = _pair(r, "hit")
    hb, ab = _pair(r, "blk")
    return Match(
        date=r["date"], season=f"{start}/{(start + 1) % 100:02d}",
        home=r["home"], away=r["away"],
        home_goals=r["score"][0], away_goals=r["score"][1], ot=r["ot"], so=r["so"],
        home_shots=hs, away_shots=as_,
        home_pp_goals=hpg, away_pp_goals=apg, home_pp_opps=hpo, away_pp_opps=apo,
        home_pim=hpim, away_pim=apim, home_shg=hsh, away_shg=ash,
        home_fo=hfo, away_fo=afo, home_hits=hh, away_hits=ah, home_blocks=hb, away_blocks=ab, time=r.get("time") or "")


def build_matches(games: dict | None = None) -> list[Match]:
    games = load_games() if games is None else games
    out, skipped = [], 0
    for r in games.values():
        m = to_match(r)
        if m:
            out.append(m)
        elif r.get("warn"):
            skipped += 1
            log.warning("игра %s %s–%s пропущена: %s", r.get("id"), r.get("home"), r.get("away"), r["warn"])
    out.sort(key=lambda m: m.date)
    return out


class PdfProvider:
    """Источник для бота: PDF-протоколы с khl.ru -> локальная папка -> games.json -> Match."""

    def fetch_matches(self, details: bool = True, progress=None) -> list[Match]:
        def push():
            if progress:
                progress(build_matches(parse_new()))
        if details:
            try:
                download_all(on_batch=push)
            except Blocked as e:
                log.error("%s", e)
        games = parse_new()
        out = build_matches(games)
        if not out:
            log.warning("Нет разобранных протоколов (папка %s). Запусти: python protocols.py sync", PDF_DIR)
        return out


# ═════════════════════════ CLI ═════════════════════════

def _plain(s: str) -> str:
    import html
    return html.unescape(re.sub(r"<[^>]+>", "", s))


def _main(argv: list[str]):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cmd = argv[0] if argv else "sync"
    if cmd in ("download", "sync"):
        if "--retry" in argv:                            # забыть запомненные 404 и пробовать все id заново
            MISS_PATH.unlink(missing_ok=True)
        n = download_all()
        print(f"Скачано новых: {n}. Всего PDF в {PDF_DIR}: {len(have_ids())}")
    if cmd in ("parse", "sync"):
        g = parse_new(force="--force" in argv)
        bad = [k for k, v in g.items() if v.get("bad") or v.get("warn")]
        print(f"Разобрано: {len(g) - len(bad)} из {len(g)}; проблемных: {len(bad)} {bad[:10] or ''}")
    elif cmd == "inspect":
        gid = int(argv[1])
        p = next((p for p in PDF_DIR.glob("*.pdf") if _id_of(p) == gid), None)
        if p is None:
            PDF_DIR.mkdir(parents=True, exist_ok=True)
            p = PDF_DIR / f"game-{gid}-ru.pdf"
            p.write_bytes(fetch_pdf(gid))
        text = pdf_to_text(p.read_bytes())
        if "--text" in argv:
            print(text, "\n" + "=" * 60)
        rec, why = parse_text(text)
        print(json.dumps(rec, ensure_ascii=False, indent=2) if rec else f"не разобран: {why}")
    elif cmd == "check":
        games = load_games()
        ids = have_ids()
        misses = _load_json(MISS_PATH, {})
        top = max(ids, default=FIRST_ID)
        print(f"PDF на диске: {len(ids)}, разобрано в games.json: {sum(1 for g in games.values() if not g.get('bad'))}")
        gaps = [g for g in range(FIRST_ID, top + 1) if g not in ids]
        print(f"Нет PDF внутри диапазона {FIRST_ID}–{top}: {gaps or 'нет'}")
        for g in gaps:
            if str(g) in misses:
                print(f"  {g}: ответ 404, запомнено {time.strftime('%d.%m %H:%M', time.localtime(misses[str(g)]))}")
        for k, v in sorted(games.items()):
            if v.get("bad"):
                print(f"  {k}: НЕ РАЗОБРАН — {v['bad']}")
            elif v.get("warn"):
                print(f"  {k}: {v['home']}–{v['away']} {v['date']} — ПРОПУЩЕН: {v['warn']} (status={v['status']!r}, score={v['score']})")
        q = " ".join(argv[1:]).casefold()
        if q:
            rows = sorted((v for v in games.values() if not v.get("bad") and
                           q in (v["home"] + " " + v["away"]).casefold()), key=lambda v: (v["date"], v["id"]))
            print(f"\nИгры с «{q}» в games.json: {len(rows)}")
            for v in rows:
                flag = " (Б)" if v["so"] else " (ОТ)" if v["ot"] else ""
                print(f"  {v['id']}  {v['date']}  {v['home']} {v['score'][0]}:{v['score'][1]} {v['away']}{flag}"
                      + (f"   !! {v['warn']}" if v.get("warn") else ""))
    elif cmd == "report":
        from stats import render_report, render_last
        ms = build_matches()
        q = " ".join(argv[1:]).casefold()
        teams = sorted({t for m in ms for t in (m.home, m.away)})
        found = [t for t in teams if q in t.casefold()]
        if not found:
            print("Команды:", ", ".join(teams))
            return
        print(_plain(render_report(ms, found[0])))
        print("\n" + _plain(render_last(ms, found[0])))
    elif cmd == "csv":
        cols = ("date,season,home,away,home_goals,away_goals,ot,so,home_shots,away_shots,home_pp_goals,"
                "home_pp_opps,away_pp_goals,away_pp_opps,home_pim,away_pim,home_shg,away_shg,home_fo,away_fo,"
                "home_hits,away_hits,home_blocks,away_blocks").split(",")
        with open("matches.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for m in build_matches():
                w.writerow([int(getattr(m, c)) if isinstance(getattr(m, c), bool)
                            else ("" if getattr(m, c) is None else getattr(m, c)) for c in cols])
        print("matches.csv записан")
    elif cmd not in ("download", "parse", "sync", "check"):
        print(__doc__)


if __name__ == "__main__":
    _main(sys.argv[1:])
