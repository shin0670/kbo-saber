"""
KBO 투구별 데이터 수집기 (네이버 문자중계)

사용법 (터미널):
  1) 테스트:   python3 collect_pitches.py --test
     → sample_game.json 하나만 만들어짐. 이 파일을 Claude에게 보내주면 됨.
  2) 본 수집:  caffeinate -i python3 collect_pitches.py --years 2024-2026
     → pitch_data/2024/경기ID.json.gz ... 형태로 저장
     (caffeinate -i : 수집 중 맥이 잠들지 않게 해줌)

중간에 꺼져도 다시 같은 명령을 실행하면 이미 받은 경기는 건너뛰고 이어서 받습니다.
"""
from __future__ import annotations

import argparse
import os
import gzip
import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://m.sports.naver.com/"}
SCHEDULE_URL = "https://api-gw.sports.naver.com/schedule/games"
GAME_URL = "https://api-gw.sports.naver.com/schedule/games/{game_id}"
RELAY_URL = "https://api-gw.sports.naver.com/schedule/games/{game_id}/relay"

BASE_DIR = Path(__file__).resolve().parent
# 저장 위치: 기본은 이 파일 옆 pitch_data 폴더, GitHub Actions에서는 PITCH_DATA_DIR 로 바꿈
OUT_DIR = Path(os.environ.get("PITCH_DATA_DIR") or BASE_DIR / "pitch_data")
DELAY = 0.3  # 요청 사이 쉬는 시간(초). 네이버 서버에 부담 안 주려고.
DROP_KEYS = {"currentPlayersInfo", "batterRecord"}

session = requests.Session()


def get_json(url, params=None, retries=3):
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, headers=HEADERS, params=params, timeout=20)
            r.raise_for_status()
            time.sleep(DELAY)
            return r.json()
        except Exception:
            if attempt == retries:
                raise
            time.sleep(2 * attempt)


# ---------- 일정 ----------
def games_on(day: date) -> list[dict]:
    params = {
        "fields": "basic,schedule,baseball",
        "upperCategoryId": "kbaseball",
        "fromDate": day.isoformat(),
        "toDate": day.isoformat(),
        "size": 500,
    }
    games = get_json(SCHEDULE_URL, params).get("result", {}).get("games", [])
    out = []
    for g in games:
        if g.get("categoryId") != "kbo" or g.get("statusCode") != "RESULT":
            continue
        out.append({
            "game_id": g.get("gameId"),
            "date": g.get("gameDate"),
            "round_code": g.get("roundCode"),
            "title": g.get("title"),
            "home": g.get("homeTeamName"),
            "away": g.get("awayTeamName"),
            "home_code": g.get("homeTeamCode"),
            "away_code": g.get("awayTeamCode"),
        })
    return out


def season_schedule(year: int) -> list[dict]:
    """한 시즌 경기 목록. 한 번 받으면 파일로 저장해두고 재사용."""
    path = OUT_DIR / str(year) / "_games.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    print(f"[{year}] 일정 받는 중 (3~11월)...")
    games, seen = [], set()
    day, end = date(year, 3, 1), date(year, 11, 30)
    if year == date.today().year:
        end = min(end, date.today() - timedelta(days=1))
    while day <= end:
        for g in games_on(day):
            if g["game_id"] not in seen:
                seen.add(g["game_id"])
                games.append(g)
        day += timedelta(days=1)

    path.parent.mkdir(parents=True, exist_ok=True)
    # 올해 시즌은 진행 중일 수 있으니 저장하지 않음 (매번 새로 받음)
    if year != date.today().year:
        path.write_text(json.dumps(games, ensure_ascii=False), encoding="utf-8")
    print(f"[{year}] 경기 {len(games)}개")
    return games


# ---------- 문자중계 ----------
def inning_count(game_id: str) -> int:
    try:
        g = get_json(GAME_URL.format(game_id=game_id))["result"]["game"]
        m = re.search(r"\d+", str(g.get("currentInning", "")))
        if m:
            return int(m.group())
    except Exception:
        pass
    return 0


def fetch_relays(game_id: str, raw: bool = False) -> list[dict]:
    """이닝별 문자중계를 받아서 반환.
    옛날 경기(2009~2012)는 이닝 수 정보가 '1'로 잘못 나와서 1회만 받아지는 문제가 있었음.
    → 이닝 정보와 상관없이 최소 9회까지는 다 받고, 연장은 빈 이닝이 나올 때까지 받음 (최대 15회).
    """
    n = inning_count(game_id)
    last = max(n, 9)
    blocks = []
    for inn in range(1, 16):
        if inn > last and blocks and not _has_relays(blocks[-1], raw):
            break
        data = get_json(RELAY_URL.format(game_id=game_id), {"inning": inn})
        block = data.get("result", {}).get("textRelayData")
        if not block or not block.get("textRelays"):
            if inn > last:
                break
            continue
        blocks.append(data if raw else block)
    return blocks


