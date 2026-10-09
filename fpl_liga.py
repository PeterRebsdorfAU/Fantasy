#!/usr/bin/env python3
"""
FPL Draft Liga Dashboard
========================
Henter data fra en Fantasy Premier League DRAFT-liga (draft.premierleague.com)
og viser stilling, win/loss streaks, pointtavle, priser, draft-røverier,
waiver-fund og andre sjove statistikker i browseren.

Kræver kun Python 3.8+ (ingen ekstra pakker).

Brug:
    python3 fpl_liga.py                         # start dashboard (spørger efter liga i browseren)
    python3 fpl_liga.py "<link fra draft-siden>"   # start direkte med jeres liga
    python3 fpl_liga.py "<link>" --export rapport.html   # gem en statisk HTML-rapport
"""

import argparse
import json
import os
import re
import socket
import ssl
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = "https://draft.premierleague.com/api/"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
CONFIG_FILE = os.path.join(HERE, "config.json")
DASHBOARD = os.path.join(HERE, "dashboard.html")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X) FPL-Draft-Dashboard/1.0"
WORKERS = 8
MEMORY_TTL = 300  # sekunder et beregnet resultat genbruges i dashboardet


class NotFound(Exception):
    pass


class UserError(Exception):
    """Fejl der vises direkte for brugeren."""


# --------------------------------------------------------------------------- #
# Hentning af data
# --------------------------------------------------------------------------- #

_ssl_ctx = None


def ssl_context():
    """Python fra python.org på Mac mangler ofte rodcertifikater – brug certifi eller Macens egne."""
    global _ssl_ctx
    if _ssl_ctx is None:
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except ImportError:
            if sys.platform == "darwin":
                try:
                    pem = subprocess.run(
                        ["security", "find-certificate", "-a", "-p",
                         "/System/Library/Keychains/SystemRootCertificates.keychain"],
                        capture_output=True, text=True, timeout=30).stdout
                    if pem:
                        ctx.load_verify_locations(cadata=pem)
                except (OSError, subprocess.SubprocessError, ssl.SSLError):
                    pass
        _ssl_ctx = ctx
    return _ssl_ctx


