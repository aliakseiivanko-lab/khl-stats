"""Расчёт статистики клуба: сезон / последние 5, 10, 20; все / дома / на выезде."""
from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Optional


@dataclass
class Match:
    date: str          # YYYY-MM-DD
    season: str        # например "2026/27"
    home: str
    away: str
    home_goals: int    # итоговый счёт (при буллитах включает победный буллит)
    away_goals: int
    ot: bool = False   # игра закончилась в ОТ или по буллитам
    so: bool = False   # решилась по буллитам
    home_shots: Optional[int] = None
    away_shots: Optional[int] = None
    home_pp_goals: Optional[int] = None   # голы в большинстве
    home_pp_opps: Optional[int] = None    # сколько было большинств
    away_pp_goals: Optional[int] = None
    away_pp_opps: Optional[int] = None
    home_pim: Optional[int] = None        # штрафные минуты
    away_pim: Optional[int] = None
    home_shg: Optional[int] = None        # голы, забитые в меньшинстве
    away_shg: Optional[int] = None
    home_fo: Optional[int] = None         # выигранные вбрасывания
    away_fo: Optional[int] = None
    home_hits: Optional[int] = None       # силовые приёмы
    away_hits: Optional[int] = None
    home_blocks: Optional[int] = None     # блокированные броски (сделанные командой)
    away_blocks: Optional[int] = None
    time: str = ""                       # время начала матча (по протоколу), для порядка игр внутри дня


def _pov(m: Match, team: str) -> dict:
    """Матч глазами команды."""
    h = m.home == team
    p, o = ("home_", "away_") if h else ("away_", "home_")
    g = lambda side, k: getattr(m, side + k)
    gf, ga = g(p, "goals"), g(o, "goals")
    return {
        "home": h, "date": m.date, "ot": m.ot, "so": m.so,
        "gf": gf, "ga": ga,
        # голы самой игры: без победного буллита
        "gfg": gf - (1 if m.so and gf > ga else 0),
        "gag": ga - (1 if m.so and ga > gf else 0),
        "sf": g(p, "shots"), "sa": g(o, "shots"),
        "ppg": g(p, "pp_goals"), "ppo": g(p, "pp_opps"),
        "ppga": g(o, "pp_goals"), "ppoa": g(o, "pp_opps"),
        "pim": g(p, "pim"),
        "shg": g(p, "shg"),
        "fo": g(p, "fo"), "foa": g(o, "fo"),
        "hit": g(p, "hits"), "blk": g(p, "blocks"),
    }


def select(matches: list[Match], team: str, n: Optional[int] = None,
           venue: str = "all", season: Optional[str] = None) -> list[dict]:
    ms = [m for m in matches if team in (m.home, m.away)]
    if season:
        ms = [m for m in ms if m.season == season]
    if venue == "home":
        ms = [m for m in ms if m.home == team]
    elif venue == "away":
        ms = [m for m in ms if m.away == team]
    ms.sort(key=lambda m: m.date)
    if n:
        ms = ms[-n:]
    return [_pov(m, team) for m in ms]


def _avg(rows, k):
    v = [r[k] for r in rows if r[k] is not None]
    return sum(v) / len(v) if v else None


def _ratio(rows, a, b):
    pairs = [(r[a], r[b]) for r in rows if r[a] is not None and r[b] is not None]
    den = sum(b for _, b in pairs)
    return sum(a for a, _ in pairs) / den if den else None


def _pair_sum(rows, a, b):
    """(сумма a, сумма b) по матчам, где есть оба значения."""
    p = [(r[a], r[b]) for r in rows if r[a] is not None and r[b] is not None]
    return (sum(x for x, _ in p), sum(y for _, y in p)) if p else None


def _cov(rows, key):
    return sum(r[key] is not None for r in rows) / len(rows)


