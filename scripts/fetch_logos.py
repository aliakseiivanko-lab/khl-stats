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


def page_image(title):
    q = {"action": "query", "format": "json", "redirects": 1, "prop": "pageimages",
         "piprop": "original", "titles": title}
    pages = get(API + "?" + urllib.parse.urlencode(q)).get("query", {}).get("pages", {})
    for p in pages.values():
        src = (p.get("original") or {}).get("source")
        if src:
            return src
    return None


def search_image(team):
    q = {"action": "query", "format": "json", "generator": "search", "gsrlimit": 1,
         "gsrsearch": f"{team} хоккейный клуб КХЛ", "prop": "pageimages", "piprop": "original"}
    pages = get(API + "?" + urllib.parse.urlencode(q)).get("query", {}).get("pages", {})
    for p in pages.values():
        src = (p.get("original") or {}).get("source")
        if src:
            return src
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
            src = page_image(TITLES[t]) if t in TITLES else None
            src = src or search_image(t)
            ext = Path(urllib.parse.urlparse(src or "").path).suffix.lower()
            if not src or ext not in (".svg", ".png"):      # jpg — почти всегда фото, а не эмблема
                print(f"{t}: подходящей эмблемы не нашли ({src})")
                continue
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