def fetch(path, cache=False):
    """Hent JSON fra Draft-API'et. cache=True gemmer svaret på disk (til data der ikke ændrer sig)."""
    cfile = os.path.join(CACHE_DIR, re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_") + ".json")
    if cache and os.path.exists(cfile):
        try:
            with open(cfile, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass

    last_err = None
    for attempt in range(5):
        try:
            req = urllib.request.Request(BASE + path, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30, context=ssl_context()) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(path)
            last_err = e
            if e.code == 503:
                last_err = UserError("FPL er ved at opdatere spillet lige nu. Prøv igen om lidt.")
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            last_err = e
        time.sleep(1.5 * (attempt + 1))
    else:
        if isinstance(last_err, UserError):
            raise last_err
        raise UserError(f"Kunne ikke hente data fra FPL Draft ({path}): {last_err}")

    if cache:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = cfile + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, cfile)
    return data


def fetch_or_none(path, cache=False):
    try:
        return fetch(path, cache)
    except NotFound:
        return None


def league_from_entry(entry_id):
    pub = fetch_or_none(f"entry/{entry_id}/public")
    if not pub or not pub.get("entry", {}).get("league_set"):
        raise UserError(f"Fandt ikke et draft-hold med ID {entry_id}, eller holdet er ikke med i en liga.")
    leagues = pub["entry"]["league_set"]
    details = fetch(f"league/{leagues[0]}/details")
    others = []
    for lid in leagues[1:]:
        d = fetch_or_none(f"league/{lid}/details")
        if d:
            others.append({"id": lid, "name": d["league"]["name"]})
    return leagues[0], details, others


def resolve(raw):
    """Accepterer et link fra draft-siden (…/entry/123/…, …/league/123/…) eller et liga-ID."""
    raw = (raw or "").strip()
    m = re.search(r"/entry/(\d+)", raw)
    if m:
        return league_from_entry(int(m.group(1)))
    m = re.search(r"/league/(\d+)", raw) or re.fullmatch(r"(?:liga:)?(\d+)", raw)
    if m:
        lid = int(m.group(1))
        details = fetch_or_none(f"league/{lid}/details")
        if details:
            return lid, details, []
        if raw.isdigit():
            return league_from_entry(lid)  # måske et holds-ID
        raise UserError(f"Fandt ingen draft-liga med ID {lid}.")
    raise UserError("Indsæt linket fra draft.premierleague.com (fx fra 'Points'-siden) eller et liga-ID.")


# --------------------------------------------------------------------------- #
# Beregninger
# --------------------------------------------------------------------------- #

def now_dk():
    """Dansk tid – også når siden bygges på en server i UTC (fx Render)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Copenhagen"))
    except Exception:  # noqa: BLE001 – ældre Python eller manglende tidszonedata
        return datetime.now()


def comp_rank(values):
    """Konkurrence-rangering (1,2,2,4) for en dict {key: værdi}, højeste værdi = 1."""
    ordered = sorted(values.items(), key=lambda kv: kv[1], reverse=True)
    ranks, prev, prev_rank = {}, object(), 0
    for idx, (k, v) in enumerate(ordered, start=1):
        if v != prev:
            prev_rank, prev = idx, v
        ranks[k] = prev_rank
    return ranks


def streak_stats(seq):
    """seq: liste af (gw, 'W'/'D'/'L'). Returnerer længste og nuværende streaks."""
    tests = {
        "win": lambda r: r == "W",
        "loss": lambda r: r == "L",
        "unbeaten": lambda r: r != "L",
        "winless": lambda r: r != "W",
    }
    best = {k: {"len": 0, "from": None, "to": None} for k in tests}
    run = {k: 0 for k in tests}
    for gw, r in seq:
        for k, ok in tests.items():
            run[k] = run[k] + 1 if ok(r) else 0
            if run[k] > best[k]["len"]:
                best[k] = {"len": run[k], "from": None, "to": gw}
    gws = [g for g, _ in seq]
    for k in best:
        if best[k]["len"]:
            end_idx = gws.index(best[k]["to"])
            best[k]["from"] = gws[end_idx - best[k]["len"] + 1]
    current = {"type": None, "len": 0}
    if seq:
        last = seq[-1][1]
        n = 0
        for _, r in reversed(seq):
            if r != last:
                break
            n += 1
        current = {"type": last, "len": n}
    return best, current


def build(lid, details, other_leagues=(), log=print):
    boot = fetch("bootstrap-static")
    cur = boot["events"].get("current")
    if not cur:
        raise UserError("Sæsonen er ikke gået i gang endnu – der er ingen runder at vise.")
    events = {e["id"]: e for e in boot["events"]["data"]}
    elements = {p["id"]: p for p in boot["elements"]}
    teams = {t["id"]: t["short_name"] for t in boot["teams"]}

    league = details["league"]
    kind = "h2h" if league.get("scoring") == "h" else "classic"
    entries = [le for le in details.get("league_entries", []) if le.get("entry_id")]
    if not entries:
        raise UserError("Ligaen har ingen hold endnu.")
    le2e = {le["id"]: le["entry_id"] for le in entries}
    standings = {le2e.get(s["league_entry"]): s for s in details.get("standings", [])}
    log(f"  Liga: {league['name']} ({kind}, {len(entries)} hold)")

    start = league.get("start_event") or 1
    if start > cur:
        raise UserError(f"Ligaen starter først i GW {start}.")
    gws = list(range(start, cur + 1))
    done = [g for g in gws if events[g]["finished"]]
    final = lambda g: events[g]["finished"] and g < cur  # data der ikke ændrer sig mere  # noqa: E731
    ids = [le["entry_id"] for le in entries]

    with ThreadPoolExecutor(WORKERS) as ex:
        log("  Henter holdhistorik ...")
        hists = dict(zip(ids, ex.map(lambda i: fetch(f"entry/{i}/history"), ids)))
        played = {i: {x["event"] for x in hists[i].get("history", [])} for i in ids}
        jobs = [(i, g) for i in ids for g in gws if g in played[i]]
        log(f"  Henter holdudtagelser ({len(jobs)} stk, gemmes til næste gang) ...")
        picks = dict(zip(jobs, ex.map(lambda ig: fetch_or_none(f"entry/{ig[0]}/event/{ig[1]}", cache=final(ig[1])), jobs)))
        log("  Henter spillerpoint, transaktioner og draft ...")
        lives = dict(zip(gws, ex.map(lambda g: fetch(f"event/{g}/live", cache=final(g)), gws)))
        f_trans = ex.submit(fetch_or_none, f"draft/league/{lid}/transactions")
        f_choices = ex.submit(fetch_or_none, f"draft/{lid}/choices")
        f_status = ex.submit(fetch_or_none, f"league/{lid}/element-status")
        transactions = [t for t in ((f_trans.result() or {}).get("transactions") or []) if t.get("result") == "a"]
        choices = (f_choices.result() or {}).get("choices") or []
        el_status = (f_status.result() or {}).get("element_status") or []

    live_pts = {g: {int(k): v["stats"]["total_points"] for k, v in lives[g]["elements"].items()} for g in gws}
    owner_of = {s["element"]: s["owner"] for s in el_status}

    def pname(e):
        p = elements.get(e)
        return p["web_name"] if p else f"#{e}"

    def pteam(e):
        p = elements.get(e)
        return teams.get(p["team"], "") if p else ""

    def season_pts(e):
        return (elements.get(e) or {}).get("total_points") or 0

    def player(e, pts, **extra):
        return {"name": pname(e), "team": pteam(e), "pts": pts, **extra}

    # ---- grunddata pr. hold ----
    M = {}
    for le in entries:
        i = le["entry_id"]
        hist = {x["event"]: x for x in hists[i].get("history", [])}
        net = {g: hist[g]["points"] for g in gws if g in hist}
        s = standings.get(i, {})
        m = {
            "id": i,
            "name": f"{le.get('player_first_name', '')} {le.get('player_last_name', '')}".strip() or le["entry_name"],
            "team": le["entry_name"],
            "rank": s.get("rank"),
            "last_rank": s.get("last_rank"),
            "total": s.get("total") if kind == "classic" else sum(net.values()),
            "gw_points": net.get(cur),
            "_net": net,
            "net": [net.get(g) for g in gws],
            "results": {},
        }
        done_scores = [net[g] for g in done if g in net]
        m["avg"] = round(statistics.mean(done_scores), 1) if done_scores else None
        m["stdev"] = round(statistics.pstdev(done_scores), 1) if len(done_scores) >= 3 else None

        # startellever, bænk og spillerbidrag ud fra holdudtagelserne
        contrib, bench_by_gw, bench_players, xi_by_gw, top_by_gw = {}, {}, {}, {}, []
        for g in gws:
            p = picks.get((i, g))
            if not p or not p.get("picks"):
                continue
            order = sorted(p["picks"], key=lambda x: x["position"])
            xi = [x["element"] for x in order if x["position"] <= 11]
            bench = [x["element"] for x in order if x["position"] > 11]
            for sub in p.get("subs") or []:  # normalt allerede indregnet, men for en sikkerheds skyld
                if sub["element_in"] in bench and sub["element_out"] in xi:
                    xi[xi.index(sub["element_out"])] = sub["element_in"]
                    bench[bench.index(sub["element_in"])] = sub["element_out"]
            lp = live_pts[g]
            bench_by_gw[g] = sum(lp.get(e, 0) for e in bench)
            bench_players[g] = [{"name": pname(e), "pts": lp.get(e, 0)} for e in bench]
            xi_by_gw[g] = set(xi)
            for e in xi:
                contrib[e] = contrib.get(e, 0) + lp.get(e, 0)
            best = max(xi, key=lambda e: lp.get(e, 0))
            top_by_gw.append({"gw": g, "name": pname(best), "pts": lp.get(best, 0)})
        m["_xi"] = xi_by_gw
        m["bench_total"] = sum(bench_by_gw.values())
        m["bench"] = [bench_by_gw.get(g) for g in gws]
        m["bench_players"] = [bench_players.get(g) for g in gws]
        bb = max(((v, g) for g, v in bench_by_gw.items() if g in done), default=None)
        m["bench_best"] = {"pts": bb[0], "gw": bb[1]} if bb else None
        top = sorted(contrib.items(), key=lambda kv: -kv[1])[:3]
        m["top_players"] = [player(e, v) for e, v in top]
        m["mvp"] = m["top_players"][0] if top else None
        m["top_by_gw"] = top_by_gw

        # nuværende trup (fra ligaens ejerskab)
        squad = [e for e, o in owner_of.items() if o == i]
        m["squad_pts"] = sum(season_pts(e) for e in squad)
        m["squad_best"] = player(max(squad, key=season_pts), season_pts(max(squad, key=season_pts))) if squad else None
        if kind == "h2h":
            m["h2h"] = {"w": 0, "d": 0, "l": 0, "pts": s.get("total", 0), "pf": 0, "pa": 0}
        M[i] = m

    # ---- transaktioner (waivers / free agents) ----
    all_pickups, all_drops = [], []
    for m in M.values():
        m["transactions"] = {"total": 0, "waivers": 0, "free_agents": 0, "best_in": None}
    for t in transactions:
        m = M.get(t["entry"])
        if not m:
            continue
        tr = m["transactions"]
        tr["total"] += 1
        tr["waivers" if t.get("kind") == "w" else "free_agents"] += 1
        ev = t["event"]
        later = [g for g in gws if g >= ev]
        gained = sum(live_pts[g].get(t["element_in"], 0) for g in later if t["element_in"] in m["_xi"].get(g, ()))
        lost = sum(live_pts[g].get(t["element_out"], 0) for g in later)
        pk = player(t["element_in"], gained, gw=ev, owner=m["name"], out=pname(t["element_out"]),
                    kind="Waiver" if t.get("kind") == "w" else "Free agent")
        all_pickups.append((m, pk))
        all_drops.append((m, player(t["element_out"], lost, gw=ev, owner=m["name"], to=pname(t["element_in"]))))
        if not tr["best_in"] or gained > tr["best_in"]["pts"]:
            tr["best_in"] = pk

    # ---- draften ----
    max_round = max((c["round"] for c in choices), default=0)
    draft_board = []
    for c in choices:
        m = M.get(c["entry"])
        if not m:
            continue
        pick = player(c["element"], season_pts(c["element"]), round=c["round"], pick=c["index"], owner=m["name"],
                      kept=owner_of.get(c["element"]) == c["entry"])
        draft_board.append((m, pick))
        m.setdefault("draft_picks", []).append(pick)
    for m in M.values():
        dp = m.get("draft_picks", [])
        early = [p for p in dp if p["round"] <= 3]
        m["draft"] = {
            "first": dp[0] if dp else None,
            "best": max(dp, key=lambda p: p["pts"]) if dp else None,
            "worst_early": min(early, key=lambda p: p["pts"]) if early else None,
            "score": sum(p["pts"] for p in dp),
            "kept": sum(1 for p in dp if p["kept"]),
            "count": len(dp),
        }

    # ---- H2H-kampe ----
    games = []
    for mt in details.get("matches", []):
        a, b = le2e.get(mt["league_entry_1"]), le2e.get(mt["league_entry_2"])
        games.append({"gw": mt["event"], "a": a, "b": b, "pa": mt["league_entry_1_points"],
                      "pb": mt["league_entry_2_points"], "finished": mt.get("finished"), "started": mt.get("started")})

    # ---- resultater pr. runde ----
    gw_summary = []
    for g in done:
        scores = {i: m["_net"][g] for i, m in M.items() if g in m["_net"]}
        if not scores:
            continue
        med = statistics.median(scores.values())
        hi, lo = max(scores.values()), min(scores.values())
        gw_summary.append({
            "gw": g, "median": med, "avg": round(statistics.mean(scores.values()), 1), "top": hi, "low": lo,
            "winners": [M[i]["name"] for i, s in scores.items() if s == hi],
            "losers": [M[i]["name"] for i, s in scores.items() if s == lo] if len(scores) > 1 else [],
            "scores": scores,
        })
        for i, s in scores.items():
            m = M[i]
            if s == hi:
                m.setdefault("gw_wins", []).append(g)
            if s == lo and len(scores) > 1:
                m.setdefault("gw_last", []).append(g)
            ap = m.setdefault("allplay", {"w": 0, "d": 0, "l": 0})
            for j, t in scores.items():
                if j != i:
                    ap["w" if s > t else "l" if s < t else "d"] += 1
            if kind == "classic":
                m["results"][g] = "W" if s > med else "L" if s < med else "D"

    if kind == "h2h":
        for gm in games:
            if gm["gw"] not in done or not gm["finished"]:
                continue
            for me, opp, mine, theirs in ((gm["a"], gm["b"], gm["pa"], gm["pb"]), (gm["b"], gm["a"], gm["pb"], gm["pa"])):
                if me not in M:
                    continue
                res = "W" if mine > theirs else "L" if mine < theirs else "D"
                m = M[me]
                m["results"][gm["gw"]] = res
                m["h2h"]["wdl"[{"W": 0, "D": 1, "L": 2}[res]]] += 1
                m["h2h"]["pf"] += mine
                m["h2h"]["pa"] += theirs
                m.setdefault("matches", []).append({"gw": gm["gw"], "res": res, "pf": mine, "pa": theirs,
                                                    "opp": M[opp]["name"] if opp in M else "Gennemsnit",
                                                    "opp_id": opp})

    # ---- afledte stats ----
    summ_by_gw = {s["gw"]: s for s in gw_summary}
    for m in M.values():
        seq = [(g, m["results"][g]) for g in sorted(m["results"])]
        m["streaks"], m["current_streak"] = streak_stats(seq)
        m["form"] = [r for _, r in seq[-5:]]
        m["gw_wins"] = m.get("gw_wins", [])
        m["gw_last"] = m.get("gw_last", [])
        ap = m.get("allplay", {"w": 0, "d": 0, "l": 0})
        n = ap["w"] + ap["d"] + ap["l"]
        ap["pct"] = round(100 * (ap["w"] + 0.5 * ap["d"]) / n, 1) if n else None
        m["allplay"] = ap
        if kind == "h2h":
            exp, act, robbed, unlucky = 0.0, 0.0, 0, 0
            for mt in m.get("matches", []):
                s = summ_by_gw.get(mt["gw"])
                if not s:
                    continue
                others = [v for k, v in s["scores"].items() if k != m["id"]]
                if others:
                    exp += sum(1 if mt["pf"] > o else 0.5 if mt["pf"] == o else 0 for o in others) / len(others)
                act += {"W": 1, "D": 0.5, "L": 0}[mt["res"]]
                robbed += mt["res"] == "W" and mt["pf"] < s["median"]
                unlucky += mt["res"] == "L" and mt["pf"] > s["median"]
            m["luck"] = round(act - exp, 2)
            m["expected_wins"] = round(exp, 1)
            m["robberies"] = robbed
            m["unlucky_losses"] = unlucky

    # ---- placering over tid ----
    rank_gws = gws if kind == "classic" else [g for g in done if any(g in m["results"] for m in M.values())]
    cum = {i: 0 for i in M}
    cum_pf = {i: 0 for i in M}
    for m in M.values():
        m["rank_hist"], m["cum"] = [], []
    for g in rank_gws:
        for i, m in M.items():
            if kind == "classic":
                cum[i] += m["_net"].get(g, 0)
            else:
                cum[i] += {"W": 3, "D": 1, "L": 0}.get(m["results"].get(g), 0)
                cum_pf[i] += m["_net"].get(g, 0)
        ranks = comp_rank({i: (cum[i], cum_pf[i]) for i in M})
        for i, m in M.items():
            m["rank_hist"].append(ranks[i])
            m["cum"].append(cum[i])
    final_rank = comp_rank({i: (cum[i], cum_pf[i]) for i in M}) if rank_gws else {i: 1 for i in M}
    for i, m in M.items():
        m["rank"] = m["rank"] or final_rank[i]
        m["last_rank"] = m["last_rank"] or (m["rank_hist"][-2] if len(m["rank_hist"]) > 1 else m["rank"])
        moves = [(m["rank_hist"][k - 1] - m["rank_hist"][k], rank_gws[k]) for k in range(1, len(rank_gws))]
        m["best_climb"] = max(moves) if moves else None
        m["worst_drop"] = min(moves) if moves else None
        m["vs_league_avg"] = round(sum(m["_net"][g] - summ_by_gw[g]["avg"] for g in summ_by_gw if g in m["_net"]), 1)

    # ---- kampprogram: denne runde (hvis i gang) ellers næste ----
    fix_gw = cur if not events[cur]["finished"] else cur + 1
    fixtures = []
    if kind == "h2h":
        for gm in games:
            if gm["gw"] != fix_gw or gm["a"] not in M or gm["b"] not in M:
                continue
            a, b = M[gm["a"]], M[gm["b"]]
            prev = [x for x in a.get("matches", []) if x["opp_id"] == b["id"]]
            fixtures.append({
                "a": {"id": a["id"], "name": a["name"], "team": a["team"], "rank": a["rank"], "form": a["form"],
                      "pts": gm["pa"] if gm["started"] else None},
                "b": {"id": b["id"], "name": b["name"], "team": b["team"], "rank": b["rank"], "form": b["form"],
                      "pts": gm["pb"] if gm["started"] else None},
                "prev": [{"gw": x["gw"], "pa": x["pf"], "pb": x["pa"]} for x in prev],
                "live": bool(gm["started"]),
            })

    # ---- ligaoversigter ----
    late = [p for _, p in draft_board if p["round"] > max_round / 2]
    early = [p for _, p in draft_board if p["round"] <= 2]
    free_agents = sorted((e for e, o in owner_of.items() if o is None), key=season_pts, reverse=True)[:8]
    draft_info = {
        "rounds": max_round,
        "steals": sorted(late, key=lambda p: -p["pts"])[:6],
        "busts": sorted(early, key=lambda p: p["pts"])[:6],
        "pickups": sorted((p for _, p in all_pickups), key=lambda p: -p["pts"])[:6],
        "got_away": sorted((p for _, p in all_drops), key=lambda p: -p["pts"])[:6],
        "free_agents": [player(e, season_pts(e)) for e in free_agents],
        "mvps": sorted(({**m["mvp"], "owner": m["name"]} for m in M.values() if m["mvp"]), key=lambda p: -p["pts"]),
    }

    managers = sorted(M.values(), key=lambda m: (m["rank"], m["name"]))
    awards = make_awards(managers, kind, draft_info, gw_summary)
    for m in managers:
        for k in ("_net", "_xi"):
            m.pop(k, None)
        m["results"] = {str(g): r for g, r in m["results"].items()}
    for s in gw_summary:
        s.pop("scores", None)

    return {
        "league": {"id": lid, "name": league["name"], "type": kind, "start_event": start, "size": len(managers)},
        "other_leagues": list(other_leagues),
        "status": {"current_gw": cur, "current_finished": events[cur]["finished"],
                   "last_finished": done[-1] if done else None,
                   "updated": now_dk().strftime("%d-%m-%Y %H:%M")},
        "gws": gws,
        "done": done,
        "rank_gws": rank_gws,
        "managers": managers,
        "gw_summary": gw_summary,
        "awards": awards,
        "draft": draft_info,
        "fixtures": {"gw": fix_gw, "list": fixtures},
    }


def make_awards(ms, kind, dr, gw_summary):
    awards = []

    def add(icon, title, metric, best=max, fmt=str, note=None, min_val=None, max_val=None):
        vals = []
        for m in ms:
            try:
                v = metric(m)
            except (KeyError, TypeError):
                v = None
            if v is not None:
                vals.append((m, v))
        if not vals:
            return
        target = best(v for _, v in vals)
        if (min_val is not None and target < min_val) or (max_val is not None and target > max_val):
            return
        winners = [m for m, v in vals if v == target]
        awards.append({
            "icon": icon, "title": title,
            "who": ", ".join(m["name"] for m in winners[:3]) + (f" +{len(winners) - 3}" if len(winners) > 3 else ""),
            "team": winners[0]["team"] if len(winners) == 1 else "",
            "value": fmt(target),
            "note": note(winners[0]) if note and len(winners) == 1 else "",
        })

    def add_item(icon, title, item, value, note=""):
        if item:
            awards.append({"icon": icon, "title": title, "who": item["owner"], "team": "", "value": value, "note": note})

    def best_gw(fn):
        """Højeste/laveste enkeltrunde: (point, gw, navne)."""
        rows = [(s["top"] if fn is max else s["low"], s["gw"], s["winners"] if fn is max else s["losers"]) for s in gw_summary]
        rows = [r for r in rows if r[2]]
        return fn(rows, key=lambda r: r[0]) if rows else None

    add("👑", "Rundens konge", lambda m: len(m["gw_wins"]), fmt=lambda v: f"{v}× rundevinder",
        note=lambda m: "GW " + ", ".join(map(str, m["gw_wins"])), min_val=1)
    add("🥄", "Træskeen", lambda m: len(m["gw_last"]), fmt=lambda v: f"{v}× sidst i runden",
        note=lambda m: "GW " + ", ".join(map(str, m["gw_last"])), min_val=1)
    add("🔥", "Varmeste streak", lambda m: m["streaks"]["win"]["len"], fmt=lambda v: f"{v} sejre i træk",
        note=lambda m: f"GW {m['streaks']['win']['from']}–{m['streaks']['win']['to']}", min_val=2)
    add("🌧️", "Den sorte serie", lambda m: m["streaks"]["loss"]["len"], fmt=lambda v: f"{v} nederlag i træk",
        note=lambda m: f"GW {m['streaks']['loss']['from']}–{m['streaks']['loss']['to']}", min_val=2)
    hi = best_gw(max)
    if hi:
        awards.append({"icon": "🚀", "title": "Rekordrunden", "who": ", ".join(hi[2]), "team": "",
                       "value": f"{hi[0]} point", "note": f"GW {hi[1]}"})
    lo = best_gw(min)
    if lo:
        awards.append({"icon": "🧊", "title": "Mareridtsrunden", "who": ", ".join(lo[2]), "team": "",
                       "value": f"{lo[0]} point", "note": f"GW {lo[1]}"})
    mvp = dr["mvps"][0] if dr["mvps"] else None
    add_item("⭐", "Ligaens MVP", mvp, f"{mvp['name']}: {mvp['pts']} point" if mvp else "",
             f"{mvp['team']} · point i startelleveren" if mvp else "")
    st = dr["steals"][0] if dr["steals"] else None
    add_item("🕵️", "Draft-røveriet", st, f"{st['name']}: {st['pts']} point" if st else "",
             f"Taget i runde {st['round']} (pick #{st['pick']})" if st else "")
    bu = dr["busts"][0] if dr["busts"] else None
    add_item("💩", "Draft-fiaskoen", bu, f"{bu['name']}: {bu['pts']} point" if bu else "",
             f"Taget i runde {bu['round']} (pick #{bu['pick']})" if bu else "")
    pu = dr["pickups"][0] if dr["pickups"] and dr["pickups"][0]["pts"] > 0 else None
    add_item("🎣", "Årets waiver-fund", pu, f"{pu['name']}: {pu['pts']} point" if pu else "",
             f"{pu['kind']} i GW {pu['gw']} (ud: {pu['out']})" if pu else "")
    ga = dr["got_away"][0] if dr["got_away"] and dr["got_away"][0]["pts"] > 0 else None
    add_item("🎈", "Den der slap væk", ga, f"{ga['name']}: {ga['pts']} point efter" if ga else "",
             f"Smidt ud i GW {ga['gw']} for {ga['to']}" if ga else "")
    add("🔁", "Waiver-galningen", lambda m: m["transactions"]["total"], fmt=lambda v: f"{v} transaktioner", min_val=1)
    add("🧘", "Zen-mesteren", lambda m: m["transactions"]["total"], best=min, fmt=lambda v: f"Kun {v} transaktioner")
    add("🪑", "Bænkekongen", lambda m: m["bench_total"], fmt=lambda v: f"{v} point på bænken")
    add("😩", "Bænke-katastrofen", lambda m: m["bench_best"]["pts"], fmt=lambda v: f"{v} bænkpoint i én runde",
        note=lambda m: f"GW {m['bench_best']['gw']}")
    add("💪", "Stærkeste trup", lambda m: m["squad_pts"], fmt=lambda v: f"{v} sæsonpoint i truppen",
        note=lambda m: f"Bedste: {m['squad_best']['name']} ({m['squad_best']['pts']}p)")
    add("🎓", "Draft-geniet", lambda m: m["draft"]["score"] or None, fmt=lambda v: f"{v} point fra egne draft-valg",
        note=lambda m: f"{m['draft']['kept']} af {m['draft']['count']} draft-valg er stadig på holdet")
    add("📏", "Mr. Stabil", lambda m: m["stdev"], best=min, fmt=lambda v: f"±{v} point pr. runde",
        note=lambda m: f"Snit {m['avg']} point")
    add("🎢", "Rutsjebanen", lambda m: m["stdev"], fmt=lambda v: f"±{v} point pr. runde",
        note=lambda m: f"Snit {m['avg']} point")
    add("📈", "Raketten", lambda m: m["best_climb"][0], fmt=lambda v: f"+{v} pladser på én runde",
        note=lambda m: f"GW {m['best_climb'][1]}", min_val=1)
    add("📉", "Faldskærmen", lambda m: m["worst_drop"][0], best=min, fmt=lambda v: f"{v} pladser på én runde",
        note=lambda m: f"GW {m['worst_drop'][1]}", max_val=-1)
    add("🤝", "Alle-mod-alle-mester", lambda m: m["allplay"]["pct"], fmt=lambda v: f"{v}% sejre mod alle",
        note=lambda m: f"{m['allplay']['w']}-{m['allplay']['d']}-{m['allplay']['l']}")
    if kind == "h2h":
        add("🍀", "Heldig kartoffel", lambda m: m["luck"], fmt=lambda v: f"+{v} sejre mere end fortjent", min_val=0.01)
        add("🙈", "Universets hakkekylling", lambda m: m["luck"], best=min,
            fmt=lambda v: f"{v} sejre i forhold til fortjent", max_val=-0.01)
        add("🦹", "Røveren", lambda m: m["robberies"], fmt=lambda v: f"{v} sejr{'e' if v != 1 else ''} med under-median-score", min_val=1)
        add("💔", "Uheldig i kærlighed", lambda m: m["unlucky_losses"],
            fmt=lambda v: f"{v} nederlag med over-median-score", min_val=1)
        add("🛡️", "Mest jagtet", lambda m: m["h2h"]["pa"], fmt=lambda v: f"{v} point imod")
    return awards


# --------------------------------------------------------------------------- #
# Webserver
# --------------------------------------------------------------------------- #

def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


_mem_cache = {}
_build_lock = threading.Lock()


def get_data(raw, refresh=False):
    raw = (raw or "").strip()
    with _build_lock:
        hit = _mem_cache.get(raw)
        if hit and not refresh and time.time() - hit[0] < MEMORY_TTL:
            return hit[1]
        print(f"[{datetime.now():%H:%M:%S}] Henter {raw} ...")
        t0 = time.time()
        data = build(*resolve(raw))
        print(f"  Færdig på {time.time() - t0:.1f} s")
        _mem_cache[raw] = (time.time(), data)
        cfg = load_config()
        cfg["draft_league"] = raw
        save_config(cfg)
        return data


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            with open(DASHBOARD, encoding="utf-8") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif u.path == "/api/config":
            self._json(200, {"league": load_config().get("draft_league")})
        elif u.path == "/api/league":
            try:
                self._json(200, get_data(q.get("id", [""])[0], refresh=q.get("refresh") == ["1"]))
            except UserError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                self._json(500, {"error": f"Uventet fejl: {e}"})
        else:
            self._send(404, "Ikke fundet", "text/plain; charset=utf-8")

    def log_message(self, *args):
        pass


def free_port(preferred):
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("Kunne ikke finde en ledig port.")


def export_html(raw, path):
    data = build(*resolve(raw))
    with open(DASHBOARD, encoding="utf-8") as f:
        html = f.read()
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = html.replace("</head>", f"<script>window.__FPL_DATA__ = {payload};</script>\n</head>", 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return os.path.abspath(path)


def main():
    ap = argparse.ArgumentParser(description="FPL Draft Liga Dashboard")
    ap.add_argument("league", nargs="?", help="Link fra draft.premierleague.com eller liga-ID (valgfrit)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--export", metavar="FIL.html", help="Gem en statisk HTML-rapport i stedet for at starte serveren")
    ap.add_argument("--build", metavar="MAPPE",
                    help="Byg et statisk site (MAPPE/index.html) – bruges af Render. Liga tages fra FPL_LEAGUE.")
    ap.add_argument("--no-browser", action="store_true", help="Åbn ikke browseren automatisk")
    args = ap.parse_args()

    league = args.league or os.environ.get("FPL_LEAGUE") or load_config().get("draft_league")

    if args.build:
        if not league:
            raise SystemExit("Mangler liga: sæt miljøvariablen FPL_LEAGUE til linket fra draft-siden.")
        os.makedirs(args.build, exist_ok=True)
        try:
            out = export_html(league, os.path.join(args.build, "index.html"))
        except UserError as e:
            raise SystemExit(f"Fejl: {e}")  # fejlet build -> Render beholder den forrige version
        print(f"Statisk site bygget: {out}")
        return

    if args.export:
        if not league:
            raise SystemExit('Angiv ligaen, fx: python3 fpl_liga.py "<link>" --export rapport.html')
        try:
            out = export_html(league, args.export)
        except UserError as e:
            raise SystemExit(f"Fejl: {e}")
        print(f"Rapport gemt: {out}")
        if not args.no_browser:
            webbrowser.open("file://" + out)
        return

    port = free_port(args.port)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    if args.league:
        url += "?league=" + urllib.request.quote(args.league.strip(), safe="")
    print("=" * 60)
    print("  ⚽  FPL Draft Liga Dashboard kører på", f"http://127.0.0.1:{port}/")
    print("  Luk dette vindue (eller tryk Ctrl+C) for at stoppe.")
    print("=" * 60)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStoppet.")


if __name__ == "__main__":
    if sys.version_info < (3, 8):
        raise SystemExit("Kræver Python 3.8 eller nyere.")
    main()