def calc(rows: list[dict]) -> Optional[dict]:
    gp = len(rows)
    if not gp:
        return None
    wins = [r for r in rows if r["gf"] > r["ga"]]
    pts = sum(2 if r["gf"] > r["ga"] else (1 if r["ot"] else 0)
              for r in rows)
    sh = _ratio(rows, "gfg", "sf")
    sv = _ratio(rows, "gag", "sa")
    fo = _pair_sum(rows, "fo", "foa")
    shg = [r["shg"] for r in rows if r["shg"] is not None]
    return {
        "gp": gp,
        "w": len(wins), "l": gp - len(wins),
        "ot": sum(r["ot"] for r in rows),
        "pts_pg": pts / gp,
        "gf": sum(r["gfg"] for r in rows) / gp,
        "ga": sum(r["gag"] for r in rows) / gp,
        "sho": sum(r["ga"] == 0 and not r["so"] for r in rows),
        "sf": _avg(rows, "sf"), "sa": _avg(rows, "sa"),
        "sh": None if sh is None else 100 * sh,
        "sv": None if sv is None else 100 * (1 - sv),
        "pp2": _pair_sum(rows, "ppg", "ppo"),      # (голы, большинств)
        "pk2": _pair_sum(rows, "ppga", "ppoa"),    # (пропущено, меньшинств)
        "shg": sum(shg) if shg else None,
        "pim": _avg(rows, "pim"),
        "fo_pct": None if not fo or sum(fo) == 0 else 100 * fo[0] / sum(fo),
        "blk": _avg(rows, "blk"), "hit": _avg(rows, "hit"),
        "ppg_sum": sum(r["ppg"] for r in rows if r["ppg"] is not None),
        "ppga_sum": sum(r["ppga"] for r in rows if r["ppga"] is not None),
        "cov_pim": _cov(rows, "pim"),
        "cov": _cov(rows, "ppo"),         # доля матчей с данными по большинству/штрафам
        "cov_shg": _cov(rows, "shg"),
        "cov_fo": _cov(rows, "fo"),       # вбрасывания
        "cov_blk": _cov(rows, "blk"),     # блокированные броски
        "cov_hit": _cov(rows, "hit"),     # силовые
    }


def form(rows: list[dict], k: int = 5) -> str:
    out = []
    for r in rows[-k:]:
        out.append("🟢" if r["gf"] > r["ga"] else ("🟠" if r["ot"] else "🔴"))
    return "".join(out)


def _plural(n: int, forms: tuple) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return forms[0]
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return forms[1]
    return forms[2]


def streak_text(rows: list[dict]) -> str:
    if not rows:
        return "–"
    win = rows[-1]["gf"] > rows[-1]["ga"]
    c = 0
    for r in reversed(rows):
        if (r["gf"] > r["ga"]) == win:
            c += 1
        else:
            break
    word = (_plural(c, ("победа", "победы", "побед")) if win
            else _plural(c, ("поражение", "поражения", "поражений")))
    return f"{c} {word}" + (" подряд" if c > 1 else "")


# ───────────── оформление ─────────────

def _f(v, d=1):
    return "–" if v is None else f"{v:.{d}f}"


def _goals_over_chances(pair):
    """«7/45/16%» — голы / сколько было попыток / процент."""
    if not pair or not pair[1]:
        return "–"
    g, n = pair
    return f"{g}/{n}/{round(100 * g / n)}%"


def _pk_text(pair):
    """«18/86/79%» — пропущено / сколько было меньшинств / % отыграно (100 − % пропущенных)."""
    if not pair or not pair[1]:
        return "–"
    g, n = pair
    return f"{g}/{n}/{100 - round(100 * g / n)}%"


def _need(cov_key, fn, need=0.6):
    """Показываем значение, только если данные есть минимум по 60% матчей окна."""
    return lambda s: fn(s) if s[cov_key] >= need else "–"


SECTIONS = [
    ("Результаты", [
        ("Матчей",            lambda s: str(s["gp"])),
        ("Победы-пораж.",     lambda s: f'{s["w"]}-{s["l"]}'),
        ("Очки за матч",      lambda s: _f(s["pts_pg"], 2)),
        ("Матчей в ОТ/Б",     lambda s: str(s["ot"])),
    ]),
    ("Голы", [
        ("Забито за матч",    lambda s: _f(s["gf"])),
        ("Пропущено за матч", lambda s: _f(s["ga"])),
        ("Сухих матчей",      lambda s: str(s["sho"])),
    ]),
    ("Спецбригады", [
        ("Голы в больш.",     _need("cov_pim", lambda s: str(s["ppg_sum"]))),
        ("Пропущено в меньш.", _need("cov_pim", lambda s: str(s["ppga_sum"]))),
        ("Большинство",       _need("cov", lambda s: _goals_over_chances(s["pp2"]), 0.2)),
        ("Меньшинство",       _need("cov", lambda s: _pk_text(s["pk2"]), 0.2)),
        ("Забито в меньш.",   _need("cov_shg", lambda s: str(s["shg"]) if s["shg"] is not None else "–")),
        ("Штраф, мин/матч",   _need("cov_pim", lambda s: _f(s["pim"]))),
    ]),
    ("Игра", [
        ("Вбрасывания, %",    _need("cov_fo", lambda s: _f(s["fo_pct"], 0))),
        ("Блок. броски/матч", _need("cov_blk", lambda s: _f(s["blk"]))),
        ("Силовые/матч",      _need("cov_hit", lambda s: _f(s["hit"]))),
    ]),
    ("Броски", [
        ("Броски/матч",       lambda s: _f(s["sf"])),
        ("Броски пр./матч",   lambda s: _f(s["sa"])),
        ("Реализация, %",     lambda s: _f(s["sh"])),
        ("Сейвы, %",          lambda s: _f(s["sv"])),
    ]),
]

