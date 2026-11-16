"""Скачивает новые протоколы khl.ru, разбирает их и пишет site/data/matches.json."""
import datetime as dt, json, sys
from dataclasses import asdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import protocols as P

_h = P.have_ids
P.have_ids = lambda: _h() | {int(k) for k in P.load_games()}   # уже разобранные заново не качаем
try:
    print("скачано новых:", P.download_all())
except P.Blocked as e:
    print("ВНИМАНИЕ:", e)
P.parse_new()
ms = P.build_matches()
out = Path("site/data/matches.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
                           "matches": [asdict(m) for m in ms]}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print("матчей:", len(ms))
sys.exit(0 if ms else 1)
