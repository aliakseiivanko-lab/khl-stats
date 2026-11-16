"""Игроки из PDF-протокола КХЛ. pypdf в режиме layout сохраняет позиции колонок (пустые ячейки не сдвигают числа).
Запись игрока: [сторона 0=хозяева/1=гости, №, имя, поз(з/н/в), сек на льду, смен, Ш, А, +/-, удал., штр.мин,
                Б, Бс, Бм, БлБ, СПр, Отб, ПХТ, Вбр, ВВбр, пропущено(вр), бросков(вр)]"""
import io, re, sys, json

VERSION = 1
COLS = ["Б", "Бс", "Бм", "БлБ", "СПр", "Отб", "ПХТ", "Вбр", "ВВбр"]


def _sec(m):
    return int(m.group(1)) * 60 + int(m.group(2))


def parse_players(data: bytes, home: str, away: str) -> list:
    from pypdf import PdfReader
    t = "\n".join((p.extract_text(extraction_mode="layout") or "") for p in PdfReader(io.BytesIO(data)).pages)
    L = [l.rstrip() for l in t.replace("\xa0", " ").splitlines()]
    flat = [re.sub(r"\s+", " ", l).strip() for l in L]
    pl, side, cent = [{}, {}], -1, {}
    for ln in L:
        if "ВВбр" in ln and "Бс" in ln:                      # шапка таблицы: центры колонок
            side += 1
            base = max(ln.find("площадке"), 0)
            cent = {m.group(): (m.start() + m.end()) / 2 for m in re.finditer(r"\S+", ln) if m.start() > base and (m.group() in COLS or m.group() == "Вбр%")}
            continue
        m = side >= 0 and re.match(r"\s*(\d+)\s+([знв])\s+(\D+?)(?=\s{2,}|$)", ln)
        if not m:
            continue
        rest, pos = ln[m.end():], m.group(2)
        d = {"n": int(m.group(1)), "pos": pos, "name": m.group(3).strip(), "sh": 0, "g": 0, "a": 0, "pm": 0,
             "pen": 0, "pim": 0, "ga": None, "sa": None, **{c: None for c in COLS}}
        if pos == "в":
            tm = re.search(r"(\d+):(\d{2})", rest)
            d["toi"] = _sec(tm) if tm else 0
        else:
            mt = re.search(r"(\d+):(\d{2})\s+(\d+)\s+(?:\d+:\d{2}\s*\(\d+\)\s*)+\d+:\d{2}", rest)
            if not mt:
                continue                                      # не играл
            d["toi"], d["sh"] = _sec(mt), int(mt.group(3))
            off = m.end() + mt.end()
            for c in COLS:
                d[c] = 0
            for tk in re.finditer(r"\d+", rest[mt.end():]):
                x = off + (tk.start() + tk.end()) / 2
                c = min(cent, key=lambda k: abs(cent[k] - x)) if cent else None
                if c in COLS:
                    d[c] += int(tk.group())
        if d["toi"] > 0:
            pl[side][d["n"]] = d

    def block(a, b):
        i = next((k for k, l in enumerate(flat) if l == a), None)
        if i is None:
            return []
        j = next((k for k in range(i + 1, len(flat)) if flat[k].startswith(b)), len(flat))
        return flat[i + 1:j]

    for l in block("ГОЛЫ", "УДАЛЕНИЯ"):                      # голы, передачи, +/-
        m = re.match(r"\d+ \d+ [–-] \d+ \d{1,3}:\d{2} (\S+) (.+) (\d+(?:,\d+)+) (\d+(?:,\d+)+)$", l)
        if not m:
            continue
        state, rest, i1, i2 = m.groups()
        sd = 0 if rest.startswith(home) and not (rest.startswith(away) and len(away) > len(home)) else 1
        rest = rest[len(home if sd == 0 else away):].strip()
        for k, (n, _) in enumerate(re.findall(r"(\d+) (\D+?)(?= \d+ |$)", rest)):
            r = pl[sd].get(int(n))
            if r:
                r["g" if k == 0 else "a"] += 1
        if state != "бол":                                    # голы в большинстве в +/- не идут
            for i, ice in enumerate((i1, i2)):
                for n in ice.split(","):
                    r = pl[i].get(int(n))
                    if r and r["pos"] != "в":
                        r["pm"] += 1 if i == sd else -1
    hi = next((k for k, l in enumerate(L) if len(re.findall(r"#\s+Период", l)) == 2), None)   # удаления: слева хозяева, справа гости
    if hi is not None:
        cut = list(re.finditer(r"#\s+Период", L[hi]))[1].start() - 3
        for l in L[hi + 1:]:
            if "Итого" in l:
                break
            for ti, part in ((0, l[:cut]), (1, l[cut:])):
                m = re.match(r"\s*\d+\s+\d\s+\d{1,2}:\d{2}\s+(\d+)\s*(\D+?)\s{2,}(\d+)(?:\s|$)", part)
                r = m and pl[ti].get(int(m.group(1)))
                if r:
                    r["pen"] += 1
                    r["pim"] += int(m.group(3))
    gs = 0
    for l in flat:                                            # вратари: пропущено / броски
        if l.startswith("СТАТИСТИКА ВРАТАРЕЙ"):
            gs += 1
        m = gs and re.match(r"(\d+) вр ", l)
        pr = re.findall(r"(\d+) - (\d+)", l)
        r = m and pr and pl[gs - 1].get(int(m.group(1)))
        if r:
            r["ga"], r["sa"] = int(pr[-1][0]), int(pr[-1][1])
    out = []
    for s in (0, 1):
        for d in pl[s].values():
            out.append([s, d["n"], d["name"], d["pos"], d["toi"], d["sh"], d["g"], d["a"], d["pm"], d["pen"], d["pim"],
                        *[d[c] for c in COLS], d["ga"], d["sa"]])
    return out


if __name__ == "__main__":                                   # python players.py game.pdf "Торпедо" "Динамо Мн"
    rows = parse_players(open(sys.argv[1], "rb").read(), sys.argv[2], sys.argv[3])
    for s in (0, 1):
        sk = [r for r in rows if r[0] == s and r[3] != "в"]
        print("сторона", s, "игроков", len(sk), "Ш А +/-+ Штр.мин удал.:", *[sum(r[i] for r in sk) for i in (6, 7, 10, 9)],
              "| Б Бс Бм БлБ СПр Отб ПХТ Вбр ВВбр:", [sum(r[i] for r in sk) for i in range(11, 20)])
    print([r for r in rows if r[3] == "в"])
    print(json.dumps([r for r in rows if r[1] in (63, 36, 24)], ensure_ascii=False))