LABEL_W = 18


def _table(cols: list) -> str:
    """cols: [(заголовок, stats|None)]. Строки без данных скрываются."""
    w = 11 if len(cols) == 1 else 10
    out = []
    if len(cols) > 1:
        out.append(" " * LABEL_W + "".join(h.rjust(w) for h, _ in cols))
    for title, rows in SECTIONS:
        body = []
        for label, fn in rows:
            cells = [fn(s) if s else "–" for _, s in cols]
            if all(c == "–" for c in cells):
                continue
            body.append(label.ljust(LABEL_W) + "".join(c.rjust(w) for c in cells))
        if body:
            out.append(f"— {title} —")
            out += body
    return "\n".join(out)


VENUE_TITLE = {"all": "все матчи", "home": "домашние матчи", "away": "гостевые матчи"}
LEGEND = ("<i>Большинство: голы / сколько было большинств / %.\n"
          "Меньшинство: пропущено / сколько было меньшинств / % отыграно.\n"
          "🟢 победа · 🟠 проигрыш в ОТ/буллитах · 🔴 проигрыш</i>")


def current_season(matches: list[Match]) -> Optional[str]:
    return max((m.season for m in matches), default=None)


def _coverage_note(rows: list[dict]) -> str:
    have = sum(r["ppo"] is not None for r in rows)
    if rows and have < len(rows):
        return (f"\nℹ️ Число большинств есть не для всех матчей "
                f"({have} из {len(rows)}): % считается по ним")
    return ""


def render_report(matches: list[Match], team: str, venue: str = "all",
                  window: Optional[int] = None) -> str:
    """window=None — весь сезон, иначе последние N матчей."""
    season = current_season(matches)
    season_rows = select(matches, team, None, venue, season)
    rows = season_rows[-window:] if window else season_rows
    period = f"последние {window} матчей" if window else "весь сезон"
    head = (f"<b>{html.escape(team)}</b>\n{VENUE_TITLE[venue]} · {period}\n"
            f"Сезон {season}\n")
    if not rows:
        return head + "\nПо этому фильтру матчей пока нет."
    return (head
            + f"\nФорма (5 последних): {form(season_rows)}\n"
              f"Серия: {streak_text(season_rows)}\n"
            + "\n<pre>" + _table([("", calc(rows))]) + "</pre>\n"
            + LEGEND + _coverage_note(rows))


def render_compare(matches: list[Match], team: str) -> str:
    season = current_season(matches)
    cols = [(name, calc(select(matches, team, None, v, season)))
            for name, v in (("Дома", "home"), ("Выезд", "away"))]
    home = select(matches, team, None, "home", season)
    away = select(matches, team, None, "away", season)
    head = (f"<b>{html.escape(team)}</b>\nдома и на выезде · весь сезон\n"
            f"Сезон {season}\n"
            f"\nФорма дома: {form(home) or '–'}\nФорма на выезде: {form(away) or '–'}\n")
    return (head + "\n<pre>" + _table(cols) + "</pre>\n" + LEGEND
            + _coverage_note(home + away))


# ───────────── последняя игра ─────────────

def last_match(matches: list[Match], team: str) -> Optional[Match]:
    season = current_season(matches)
    ms = sorted((m for m in matches if team in (m.home, m.away) and m.season == season),
                key=lambda m: m.date)
    return ms[-1] if ms else None


