"""
KBO 구종가치 계산기 (FanGraphs Pitch Values 방식)

사용법 (터미널):
  python3 pitch_values.py --years 2024-2026

collect_pitches.py 로 모아둔 pitch_data/연도/*.json.gz 를 읽어서
pitch_values/구종가치_연도.csv 와 pitch_values/pitch_values_연도.json 을 만듭니다.

계산 순서
  1) 시즌 전체 타석으로 기대득점표(RE24)를 만든다
  2) 타석 결과(1루타, 볼넷, 삼진, 아웃 ...)별 평균 득점가치(선형가중치)를 구한다
  3) 볼카운트별 가치 = 그 카운트를 거쳐간 타석들의 결과 가치 평균
  4) 공 하나의 가치 = (던진 후 가치) - (던지기 전 가치)
     타석이 끝난 공은 (결과 가치) - (던지기 전 카운트 가치)
  5) 투수 입장으로 부호를 뒤집어(+가 투수에게 좋음) 투수·구종별로 합산
     → 구종가치(누적, 점) 와 100구당 구종가치
"""
from __future__ import annotations

import argparse
import os
import csv
import gzip
import json
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# 기본은 이 파일 옆 폴더, GitHub Actions에서는 PITCH_DATA_DIR / PITCH_OUT_DIR 로 바꿈
DATA_DIR = Path(os.environ.get("PITCH_DATA_DIR") or BASE_DIR / "pitch_data")
OUT_DIR = Path(os.environ.get("PITCH_OUT_DIR") or BASE_DIR / "pitch_values")

# 네이버 구종 이름 → FanGraphs 표기
PITCH_CODE = {
    "직구": "FB", "포심": "FB",
    "투심": "SI", "싱커": "SI",
    "커터": "CT",
    "슬라이더": "SL",
    "스위퍼": "ST",  # 네이버가 스위퍼로 적은 공만 스위퍼로 따로 표시 (예전 슬라이더 기록은 그대로)
    "커브": "CB", "너클커브": "CB", "슬러브": "CB",
    "체인지업": "CH", "서클체인지업": "CH",
    "포크": "SF", "스플리터": "SF",
    "너클볼": "KN",
}


# ---------- 타석 결과 분류 ----------
def classify(text: str) -> str:
    t = text.split(":", 1)[-1].replace(" ", "")
    if "홈런" in t:
        return "HR"
    if "3루타" in t:
        return "3B"
    if "2루타" in t:
        return "2B"
    if "1루타" in t or "안타" in t:
        return "1B"
    if "고의4구" in t or "고의사구" in t:
        return "IBB"
    if "볼넷" in t or "4구" in t:
        return "BB"
    if "몸에맞는" in t or "사구" in t:
        return "HBP"
    if "삼진" in t or "낫아웃" in t:
        return "K"
    if "실책" in t:
        return "ROE"
    if "희생번트" in t:
        return "SH"
    if "희생플라이" in t:
        return "SF"
    if "병살" in t or "삼중살" in t:
        return "DP"
    if "야수선택" in t:
        return "FC"
    if "아웃" in t:
        return "OUT"
    return "OTHER"