def _has_relays(b: dict, raw: bool) -> bool:
    block = b.get("result", {}).get("textRelayData") if raw else b
    return bool(block and block.get("textRelays"))


def innings_in_file(path: Path) -> int:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    return len({o.get("_inn") for o in data.get("options", [])})


def run_diagnose(game_id: str):
    """옛날 경기가 왜 1회만 받아지는지 확인용."""
    g = get_json(GAME_URL.format(game_id=game_id)).get("result", {}).get("game", {})
    print("currentInning:", repr(g.get("currentInning")), "/ statusCode:", g.get("statusCode"))
    for inn in range(1, 11):
        data = get_json(RELAY_URL.format(game_id=game_id), {"inning": inn})
        block = data.get("result", {}).get("textRelayData") or {}
        relays = block.get("textRelays", [])
        lines = sum(len(r.get("textOptions", [])) for r in relays)
        inns = sorted({r.get("inn") for r in relays})
        print(f"  inning={inn}: 묶음 {len(relays)}개, 줄 {lines}개, 들어있는 이닝 {inns}")


def compact(blocks: list[dict]) -> dict:
    """저장 용량을 줄이려고 필요한 부분만 남김 (투구 궤적 상세값은 버림)."""
    players = {}
    for b in blocks:
        for side in ("home", "away"):
            for key in (f"{side}Lineup", f"{side}Entry"):
                part = b.get(key) or {}
                if not isinstance(part, dict):
                    continue
                for group in ("pitcher", "batter"):
                    for p in part.get(group, []):
                        if p.get("pcode"):
                            players[str(p["pcode"])] = {
                                "name": p.get("name"),
                                "side": side,
                                "hitType": p.get("hitType"),
                            }

    options = []
    for b in blocks:
        for relay in b.get("textRelays", []):
            for opt in relay.get("textOptions", []):
                # 줄마다 붙어 있는 선수 시즌기록은 용량만 크고 계산에 안 쓰여서 버림
                o = {k: v for k, v in opt.items() if k not in DROP_KEYS}
                o["_inn"] = relay.get("inn")
                o["_half"] = relay.get("homeOrAway")
                options.append(o)
    # 이닝마다 따로 받다 보니 겹치는 줄이 생길 수 있어서 seqno로 중복 제거
    uniq = {}
    for o in options:
        uniq[(o.get("seqno"), o.get("_inn"), o.get("_half"), o.get("text"))] = o
    options = sorted(uniq.values(), key=lambda o: o.get("seqno") or 0)
    return {"players": players, "options": options}


# ---------- 실행 ----------
def run_test():
    print("테스트: 2025-06-10 첫 경기를 받아봅니다...")
    games = games_on(date(2025, 6, 10))
    if not games:
        print("경기를 못 찾았어요.")
        return
    g = games[0]
    raw = fetch_relays(g["game_id"], raw=True)
    out = BASE_DIR / "sample_game.json"
    out.write_text(json.dumps({"game": g, "raw": raw[:3]}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"완료: {out}  ({out.stat().st_size // 1024} KB)")
    print("이 파일을 Claude 대화창에 끌어다 놓아 주세요.")


def run_collect(years: list[int], redo_short: bool = False):
    for year in years:
        games = season_schedule(year)
        ydir = OUT_DIR / str(year)
        ydir.mkdir(parents=True, exist_ok=True)
        todo = []
        for g in games:
            path = ydir / f"{g['game_id']}.json.gz"
            if not path.exists():
                todo.append(g)
            elif redo_short and innings_in_file(path) < 5:
                todo.append(g)  # 1회만 받아진 옛날 파일은 다시 받기
        print(f"[{year}] 전체 {len(games)}경기 / 남은 {len(todo)}경기")
        for i, g in enumerate(todo, 1):
            try:
                blocks = fetch_relays(g["game_id"])
                data = {"game": g, **compact(blocks)}
                with gzip.open(ydir / f"{g['game_id']}.json.gz", "wt", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                print(f"  [{year}] {i}/{len(todo)} {g['game_id']} ok ({len(data['options'])}줄)")
            except Exception as e:
                print(f"  [{year}] {i}/{len(todo)} {g['game_id']} 실패: {e}")


def parse_years(text: str) -> list[int]:
    if "-" in text:
        a, b = text.split("-")
        return list(range(int(a), int(b) + 1))
    return [int(y) for y in text.split(",")]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="경기 1개만 받아 sample_game.json 생성")
    ap.add_argument("--years", default="2009-2026", help="예: 2024-2026 또는 2015,2020")
    ap.add_argument("--redo-short", action="store_true", help="1회만 받아진 파일을 다시 받기")
    ap.add_argument("--diagnose", metavar="경기ID", help="경기 하나의 이닝별 중계 상태 확인")
    args = ap.parse_args()
    if args.diagnose:
        run_diagnose(args.diagnose)
    elif args.test:
        run_test()
    else:
        run_collect(parse_years(args.years), redo_short=args.redo_short)