def _pair_txt(g, n):
    return "–" if g is None or n is None else f"{g}/{n}"


def _fo_txt(r):
    if r["fo"] is None or r["foa"] is None or r["fo"] + r["foa"] == 0:
        return "–"
    return f'{r["fo"]} ({round(100 * r["fo"] / (r["fo"] + r["foa"]))}%)'


def render_last(matches: list[Match], team: str) -> str:
    m = last_match(matches, team)
    if not m:
        return f"<b>{html.escape(team)}</b>\nМатчей пока нет."
    me = _pov(m, team)
    opp_name = m.away if m.home == team else m.home
    op = _pov(m, opp_name)
    if me["gf"] > me["ga"]:
        res = "🟢 Победа" + (" по буллитам" if m.so else " в ОТ" if m.ot else "")
    elif m.ot:
        res = "🟠 Поражение" + (" по буллитам" if m.so else " в ОТ")
    else:
        res = "🔴 Поражение"
    d = m.date.split("-")
    date_txt = f"{d[2]}.{d[1]}.{d[0]}"
    head = (f"<b>{html.escape(team)}</b>\nпоследняя игра · {date_txt}\n\n"
            f"{html.escape(m.home)} {m.home_goals}:{m.away_goals} {html.escape(m.away)}"
            f"{' (Б)' if m.so else ' (ОТ)' if m.ot else ''}\n"
            f"{res} · {'дома' if me['home'] else 'на выезде'}\n")
    rows = [
        ("Броски",          lambda r: "–" if r["sf"] is None else str(r["sf"])),
        ("Большинство",     lambda r: _pair_txt(r["ppg"], r["ppo"])),
        ("Штраф, мин",      lambda r: "–" if r["pim"] is None else str(r["pim"])),
        ("Забито в меньш.", lambda r: "–" if r["shg"] is None else str(r["shg"])),
        ("Вбрасывания",     _fo_txt),
        ("Блок. броски",    lambda r: "–" if r["blk"] is None else str(r["blk"])),
        ("Силовые",         lambda r: "–" if r["hit"] is None else str(r["hit"])),
    ]
    w = 11
    out = [" " * LABEL_W + "Свои".rjust(w) + "Соперн.".rjust(w)]
    for label, fn in rows:
        a, b = fn(me), fn(op)
        if a == "–" and b == "–":
            continue
        out.append(label.ljust(LABEL_W) + a.rjust(w) + b.rjust(w))
    body = ("\n<pre>" + "\n".join(out) + "</pre>") if len(out) > 1 else "\nПодробной статистики по матчу пока нет."
    return head + body + "\n<i>Большинство: голы / число большинств</i>"


# ───────────── таблица лиги: все команды по категориям ─────────────

def _short(name: str, n: int = 9) -> str:
    return name if len(name) <= n else name[:n]


def _pct_pair(p):
    return None if not p or not p[1] else 100 * p[0] / p[1]


def _fmt_pair(p):
    return "–" if not p or not p[1] else f"{p[0]}/{p[1]}"


def _diff(a, b):
    return None if a is None or b is None else a - b


def _sgn(v, d=1):
    return "–" if v is None else f"{v:+.{d}f}"


def _pk_pct(s):
    p = _pct_pair(s["pk2"])
    return None if p is None else 100 - p


def _sum2(a, b):
    return None if a is None or b is None else a + b


_GP = ("И", lambda s: str(s["gp"]))