def num(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def base_state(gs: dict) -> int:
    """주자 상황을 0~7 숫자로 (1루=1, 2루=2, 3루=4 를 더한 값)"""
    return (num(gs.get("base1")) > 0) * 1 + (num(gs.get("base2")) > 0) * 2 + (num(gs.get("base3")) > 0) * 4


def game_kind(game: dict) -> str | None:
    rc = (game.get("round_code") or "").lower()
    title = game.get("title") or ""
    if rc == "kbo_r":
        return "정규시즌"
    if rc == "kbo_e" or "allstar" in rc or "올스타" in title:
        return None
    return "포스트시즌"


NAMES: dict = {}


def build_name_map():
    """2011년처럼 투수 이름이 빠진 시즌을 위해, 모든 시즌의 선수코드→이름 표를 미리 만든다."""
    for f in DATA_DIR.glob("*/*.json.gz"):
        try:
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                for code, p in json.load(fh).get("players", {}).items():
                    if p.get("name"):
                        NAMES[code] = p["name"]
        except Exception:
            pass


# ---------- 경기 → 타석 목록 ----------
def parse_game(data: dict) -> list[dict]:
    """경기 하나를 타석 단위로 쪼갠다."""
    game = data["game"]
    players = data.get("players", {})
    pas = []
    cur = None
    half_key = None
    half_pas = []

    def close_pa(next_gs=None):
        nonlocal cur
        if cur is None:
            return
        cur["end_gs"] = next_gs
        half_pas.append(cur)
        cur = None

    def close_half():
        nonlocal half_pas
        if not half_pas:
            return
        # 이닝 마지막 상태: 마지막 타석 이후 기록된 마지막 줄의 상태
        last_gs = half_pas[-1].get("last_gs") or {}
        for i, pa in enumerate(half_pas):
            pa["half_end_gs"] = last_gs
            if pa.get("end_gs") is None:
                pa["end_gs"] = last_gs
        pas.extend(half_pas)
        half_pas = []

    for o in data["options"]:
        key = (o.get("_inn"), o.get("_half"))
        gs = o.get("currentGameState") or {}
        if key != half_key:
            close_pa()
            close_half()
            half_key = key
        typ = o.get("type")

        if typ == 8:  # "n번타자 ○○○" = 새 타석 시작
            close_pa(gs)
            cur = {
                "game_id": game["game_id"],
                "kind": game_kind(game),
                "half": o.get("_half"),
                "start_gs": gs,
                "pitches": [],
                "result": None,
                "last_gs": gs,
                "b": 0,
                "s": 0,
            }
            continue

        if cur is None:
            continue
        cur["last_gs"] = gs

        if o.get("pitchNum") is not None and o.get("pitchResult"):
            pitcher = str(gs.get("pitcher") or "")
            # 볼카운트는 투구 결과로 직접 계산 (2015년 이전 중계는 카운트가 0으로 고정돼 있음)
            r = o.get("pitchResult")
            if r == "B":
                cur["b"] += 1
            elif r in ("T", "S", "V", "W"):  # 스트라이크, 헛스윙, 번트헛스윙, 번트파울
                cur["s"] += 1
            elif r == "F" and cur["s"] < 2:
                cur["s"] += 1
            cur["pitches"].append({
                "pitcher": pitcher,
                "pitcher_name": (players.get(pitcher) or {}).get("name") or NAMES.get(pitcher),
                "pitcher_side": "home" if o.get("_half") == "0" else "away",
                "stuff": (o.get("stuff") or "").strip(),
                "speed": num(o.get("speed"), None),
                "result": o.get("pitchResult"),
                "balls_after": cur["b"],
                "strikes_after": cur["s"],
            })
        elif typ in (13, 23) and cur["result"] is None:
            cur["result"] = classify(o.get("text") or "")

    close_pa()
    close_half()

    # 팀 이름 붙이기
    for pa in pas:
        for p in pa["pitches"]:
            p["team"] = game["home"] if p["pitcher_side"] == "home" else game["away"]
    return pas


def bat_score(gs: dict, half: str) -> int:
    return num(gs.get("awayScore")) if half == "0" else num(gs.get("homeScore"))


# ---------- 시즌 계산 ----------
def compute_season(year: int, borrowed_lw: dict | None = None):
    files = sorted((DATA_DIR / str(year)).glob("*.json.gz"))
    if not files:
        print(f"[{year}] 데이터 없음 (pitch_data/{year} 폴더가 비어 있음)")
        return None

    pas = []
    for f in files:
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            pas.extend(parse_game(json.load(fh)))
    reg = [pa for pa in pas if pa["kind"] == "정규시즌"]
    print(f"[{year}] 경기 {len(files)}개, 타석 {len(pas)}개 (정규시즌 {len(reg)})")
    if len(pas) < 50 * len(files):
        print(f"[{year}] ⚠ 경기당 타석이 너무 적어요. 수집이 덜 된 것 같아서 이 시즌은 건너뜁니다.")
        print(f"        python3 collect_pitches.py --years {year} --redo-short  로 다시 받아 주세요.")
        return None

    # 옛날 중계(2015년 이전)는 아웃·주자·점수가 경기 내내 0으로 고정돼 있어서
    # 득점가치를 그 시즌 데이터로 못 구함 → 가장 가까운 정상 시즌의 값을 빌려 씀
    frozen = sum(1 for pa in reg if num(pa["start_gs"].get("out")) > 0) < 0.1 * max(len(reg), 1)
    if frozen and borrowed_lw is None:
        return "frozen"

    # 1) 기대득점표 RE[아웃][주자]  — 이닝이 3아웃으로 끝난 경우만 사용 (끝내기 이닝 제외)
    re_sum = defaultdict(float)
    re_n = defaultdict(int)
    for pa in reg:
        s, e = pa["start_gs"], pa["half_end_gs"]
        if num(e.get("out")) < 3:
            continue
        state = (num(s.get("out")), base_state(s))
        if state[0] > 2:
            continue
        re_sum[state] += bat_score(e, pa["half"]) - bat_score(s, pa["half"])
        re_n[state] += 1
    RE = {k: re_sum[k] / re_n[k] for k in re_n}

    def re_of(gs):
        outs = num(gs.get("out"))
        if outs >= 3:
            return 0.0
        return RE.get((outs, base_state(gs)), 0.0)

    # 2) 결과별 선형가중치 = 결과별 평균 RE24
    lw_sum = defaultdict(float)
    lw_n = defaultdict(int)
    for pa in reg:
        if not pa["result"]:
            continue
        s, e = pa["start_gs"], pa["end_gs"] or pa["half_end_gs"]
        runs = bat_score(e, pa["half"]) - bat_score(s, pa["half"])
        re24 = re_of(e) + runs - re_of(s)
        lw_sum[pa["result"]] += re24
        lw_n[pa["result"]] += 1
    LW = {k: lw_sum[k] / lw_n[k] for k in lw_n}
    if frozen:
        LW = dict(borrowed_lw["LW"])
        RE = {}
        print(f"[{year}] 중계에 아웃·점수 기록이 없어서 {borrowed_lw['year']}년 득점가치를 빌려 씁니다.")

    # 3) 볼카운트별 가치
    cnt_sum = defaultdict(float)
    cnt_n = defaultdict(int)
    for pa in reg:
        if not pa["result"] or not pa["pitches"]:
            continue
        val = LW.get(pa["result"])
        if val is None:
            continue
        seen = {(0, 0)}
        for p in pa["pitches"][:-1]:
            seen.add((min(p["balls_after"], 3), min(p["strikes_after"], 2)))
        for c in seen:
            cnt_sum[c] += val
            cnt_n[c] += 1
    CV = {c: cnt_sum[c] / cnt_n[c] for c in cnt_n}

    # 4) 공 하나하나의 가치 → 5) 투수·구종별 합산 (정규시즌/포스트시즌 따로)
    agg = defaultdict(lambda: {"n": 0, "runs": 0.0, "spd_sum": 0, "spd_n": 0})
    pitcher_total = defaultdict(int)
    for pa in pas:
        if pa["kind"] is None:
            continue
        before = (0, 0)
        last = len(pa["pitches"]) - 1
        for i, p in enumerate(pa["pitches"]):
            v_before = CV.get(before, 0.0)
            if i == last and pa["result"] in LW:
                v_after = LW[pa["result"]]
            else:
                after = (min(p["balls_after"], 3), min(p["strikes_after"], 2))
                v_after = CV.get(after, v_before)
                before = after
            value = -(v_after - v_before)  # 투수 기준: 실점 기대가 줄면 +
            stuff = p["stuff"] or "미분류"
            key = (pa["kind"], p["pitcher"], p["pitcher_name"], p["team"], stuff)
            a = agg[key]
            a["n"] += 1
            a["runs"] += value
            if p["speed"]:
                a["spd_sum"] += p["speed"]
                a["spd_n"] += 1
            pitcher_total[(pa["kind"], p["pitcher"])] += 1

    rows = []
    for (kind, pcode, pname, team, stuff), a in agg.items():
        total = pitcher_total[(kind, pcode)]
        code = PITCH_CODE.get(stuff, stuff)
        rows.append({
            "시즌": year,
            "구분": kind,
            "선수코드": pcode,
            "선수": pname,
            "팀": team,
            "구종": stuff,
            "코드": f"w{code}" if stuff in PITCH_CODE else stuff,
            "투구수": a["n"],
            "구사율": round(100 * a["n"] / total, 1) if total else 0,
            "평균구속": round(a["spd_sum"] / a["spd_n"], 1) if a["spd_n"] else None,
            "구종가치": round(a["runs"], 1),
            "100구당": round(100 * a["runs"] / a["n"], 2) if a["n"] else 0,
        })
    rows.sort(key=lambda r: (r["구분"], -r["구종가치"]))
    return {"rows": rows, "RE": RE, "LW": LW, "CV": CV, "year": year,
            "borrowed": borrowed_lw["year"] if frozen else None}


def save(year: int, res: dict):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = res["rows"]
    with open(OUT_DIR / f"구종가치_{year}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # 사이트에서 쓰기 좋은 형태: 선수별로 구종 묶기
    by_player = {}
    for r in rows:
        k = f'{r["구분"]}|{r["선수코드"]}'
        p = by_player.setdefault(k, {"season": year, "type": r["구분"], "pcode": r["선수코드"],
                                     "name": r["선수"], "team": r["팀"], "pitches": {}})
        p["pitches"][r["구종"]] = {
            "code": r["코드"], "n": r["투구수"], "usage": r["구사율"],
            "velo": r["평균구속"], "value": r["구종가치"], "per100": r["100구당"],
        }
    meta = {
        "run_expectancy": {f"{o}아웃_{b}": round(v, 3) for (o, b), v in sorted(res["RE"].items())},
        "linear_weights": {k: round(v, 3) for k, v in sorted(res["LW"].items())},
        "count_values": {f"{b}-{s}": round(v, 3) for (b, s), v in sorted(res["CV"].items())},
    }
    with open(OUT_DIR / f"pitch_values_{year}.json", "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "players": list(by_player.values())}, f, ensure_ascii=False, indent=1)


def print_check(year: int, res: dict):
    print(f"[{year}] 결과별 득점가치:",
          ", ".join(f"{k} {v:+.2f}" for k, v in sorted(res["LW"].items(), key=lambda x: -x[1])))
    print(f"[{year}] 카운트 가치:",
          ", ".join(f"{b}-{s} {v:+.3f}" for (b, s), v in sorted(res["CV"].items())))
    top = [r for r in res["rows"] if r["구분"] == "정규시즌" and r["투구수"] >= 300][:5]
    for r in top:
        print(f"   {r['선수']}({r['팀']}) {r['구종']} {r['투구수']}구  {r['구종가치']:+.1f}  (100구당 {r['100구당']:+.2f})")


def parse_years(text: str) -> list[int]:
    if "-" in text:
        a, b = text.split("-")
        return list(range(int(a), int(b) + 1))
    return [int(y) for y in text.split(",")]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2009-2026")
    args = ap.parse_args()
    print("선수 이름표 만드는 중...")
    build_name_map()
    results, frozen_years = {}, []
    for y in parse_years(args.years):
        res = compute_season(y)
        if res == "frozen":
            frozen_years.append(y)
        elif res:
            results[y] = res
    if frozen_years and not results:
        print("득점가치를 빌려올 정상 시즌(2016년 이후)이 없어요. 2016년 이후도 같이 계산해 주세요.")
    elif frozen_years:
        normal_years = list(results)  # 빌려줄 수 있는 건 정상 시즌뿐
        for y in frozen_years:
            near = min(normal_years, key=lambda r: abs(r - y))
            res = compute_season(y, {"LW": results[near]["LW"], "year": near})
            if res and res != "frozen":
                results[y] = res
    for y in sorted(results):
        save(y, results[y])
        print_check(y, results[y])
    print(f"완료: {OUT_DIR}")
