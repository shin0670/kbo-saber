"""KBO raw records -> per-season player stats with sabermetrics.

usage: python process.py kbo_raw.json out_dir

Input keys (from kbo_scraper.js):
  regular season : hb1Y hb2Y runY defY pb1Y pb2Y           (run/def from 2001)
  postseason     : phY_S ppY_S   S = 4 WC, 3 준PO, 5 PO, 7 KS
each = {"head": [...], "teams": [...], "rows": [[playerId, teamCode, name, teamName, ...cells]]}

Method notes (also shown on the site):
  * wOBA weights: linear weights from that season's KBO run environment
    (Tango/FanGraphs method), scaled so league wOBA == league OBP.
  * wRC+ : no park factor (KBO park data not public) -> league-relative only.
  * FIP (FanGraphs glossary): (13*HR + 3*(BB+HBP) - 2*K)/IP + constant; constant makes league FIP == league ERA.
  * WAR (fWAR-style): runs above average + positional adj, then a replacement
    level calibrated so the league total = teams*G*(0.5-0.294) wins,
    split 57% batters / 43% pitchers (same split FanGraphs uses).
    Defense value itself (UZR-type) is NOT included: KBO publishes no zone data.
    Before 2001 KBO has no fielding/baserunning tables -> batting WAR only.
"""
import json, sys, os
from collections import defaultdict, Counter

RAW, OUT = sys.argv[1], sys.argv[2]
raw = json.load(open(RAW, encoding="utf-8"))
meta_in = raw.pop("_meta", {})

SERIES_NAME = {"4": "와일드카드", "3": "준플레이오프", "5": "플레이오프", "7": "한국시리즈"}
POS_ADJ = {"포수": 12.5, "1루수": -12.5, "2루수": 2.5, "3루수": 2.5, "유격수": 7.5,
           "좌익수": -7.5, "중견수": 2.5, "우익수": -7.5}
DH_ADJ = -17.5  # per full season


def num(x):
    if x is None:
        return 0.0
    x = str(x).replace(",", "").strip()
    try:
        return float(x)
    except ValueError:
        return 0.0


def ip_outs(s):
    s = str(s).strip()
    outs = 0
    for p in s.split():
        if "/" in p:
            a, b = p.split("/")
            try:
                outs += round(3 * int(a) / int(b))
            except (ValueError, ZeroDivisionError):
                pass
        else:
            try:
                outs += int(p) * 3
            except ValueError:
                pass
    return outs


def load(key, dedupe=True):
    """-> list of dicts. Removes rows that KBO lists under 2 team filters with identical
    stats (pre-2019 traded players appear under both clubs with the same full-season line)."""
    t = raw.get(key)
    if not t or not t.get("head"):
        return []
    head = t["head"]
    seen, out = set(), []
    # display team name -> team code (most common pairing this table)
    pair = Counter((r[3], r[1]) for r in t["rows"])
    name2code = {}
    for (nm, code), c in sorted(pair.items(), key=lambda x: -x[1]):
        name2code.setdefault(nm, code)
    for r in t["rows"]:
        if not r[0]:
            continue
        sig = (r[0], r[3], tuple(r[4:]))
        if dedupe and sig in seen:
            continue
        seen.add(sig)
        d = dict(zip(head[3:], r[4:]))
        d.update(id=r[0], name=r[2], team=r[3], tm=name2code.get(r[3], r[1]))
        out.append(d)
    return out


def keyed(rows):
    m = {}
    for d in rows:
        m.setdefault((d["id"], d["tm"]), d)
    return m


def r_(v, n):
    return None if v is None else round(v, n)


seasons = sorted({int(k[3:7]) for k in raw if k.startswith("hb1")})

# ---------- park factors (2001+: game results with home/away) ----------
# raw["parkY"] = {teamCode: [homeG, runs in home games (both teams), awayG, runs in away games]}
PF_RATIO = {}
for k, v in raw.items():
    if k.startswith("park"):
        y = int(k[4:])
        for tm, (hg, hr, ag, ar) in v.items():
            if hg and ag and ar:
                PF_RATIO[(y, tm)] = (hr / hg) / (ar / ag)


