"""Скачивает эмблемы команд из русской Википедии в site/logos/ и дописывает site/logos/list.json.
Команды, у которых эмблема уже есть в list.json, пропускаются (свои файлы не перезаписываются).
Запускается в GitHub Actions (там есть интернет). Результат — лучшее из возможного: если картинку найти
не удалось, у команды остаётся щит-монограмма."""
import json, re, sys, time, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGOS = ROOT / "site" / "logos"
API = "https://ru.wikipedia.org/w/api.php"
UA = {"User-Agent": "khl-stats-logos/1.0 (github pages hobby project)"}

TITLES = {
    "Авангард": "Авангард (хоккейный клуб)", "Автомобилист": "Автомобилист (хоккейный клуб)",
    "Адмирал": "Адмирал (хоккейный клуб)", "Ак Барс": "Ак Барс (хоккейный клуб)",
    "Амур": "Амур (хоккейный клуб)", "Барыс": "Барыс (хоккейный клуб)",
    "Динамо М": "Динамо (хоккейный клуб, Москва)", "Динамо Мн": "Динамо (хоккейный клуб, Минск)",
    "Драконы": "Шанхайские Драконы", "Лада": "Лада (хоккейный клуб)",
    "Локомотив": "Локомотив (хоккейный клуб)", "Металлург Мг": "Металлург (хоккейный клуб, Магнитогорск)",
    "Нефтехимик": "Нефтехимик (хоккейный клуб)", "СКА": "СКА (хоккейный клуб)",
    "Салават Юлаев": "Салават Юлаев (хоккейный клуб)", "Северсталь": "Северсталь (хоккейный клуб)",
    "Сибирь": "Сибирь (хоккейный клуб)", "Спартак": "Спартак (хоккейный клуб, Москва)",
    "Торпедо": "Торпедо (хоккейный клуб, Нижний Новгород)", "Трактор": "Трактор (хоккейный клуб)",
    "ХК Сочи": "Сочи (хоккейный клуб)", "ЦСКА": "ЦСКА (хоккейный клуб)",
}


def get(url, binary=False):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
        data = r.read()
    return data if binary else json.loads(data.decode("utf-8"))


BAD = re.compile(r"flag|map|jersey|форма|kit|arena|арена|stadium|стадион|дворец|player|commons-logo|wikidata|wiki|edit|question|symbol|icon|ambox|folder|nuvola|portal|stub|increase|decrease|steady", re.I)
GOOD = re.compile(r"logo|лого|эмбл|emblem|crest|герб", re.I)


def api(**q):
    return get(API + "?" + urllib.parse.urlencode({"format": "json", "redirects": 1, **q}))


def logo_url(title):
    """Ищет среди файлов страницы клуба эмблему (svg/png с «лого/эмблема/logo» в имени) и отдаёт её адрес."""
    pages = api(action="query", prop="images", imlimit="max", titles=title).get("query", {}).get("pages", {})
    names = [i["title"] for p in pages.values() for i in p.get("images", [])]
    cand = [n for n in names if re.search(r"\.(svg|png)$", n, re.I) and not BAD.search(n)]
    pick = next((n for n in cand if GOOD.search(n)), cand[0] if cand else None)
    if not pick:
        return None
    info = api(action="query", prop="imageinfo", iiprop="url", iiurlwidth=300, titles=pick).get("query", {}).get("pages", {})
    for p in info.values():
        ii = (p.get("imageinfo") or [{}])[0]
        return ii.get("thumburl") or ii.get("url")
    return None


def main():
    LOGOS.mkdir(parents=True, exist_ok=True)
    lp = LOGOS / "list.json"
    lst = json.loads(lp.read_text(encoding="utf-8")) if lp.exists() else {}
    teams = sorted({t for m in json.loads((ROOT / "site/data/matches.json").read_text(encoding="utf-8"))["matches"]
                    for t in (m["home"], m["away"])})
    added = 0
    for t in teams:
        if t in lst and (LOGOS / lst[t]).exists():
            continue
        try:
            src = logo_url(TITLES[t]) if t in TITLES else None
            if not src:
                print(f"{t}: подходящей эмблемы не нашли")
                continue
            ext = ".png" if ".png" in src.lower() else ".svg"
            slug = re.sub(r"[^a-z0-9]+", "-", f"team{len(lst) + 1}").strip("-")
            name = f"{slug}{ext}"
            (LOGOS / name).write_bytes(get(src, binary=True))
            lst[t] = name
            added += 1
            print(f"{t}: {src}")
        except Exception as e:
            print(f"{t}: ошибка {e!r}")
        time.sleep(1)
    lp.write_text(json.dumps(lst, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"готово, добавлено эмблем: {added}")


if __name__ == "__main__":
    sys.exit(main())