# ключ, кнопка, заголовок, колонки [(шапка, fn(s))], чем сортируем, сортировать по убыванию?
CATS = [
    ("pts", "Очки", "Очки за матч",
     [_GP, ("В-П", lambda s: f'{s["w"]}-{s["l"]}'), ("О/м", lambda s: _f(s["pts_pg"], 2))],
     lambda s: s["pts_pg"], True),
    ("gls", "Голы", "Голы за матч",
     [("ЗБ/м", lambda s: _f(s["gf"])), ("ПР/м", lambda s: _f(s["ga"])),
      ("±/м", lambda s: _sgn(s["gf"] - s["ga"]))],
     lambda s: s["gf"] - s["ga"], True),
    ("pp", "Большинство", "Большинство (голы / большинств / %)",
     [("Г/Б", lambda s: _fmt_pair(s["pp2"])), ("%", lambda s: _f(_pct_pair(s["pp2"]), 1))],
     lambda s: _pct_pair(s["pp2"]), True),
    ("pk", "Меньшинство", "Игра в меньшинстве (пропущено / меньшинств / % отыграно)",
     [("Пр/М", lambda s: _fmt_pair(s["pk2"])), ("%", lambda s: _f(_pk_pct(s), 1))],
     _pk_pct, True),
    ("pim", "Штраф", "Штрафные минуты за матч (меньше — выше)",
     [_GP, ("Мин/м", lambda s: _f(s["pim"]))], lambda s: s["pim"], False),
    ("fo", "Вбрасывания", "Выигранные вбрасывания, %",
     [_GP, ("Вбр%", lambda s: _f(s["fo_pct"], 1))], lambda s: s["fo_pct"], True),
    ("sh", "Броски", "Броски в створ за матч",
     [("Бр/м", lambda s: _f(s["sf"])), ("Бр.пр", lambda s: _f(s["sa"])),
      ("Разн", lambda s: _sgn(_diff(s["sf"], s["sa"])))],
     lambda s: _diff(s["sf"], s["sa"]), True),
    ("eff", "Реализация", "Реализация и сейвы, % (PDO = сумма)",
     [("Реал", lambda s: _f(s["sh"])), ("Сейв", lambda s: _f(s["sv"])),
      ("PDO", lambda s: _f(_sum2(s["sh"], s["sv"])))],
     lambda s: _sum2(s["sh"], s["sv"]), True),
    ("blk", "Блоки", "Блокированные броски за матч",
     [_GP, ("Бл/м", lambda s: _f(s["blk"]))], lambda s: s["blk"], True),
    ("hit", "Силовые", "Силовые приёмы за матч",
     [_GP, ("Сил/м", lambda s: _f(s["hit"]))], lambda s: s["hit"], True),
]
CAT_BY_KEY = {c[0]: c for c in CATS}


def render_league(matches: list[Match], cat: str = "pts", venue: str = "all",
                  window: Optional[int] = None) -> str:
    key, _, title, cols, sort_fn, desc = CAT_BY_KEY.get(cat, CATS[0])
    season = current_season(matches)
    teams = sorted({t for m in matches for t in (m.home, m.away)})
    data = []
    for t in teams:
        s = calc(select(matches, t, window, venue, season))
        if s:
            data.append((t, s))

    def k(item):
        v = sort_fn(item[1])
        return (v is None, 0 if v is None else (-v if desc else v))
    data.sort(key=k)

    period = f"последние {window} матчей каждой команды" if window else "весь сезон"
    head = f"<b>Лига · {title}</b>\n{VENUE_TITLE[venue]} · {period}\nСезон {season}\n"
    if not data:
        return head + "\nМатчей по этому фильтру пока нет."
    w = 7
    lines = [f"{'#':>2} {'Команда':<11}" + "".join(h.rjust(w) for h, _ in cols)]
    for i, (t, s) in enumerate(data, 1):
        lines.append(f"{i:>2} {_short(t, 11):<11}" + "".join(fn(s).rjust(w) for _, fn in cols))
    return head + "\n<pre>" + "\n".join(lines) + "</pre>"


# ───────────── сравнение двух команд ─────────────

def render_vs(matches: list[Match], a: str, b: str, venue: str = "all",
              window: Optional[int] = None) -> str:
    """venue: all — все матчи обеих; ha — первая дома, вторая в гостях."""
    season = current_season(matches)
    va, vb = ("home", "away") if venue == "ha" else ("all", "all")
    ra = select(matches, a, window, va, season)
    rb = select(matches, b, window, vb, season)
    period = f"последние {window} матчей" if window else "весь сезон"
    where = (f"{html.escape(a)} — дома, {html.escape(b)} — в гостях" if venue == "ha"
             else "все матчи обеих команд")
    head = (f"<b>{html.escape(a)} vs {html.escape(b)}</b>\n{where} · {period}\nСезон {season}\n"
            f"\nФорма {html.escape(_short(a))}: {form(ra) or '–'}\n"
            f"Форма {html.escape(_short(b))}: {form(rb) or '–'}\n")
    if not ra or not rb:
        return head + "\nУ одной из команд нет матчей по этому фильтру."
    cols = [(_short(a), calc(ra)), (_short(b), calc(rb))]
    return head + "\n<pre>" + _table(cols) + "</pre>\n" + LEGEND