def park_factor(y, tm):
    """FanGraphs-style basic park factor: up to 5 seasons of home/road run ratio, regressed,
    then halved (players play half their games at home)."""
    rs = [PF_RATIO[(yy, tm)] for yy in range(y, y - 5, -1) if (yy, tm) in PF_RATIO]
    if not rs:
        return 1.0
    ratio = sum(rs) / len(rs)
    ratio = 1 + (ratio - 1) * len(rs) / (len(rs) + 1)  # regress toward 1, less with more seasons
    return (ratio + 1) / 2


# fielding: run value of one play made above/below average, regression, season cap
RUN_PLAY = 0.75
FLD_REG = 0.5
FLD_CAP = 10.0
RUN_GDP = 0.45
FHIST = defaultdict(dict)  # (playerId, pos) -> {year: [range runs above avg, innings]}
HROWS, PROWS, PSH, PSP, LEAGUE = [], [], [], [], []
players = {}

for yr in seasons:
    y = str(yr)
    hb1, hb2 = keyed(load("hb1" + y)), keyed(load("hb2" + y))
    run = keyed(load("run" + y))
    dfn = load("def" + y)
    pb1, pb2 = keyed(load("pb1" + y)), keyed(load("pb2" + y))
    if not hb1 or not pb1:
        continue
    has_run, has_def = bool(run), bool(dfn)

    H = []
    for k, a in hb1.items():
        b, rn = hb2.get(k, {}), run.get(k, {})
        h = {"id": a["id"], "name": a["name"], "tm": a["tm"], "team": a["team"]}
        for f in ["G", "PA", "AB", "R", "H", "2B", "3B", "HR", "TB", "RBI", "SAC", "SF"]:
            h[f] = num(a.get(f))
        for f in ["BB", "IBB", "HBP", "SO", "GDP"]:
            h[f] = num(b.get(f))
        h["SB"], h["CS"] = num(rn.get("SB")), num(rn.get("CS"))
        H.append(h)

    # fielding innings per (id, tm, pos)
    fld = defaultdict(lambda: defaultdict(float))
    fldG = defaultdict(lambda: defaultdict(float))
    frows = []
    for d in dfn:
        pos = d.get("POS", "")
        inn = ip_outs(d.get("IP", "")) / 3
        fld[(d["id"], d["tm"])][pos] += inn
        fldG[(d["id"], d["tm"])][pos] += num(d.get("G"))
        frows.append({"id": d["id"], "tm": d["tm"], "pos": pos, "inn": inn, "PO": num(d.get("PO")), "A": num(d.get("A")),
                      "E": num(d.get("E")), "DP": num(d.get("DP")), "PB": num(d.get("PB")), "SB": num(d.get("SB")),
                      "CS": num(d.get("CS"))})

    GS = defaultdict(float)
    for d in dfn:
        if d.get("POS") == "투수":
            GS[(d["id"], d["tm"])] += num(d.get("GS"))
    Pt = []
    for k, a in pb1.items():
        b = pb2.get(k, {})
        p = {"id": a["id"], "name": a["name"], "tm": a["tm"], "team": a["team"], "outs": ip_outs(a.get("IP", ""))}
        for f in ["G", "W", "L", "SV", "HLD", "H", "HR", "BB", "HBP", "SO", "R", "ER"]:
            p[f] = num(a.get(f))
        for f in ["CG", "SHO", "QS", "BSV", "TBF", "NP", "IBB", "WP", "BK"]:
            p[f] = num(b.get(f))
        Pt.append(p)

    # ---------------- league environment ----------------
    S = lambda L, f: sum(x[f] for x in L)
    lg = {f: S(H, f) for f in ["PA", "AB", "R", "H", "2B", "3B", "HR", "SF", "BB", "IBB", "HBP", "SO", "SB", "CS"]}
    lg1B = lg["H"] - lg["2B"] - lg["3B"] - lg["HR"]
    p_outs, p_R, p_ER = S(Pt, "outs"), S(Pt, "R"), S(Pt, "ER")
    lgIP = p_outs / 3
    nteams = len({h["tm"] for h in H})
    G = max(h["G"] for h in H)
    RperOut = p_R / p_outs
    runBB = RperOut + 0.14
    run = {"BB": runBB, "HBP": runBB + 0.025, "1B": runBB + 0.155}
    run["2B"] = run["1B"] + 0.3
    run["3B"] = run["2B"] + 0.27
    run["HR"] = 1.4
    runSB, runCS = 0.2, -(2 * RperOut + 0.075)
    uBB = lg["BB"] - lg["IBB"]
    denom = lg["AB"] + uBB + lg["HBP"] + lg["SF"]
    rawW = {k: v + RperOut for k, v in run.items()}
    lg_raw = (rawW["BB"] * uBB + rawW["HBP"] * lg["HBP"] + rawW["1B"] * lg1B + rawW["2B"] * lg["2B"]
              + rawW["3B"] * lg["3B"] + rawW["HR"] * lg["HR"]) / denom
    lgOBP = (lg["H"] + lg["BB"] + lg["HBP"]) / (lg["AB"] + lg["BB"] + lg["HBP"] + lg["SF"])
    scale = lgOBP / lg_raw
    W = {k: v * scale for k, v in rawW.items()}
    lgwOBA, lgRPA = lgOBP, lg["R"] / lg["PA"]
    lgERA, lgRA9 = p_ER * 9 / lgIP, p_R * 9 / lgIP
    RPW = 9 * (p_R / lgIP) * 1.5 + 3
    lgwSB = (lg["SB"] * runSB + lg["CS"] * runCS) / (lg1B + uBB + lg["HBP"]) if has_run else 0
    cFIP = lgERA - (13 * S(Pt, "HR") + 3 * (S(Pt, "BB") + S(Pt, "HBP")) - 2 * S(Pt, "SO")) / lgIP
    totWAR = nteams * G * (0.5 - 0.294)

    # ---------------- fielding runs (estimated from official fielding totals) ----------------
    # range: plays made (PO+A; A only at 1B) vs league rate at the position, scaled by the team's
    # balls-in-play outs per inning (a strikeout-heavy staff leaves fewer plays to make).
    fruns = defaultdict(float)
    if has_def:
        team_bip = defaultdict(lambda: [0.0, 0.0])  # tm -> [outs - SO, innings]
        for p in Pt:
            team_bip[p["tm"]][0] += p["outs"] - p["SO"]
            team_bip[p["tm"]][1] += p["outs"] / 3
        lg_bip_rate = sum(v[0] for v in team_bip.values()) / max(1, sum(v[1] for v in team_bip.values()))
        tfac = {tm: (v[0] / v[1]) / lg_bip_rate if v[1] else 1.0 for tm, v in team_bip.items()}
        plays = lambda r: r["A"] if r["pos"] == "1루수" else r["PO"] + r["A"]
        # position-group context: a ground-ball staff gives infielders more chances, a fly-ball staff
        # gives outfielders more. Team's group plays per inning vs league, shrunk halfway to 1.
        GROUP = {"2루수": "IF", "3루수": "IF", "유격수": "IF", "1루수": "1B", "좌익수": "OF", "중견수": "OF", "우익수": "OF"}
        grp_t = defaultdict(float); grp_l = defaultdict(float)
        for r in frows:
            g_ = GROUP.get(r["pos"])
            if g_:
                grp_t[(r["tm"], g_)] += plays(r); grp_l[g_] += plays(r)
        lg_inn = sum(v[1] for v in team_bip.values()) / max(1, len(team_bip))
        def gfac(tm, g_):
            if not g_ or not team_bip.get(tm) or not grp_l[g_]:
                return 1.0
            t_rate = grp_t[(tm, g_)] / team_bip[tm][1]
            l_rate = grp_l[g_] / sum(v[1] for v in team_bip.values())
            return 1 + (t_rate / l_rate - 1) * 0.5
        lgpos = defaultdict(lambda: defaultdict(float))
        for r in frows:
            if r["pos"] in POS_ADJ:
                L_ = lgpos[r["pos"]]
                L_["inn"] += r["inn"]; L_["plays"] += plays(r); L_["DP"] += r["DP"]; L_["PB"] += r["PB"]
                L_["SB"] += r["SB"]; L_["CS"] += r["CS"]; L_["eff"] += r["inn"] * gfac(r["tm"], GROUP.get(r["pos"]))
        for r in frows:
            pos = r["pos"]
            if pos not in POS_ADJ or r["inn"] <= 0:
                continue
            L_ = lgpos[pos]
            exp = r["inn"] * gfac(r["tm"], GROUP.get(pos))
            diff = (plays(r) - L_["plays"] / L_["eff"] * exp) * RUN_PLAY if L_["eff"] else 0
            if pos in ("2루수", "유격수"):
                diff += (r["DP"] - L_["DP"] / L_["eff"] * exp) * 0.25
            h_ = FHIST[(r["id"], pos)].setdefault(yr, [0.0, 0.0])
            h_[0] += diff; h_[1] += r["inn"]
        for r in frows:
            pos = r["pos"]
            if pos not in POS_ADJ or r["inn"] <= 0:
                continue
            L_ = lgpos[pos]
            # one season of range data is mostly noise: use a 3-season weighted rate (3:2:1), then regress
            num_, den_ = 0.0, 0.0
            for yy, w in ((yr, 3), (yr - 1, 2), (yr - 2, 1)):
                if yy in FHIST[(r["id"], pos)]:
                    d_, i_ = FHIST[(r["id"], pos)][yy]
                    num_ += w * d_; den_ += w * i_
            runs = (num_ / den_) * r["inn"] * FLD_REG if den_ else 0.0
            if pos == "포수":
                att = r["SB"] + r["CS"]
                lg_cs = L_["CS"] / (L_["SB"] + L_["CS"]) if (L_["SB"] + L_["CS"]) else 0
                runs = (r["CS"] - att * lg_cs) * (runSB - runCS)          # caught stealing above average
                runs += (L_["PB"] / L_["inn"] * r["inn"] - r["PB"]) * 0.28  # fewer passed balls
            fruns[(r["id"], r["tm"])] += runs
        for k in fruns:
            fruns[k] = max(-FLD_CAP, min(FLD_CAP, fruns[k]))
    lg_bip_h = sum(h["AB"] - h["SO"] - h["HR"] for h in H)
    lg_gdp_rate = sum(h["GDP"] for h in H) / lg_bip_h if lg_bip_h else 0

    # ---------------- hitters ----------------
    for h in H:
        pa, ab = h["PA"], h["AB"]
        b1 = h["H"] - h["2B"] - h["3B"] - h["HR"]
        ubb = h["BB"] - h["IBB"]
        d = ab + ubb + h["HBP"] + h["SF"]
        od = ab + h["BB"] + h["HBP"] + h["SF"]
        h["b1"] = b1
        h["AVG"] = h["H"] / ab if ab else None
        h["OBP"] = (h["H"] + h["BB"] + h["HBP"]) / od if od else None
        h["SLG"] = h["TB"] / ab if ab else None
        h["OPS"] = h["OBP"] + h["SLG"] if ab else None
        h["ISO"] = h["SLG"] - h["AVG"] if ab else None
        bd = ab - h["SO"] - h["HR"] + h["SF"]
        h["BABIP"] = (h["H"] - h["HR"]) / bd if bd > 0 else None
        h["K%"] = h["SO"] / pa if pa else None
        h["BB%"] = h["BB"] / pa if pa else None
        ws = W["BB"] * ubb + W["HBP"] * h["HBP"] + W["1B"] * b1 + W["2B"] * h["2B"] + W["3B"] * h["3B"] + W["HR"] * h["HR"]
        h["wOBA"] = ws / d if d else None
        h["wRAA"] = (h["wOBA"] - lgwOBA) / scale * pa if d else 0.0
        h["PF"] = park_factor(yr, h["tm"])
        park_runs = (lgRPA - h["PF"] * lgRPA) * pa
        h["bat"] = h["wRAA"] + park_runs
        h["wRC+"] = (h["wRAA"] / pa + lgRPA + (lgRPA - h["PF"] * lgRPA)) / lgRPA * 100 if pa else None
        h["wGDP"] = (lg_gdp_rate * (ab - h["SO"] - h["HR"]) - h["GDP"]) * RUN_GDP if has_run else None
        h["Fld"] = fruns.get((h["id"], h["tm"]), 0.0) if has_def else None
        h["wSB"] = (h["SB"] * runSB + h["CS"] * runCS - lgwSB * (b1 + ubb + h["HBP"])) if has_run else None
        pos_runs, prim = None, None
        if has_def:
            fi = {p: inn for p, inn in fld.get((h["id"], h["tm"]), {}).items() if p in POS_ADJ}
            pos_runs = sum(POS_ADJ[p] * inn / (9 * G) for p, inn in fi.items())
            fg = sum(fldG.get((h["id"], h["tm"]), {}).get(p, 0) for p in fi)
            dh_g = max(0.0, h["G"] - fg)
            if pa and h["G"]:
                pos_runs += DH_ADJ * (pa * dh_g / h["G"]) / (G * 4.2)
            if fi:
                prim = max(fi, key=fi.get)
            elif pa:
                prim = "지명타자"
        h["Pos"], h["pos"] = pos_runs, prim

    # replacement level calibrated to league total
    base_runs = lambda h: h["bat"] + (h["wSB"] or 0) + (h["wGDP"] or 0) + (h["Pos"] or 0)
    above = sum(base_runs(h) + (h["Fld"] or 0) for h in H)
    repl_per_pa = (0.57 * totWAR * RPW - above) / lg["PA"]
    above_nd = sum(base_runs(h) for h in H)
    repl_nd = (0.57 * totWAR * RPW - above_nd) / lg["PA"]
    for h in H:
        h["WAR"] = (base_runs(h) + (h["Fld"] or 0) + repl_per_pa * h["PA"]) / RPW
        h["WARnd"] = (base_runs(h) + repl_nd * h["PA"]) / RPW

    # ---------------- pitchers ----------------
    for p in Pt:
        ip = p["outs"] / 3
        tbf = p["TBF"]
        p["ERA"] = p["ER"] * 9 / ip if ip else None
        p["WHIP"] = (p["H"] + p["BB"]) / ip if ip else None
        p["K/9"] = p["SO"] * 9 / ip if ip else None
        p["BB/9"] = p["BB"] * 9 / ip if ip else None
        p["HR/9"] = p["HR"] * 9 / ip if ip else None
        p["K%"] = p["SO"] / tbf if tbf else None
        p["BB%"] = p["BB"] / tbf if tbf else None
        p["K-BB%"] = (p["SO"] - p["BB"]) / tbf if tbf else None
        bip = tbf - p["SO"] - p["BB"] - p["HBP"] - p["HR"]
        p["BABIP"] = (p["H"] - p["HR"]) / bip if bip > 0 else None
        p["FIP"] = (13 * p["HR"] + 3 * (p["BB"] + p["HBP"]) - 2 * p["SO"]) / ip + cFIP if ip else None
        pf = park_factor(yr, p["tm"])
        p["PF"] = pf
        # FanGraphs minus stats: 100 = league average, lower is better, park-adjusted
        p["ERA+"] = lgERA * pf / p["ERA"] * 100 if p["ERA"] else None
        p["ERA-"] = 100 * (p["ERA"] + (p["ERA"] - p["ERA"] * pf)) / lgERA if p["ERA"] is not None else None
        p["FIP-"] = 100 * (p["FIP"] + (p["FIP"] - p["FIP"] * pf)) / lgERA if p["FIP"] is not None else None
        lob_d = p["H"] + p["BB"] + p["HBP"] - 1.4 * p["HR"]
        p["LOB%"] = (p["H"] + p["BB"] + p["HBP"] - p["R"]) / lob_d if lob_d > 0 else None
        gs = GS.get((p["id"], p["tm"]))
        if gs is None or not has_def:  # before 2001 KBO has no GS: estimate from innings per game
            ipg = ip / p["G"] if p["G"] else 0
            gs = p["G"] * max(0.0, min(1.0, (ipg - 1.5) / 4.0))
            p["GS"] = None
        else:
            p["GS"] = gs
        sp_share = gs / p["G"] if p["G"] else 0
        p["role"] = "선발" if sp_share >= 0.5 else "구원"
        # FanGraphs pitcher WAR: FIP -> runs (RA9 scale), park-adjust, dynamic runs-per-win,
        # replacement level .12 wins/9IP for starters and .03 for relievers.
        if ip:
            fipr9 = p["FIP"] + (lgRA9 - lgERA)
            pfipr9 = fipr9 / pf
            raap9 = lgRA9 - pfipr9
            ipg = ip / p["G"]
            drpw = (((18 - ipg) * lgRA9 + ipg * pfipr9) / 18 + 2) * 1.5
            repl = 0.03 * (1 - sp_share) + 0.12 * sp_share
            p["war0"] = (raap9 / drpw + repl) * ip / 9
        else:
            p["war0"] = 0.0
    # league correction so pitchers total 43% of the league's WAR (FanGraphs: 430 of 1000)
    corr = (0.43 * totWAR - sum(p["war0"] for p in Pt)) / lgIP
    for p in Pt:
        p["WAR"] = p["war0"] + corr * p["outs"] / 3

    LEAGUE.append({"y": yr, "teams": nteams, "G": int(G), "AVG": lg["H"] / lg["AB"], "OBP": lgOBP,
                   "SLG": (lg1B + 2 * lg["2B"] + 3 * lg["3B"] + 4 * lg["HR"]) / lg["AB"], "ERA": lgERA,
                   "RA9": lgRA9, "wOBA": lgwOBA, "wBB": W["BB"], "wHBP": W["HBP"], "w1B": W["1B"], "w2B": W["2B"],
                   "w3B": W["3B"], "wHR": W["HR"], "scale": scale, "RPA": lgRPA, "RPW": RPW, "cFIP": cFIP,
                   "hasRun": has_run, "hasDef": has_def, "hasPark": any(k[0] == yr for k in PF_RATIO),
                   "PF": {tm: round(park_factor(yr, tm), 3) for tm in sorted({h["tm"] for h in H})},
                   "hWAR": sum(h["WAR"] for h in H), "pWAR": sum(p["WAR"] for p in Pt)})

    for h in H:
        players[h["id"]] = h["name"]
        HROWS.append([yr, h["id"], h["tm"], h["team"]] + [int(h[f]) for f in
                     ["G", "PA", "AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "IBB", "HBP", "SO", "GDP", "SAC", "SF"]]
                     + ([int(h["SB"]), int(h["CS"])] if has_run else [None, None])
                     + [r_(h["AVG"], 3), r_(h["OBP"], 3), r_(h["SLG"], 3), r_(h["OPS"], 3), r_(h["ISO"], 3),
                        r_(h["BABIP"], 3), r_(h["K%"], 3), r_(h["BB%"], 3), r_(h["wOBA"], 3), r_(h["wRC+"], 0),
                        r_(h["wRAA"], 1), r_(h["wSB"], 1), r_(h["Pos"], 1), r_(h["WAR"], 2), h["pos"],
                        r_(h["wGDP"], 1), r_(h["Fld"], 1), r_(h["bat"] - h["wRAA"], 1), r_(h["WARnd"], 2)])
    for p in Pt:
        players[p["id"]] = p["name"]
        PROWS.append([yr, p["id"], p["tm"], p["team"]] + [int(p[f]) for f in
                     ["G", "W", "L", "SV", "HLD", "BSV", "QS", "CG", "SHO"]] + [p["outs"]]
                     + [int(round(p["GS"])) if p["GS"] is not None else None]
                     + [int(p[f]) for f in ["H", "HR", "BB", "IBB", "HBP", "SO", "R", "ER", "TBF", "NP"]]
                     + [r_(p["ERA"], 2), r_(p["WHIP"], 2), r_(p["K/9"], 2), r_(p["BB/9"], 2), r_(p["HR/9"], 2),
                        r_(p["K%"], 3), r_(p["BB%"], 3), r_(p["K-BB%"], 3), r_(p["BABIP"], 3), r_(p["FIP"], 2),
                        r_(p["ERA+"], 0), r_(p["FIP-"], 0), r_(p["WAR"], 2), p["role"],
                        r_(p["ERA-"], 0), r_(p["LOB%"], 3)])

    # ---------------- postseason (uses that year's regular-season weights) ----------------
    for s in ["4", "3", "5", "7"]:
        for d in load(f"ph{y}_{s}"):
            ab, pa = num(d.get("AB")), num(d.get("PA"))
            hh, d2, d3, hr = num(d.get("H")), num(d.get("2B")), num(d.get("3B")), num(d.get("HR"))
            bb, hbp, so = num(d.get("BB")), num(d.get("HBP")), num(d.get("SO"))
            b1 = hh - d2 - d3 - hr
            tb = b1 + 2 * d2 + 3 * d3 + 4 * hr
            od = ab + bb + hbp
            avg = hh / ab if ab else None
            obp = (hh + bb + hbp) / od if od else None
            slg = tb / ab if ab else None
            woba = (W["BB"] * bb + W["HBP"] * hbp + W["1B"] * b1 + W["2B"] * d2 + W["3B"] * d3 + W["HR"] * hr) / od if od else None
            players.setdefault(d["id"], d["name"])
            PSH.append([yr, int(s), d["id"], d["tm"], d["team"], int(num(d.get("G"))), int(pa), int(ab), int(hh),
                        int(d2), int(d3), int(hr), int(num(d.get("RBI"))), int(num(d.get("SB"))), int(num(d.get("CS"))),
                        int(bb), int(hbp), int(so), int(num(d.get("GDP"))), int(num(d.get("E"))),
                        r_(avg, 3), r_(obp, 3), r_(slg, 3), r_((obp or 0) + (slg or 0), 3) if ab else None,
                        r_(woba, 3), r_(so / pa, 3) if pa else None, r_(bb / pa, 3) if pa else None])
        for d in load(f"pp{y}_{s}"):
            outs = ip_outs(d.get("IP", ""))
            ip = outs / 3
            hh, hr, bb, hbp, so = (num(d.get(f)) for f in ["H", "HR", "BB", "HBP", "SO"])
            er, tbf = num(d.get("ER")), num(d.get("TBF"))
            players.setdefault(d["id"], d["name"])
            PSP.append([yr, int(s), d["id"], d["tm"], d["team"]] + [int(num(d.get(f))) for f in
                       ["G", "W", "L", "SV", "HLD", "CG", "SHO"]] + [outs, int(hh), int(hr), int(bb), int(hbp), int(so),
                       int(num(d.get("R"))), int(er), int(tbf),
                       r_(er * 9 / ip, 2) if ip else None, r_((hh + bb) / ip, 2) if ip else None,
                       r_(so * 9 / ip, 2) if ip else None,
                       r_((13 * hr + 3 * (bb + hbp) - 2 * so) / ip + cFIP, 2) if ip else None,  # same FanGraphs FIP
                       r_(so / tbf, 3) if tbf else None, r_(bb / tbf, 3) if tbf else None])

HCOLS = ["y", "id", "tm", "team", "G", "PA", "AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "IBB", "HBP", "SO", "GDP",
         "SAC", "SF", "SB", "CS", "AVG", "OBP", "SLG", "OPS", "ISO", "BABIP", "K%", "BB%", "wOBA", "wRC+", "wRAA", "wSB",
         "Pos", "WAR", "pos", "wGDP", "Fld", "Park", "WARnd"]
PCOLS = ["y", "id", "tm", "team", "G", "W", "L", "SV", "HLD", "BSV", "QS", "CG", "SHO", "outs", "GS", "H", "HR", "BB", "IBB",
         "HBP", "SO", "R", "ER", "TBF", "NP", "ERA", "WHIP", "K/9", "BB/9", "HR/9", "K%", "BB%", "K-BB%", "BABIP", "FIP",
         "ERA+", "FIP-", "WAR", "role", "ERA-", "LOB%"]
PSHCOLS = ["y", "s", "id", "tm", "team", "G", "PA", "AB", "H", "2B", "3B", "HR", "RBI", "SB", "CS", "BB", "HBP", "SO",
           "GDP", "E", "AVG", "OBP", "SLG", "OPS", "wOBA", "K%", "BB%"]
PSPCOLS = ["y", "s", "id", "tm", "team", "G", "W", "L", "SV", "HLD", "CG", "SHO", "outs", "H", "HR", "BB", "HBP", "SO",
           "R", "ER", "TBF", "ERA", "WHIP", "K/9", "FIP", "K%", "BB%"]

os.makedirs(OUT, exist_ok=True)
sep = (",", ":")
dump = lambda name, obj: json.dump(obj, open(f"{OUT}/{name}", "w", encoding="utf-8"), ensure_ascii=False, separators=sep)
dump("hitters.json", {"cols": HCOLS, "rows": HROWS})
dump("pitchers.json", {"cols": PCOLS, "rows": PROWS})
dump("post_hitters.json", {"cols": PSHCOLS, "rows": PSH})
dump("post_pitchers.json", {"cols": PSPCOLS, "rows": PSP})
dump("meta.json", {"players": players, "series": SERIES_NAME, "scrapedAt": meta_in.get("scrapedAt"),
                   "league": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in L.items()} for L in LEAGUE]})
print(f"seasons {len(LEAGUE)}  hitters {len(HROWS)}  pitchers {len(PROWS)}  post-h {len(PSH)}  post-p {len(PSP)}  players {len(players)}")
print("scrape errors:", meta_in.get("errors"))
