"""
구종가치 계산 결과(pitch_values/구종가치_연도.csv)를 사이트용 파일 하나로 묶는다.

사용법 (kbo-saber 폴더에서):
  python3 scraper/pitch_site.py ../pitch_values site/data/pitch_values.json

- 시즌 중 팀을 옮긴 투수는 팀별로 나뉜 기록을 합친다 (구사율·평균구속 다시 계산)
- 2015년 이전은 중계에 아웃·점수 기록이 없어 2016년 득점가치를 빌려 쓴 추정치라서 est 목록에 넣는다
"""
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

ESTIMATED_BEFORE = 2016  # 이 해 전 시즌은 추정치
KIND = {"정규시즌": 0, "포스트시즌": 1}


def main(src: str, out: str):
    src_dir = Path(src)
    files = sorted(src_dir.glob("구종가치_*.csv"))
    if not files:
        sys.exit(f"{src_dir} 에 구종가치_연도.csv 파일이 없어요. pitch_values.py 를 먼저 실행해 주세요.")

    rows, years = [], []
    for f in files:
        year = int(f.stem.split("_")[1])
        years.append(year)
        agg = defaultdict(lambda: {"n": 0, "v": 0.0, "spd": 0.0, "spd_n": 0})
        total = defaultdict(int)
        with open(f, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                kind = KIND.get(r["구분"])
                if kind is None or not r["선수코드"]:
                    continue
                n = int(r["투구수"])
                a = agg[(kind, r["선수코드"], r["구종"])]
                a["n"] += n
                a["v"] += float(r["구종가치"])
                if r["평균구속"]:
                    a["spd"] += float(r["평균구속"]) * n
                    a["spd_n"] += n
                total[(kind, r["선수코드"])] += n
        for (kind, pid, pitch), a in agg.items():
            n = a["n"]
            rows.append([
                year, pid, kind, pitch, n,
                round(100 * n / total[(kind, pid)], 1),
                round(a["spd"] / a["spd_n"], 1) if a["spd_n"] else None,
                round(a["v"], 1),
                round(100 * a["v"] / n, 2) if n else None,
            ])

    rows.sort(key=lambda r: (r[0], r[1], r[2], -r[4]))
    data = {
        "cols": ["y", "id", "k", "pt", "n", "usage", "velo", "val", "per100"],
        "rows": rows,
        "years": [min(years), max(years)],
        "est": [y for y in years if y < ESTIMATED_BEFORE],
    }
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"완료: {out}  ({len(rows)}줄, {min(years)}~{max(years)}, {Path(out).stat().st_size // 1024} KB)")


if __name__ == "__main__":
    a = sys.argv[1:]
    main(a[0] if a else "../pitch_values", a[1] if len(a) > 1 else "site/data/pitch_values.json")
