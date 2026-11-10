"""Build site/data/approps.json for the Concord Action Appropriations Tracker.

No AI anywhere in this path: it reads official records, matches action text with fixed rules,
and writes one small JSON file.

Sources, in order:
  1. Congress.gov API v3 (/bill/{c}/{type}/{n} and /actions), when CONGRESS_API_KEY is set to a real key.
  2. Otherwise the same Library of Congress bill records from GovInfo's no-key bulk feed
     (https://www.govinfo.gov/bulkdata/BILLSTATUS/...). DEMO_KEY allows only about 10 calls, so the
     script never uses it for a full run.
  3. Roll-call tallies: House Clerk XML (clerk.house.gov/evs) and senate.gov LIS XML.

Run:  python -I scripts/fetch_status.py            (writes site/data/approps.json)
      python -I scripts/fetch_status.py --source congress --only mcva   (test one bill on the API)
The key is read from the environment or a git-ignored .env file next to this folder. It is never
written to the output.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config", "bills.json")
OUT = os.path.join(ROOT, "site", "data", "approps.json")
UA = "ConcordActionAppropsTracker/0.1 (mockup; contact via concordaction.org)"

TYPE_PATH = {"hr": "house-bill", "s": "senate-bill", "hjres": "house-joint-resolution", "sjres": "senate-joint-resolution"}
TYPE_LABEL = {"hr": "H.R.", "s": "S.", "hjres": "H.J.Res.", "sjres": "S.J.Res."}
CALLS = {"congress": 0, "govinfo": 0, "clerk": 0, "senate": 0}


# ---------------------------------------------------------------- fetching

def load_env():
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def get(url, kind, tries=3):
    CALLS[kind] += 1
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (404, 403):
                break
        except Exception as e:  # network hiccup
            last = e
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"fetch failed {redact(url)}: {last}")


def redact(url):
    return re.sub(r"api_key=[^&]+", "api_key=***", url)


def congress_gov_url(ref, congress):
    return f"https://www.congress.gov/bill/{congress}th-congress/{TYPE_PATH[ref['type']]}/{ref['number']}"


def label(ref):
    return f"{TYPE_LABEL[ref['type']]} {ref['number']}"


# Normalized action: {date, text, type, code, system, votes: [{chamber, roll, url, date}]}

def actions_from_api(ref, congress, key):
    base = f"https://api.congress.gov/v3/bill/{congress}/{ref['type']}/{ref['number']}"
    bill = json.loads(get(f"{base}?format=json&api_key={key}", "congress"))["bill"]
    acts, offset = [], 0
    while True:
        page = json.loads(get(f"{base}/actions?format=json&limit=250&offset={offset}&api_key={key}", "congress"))
        for a in page.get("actions", []):
            votes = [{"chamber": v.get("chamber"), "roll": v.get("rollNumber"), "url": v.get("url"), "date": v.get("date")}
                     for v in a.get("recordedVotes", []) or []]
            acts.append({"date": a.get("actionDate"), "text": a.get("text") or "", "type": a.get("type") or "",
                         "code": a.get("actionCode") or "", "system": (a.get("sourceSystem") or {}).get("name", ""),
                         "votes": votes})
        if not page.get("pagination", {}).get("next"):
            break
        offset += 250
    title = bill.get("title", "")
    latest = bill.get("latestAction") or {}
    laws = [f"{l.get('type', 'Public Law')} {l.get('number')}" for l in bill.get("laws", []) or []]
    return {"title": title, "latest": {"date": latest.get("actionDate"), "text": latest.get("text")},
            "updated": bill.get("updateDate"), "laws": laws, "actions": acts, "via": "Congress.gov API v3"}


def actions_from_govinfo(ref, congress):
    url = f"https://www.govinfo.gov/bulkdata/BILLSTATUS/{congress}/{ref['type']}/BILLSTATUS-{congress}{ref['type']}{ref['number']}.xml"
    bill = ET.fromstring(get(url, "govinfo")).find("bill")
    acts = []
    for a in bill.find("actions").findall("item"):
        votes = []
        rv = a.find("recordedVotes")
        if rv is not None:
            for v in rv.findall("recordedVote"):
                votes.append({"chamber": v.findtext("chamber"), "roll": v.findtext("rollNumber"),
                              "url": v.findtext("url"), "date": v.findtext("date")})
        ss = a.find("sourceSystem")
        acts.append({"date": a.findtext("actionDate"), "text": a.findtext("text") or "", "type": a.findtext("type") or "",
                     "code": a.findtext("actionCode") or "", "system": ss.findtext("name") if ss is not None else "",
                     "votes": votes})
    la = bill.find("latestAction")
    laws = []
    lw = bill.find("laws")
    if lw is not None:
        laws = [f"{i.findtext('type')} {i.findtext('number')}" for i in lw.findall("item")]
    return {"title": bill.findtext("title") or "",
            "latest": {"date": la.findtext("actionDate") if la is not None else None,
                       "text": la.findtext("text") if la is not None else None},
            "updated": bill.findtext("updateDate"), "laws": laws, "actions": acts,
            "via": "Congress.gov bill status (GovInfo BILLSTATUS feed)"}


# ---------------------------------------------------------------- votes

def party_word(p):
    return {"R": "Republicans", "D": "Democrats", "I": "Independents", "ID": "Independents"}.get(p, p)


def house_vote(url):
    r = ET.fromstring(get(url, "clerk"))
    md = r.find("vote-metadata")
    parties = []
    for t in md.find("vote-totals").findall("totals-by-party"):
        parties.append({"party": t.findtext("party"), "yea": int(t.findtext("yea-total") or 0),
                        "nay": int(t.findtext("nay-total") or 0)})
    tot = md.find("vote-totals").find("totals-by-vote")
    d = dt.datetime.strptime(md.findtext("action-date"), "%d-%b-%Y").date().isoformat()
    return {"chamber": "House", "roll": int(md.findtext("rollcall-num")), "date": d,
            "question": (md.findtext("vote-question") or "").strip(), "result": (md.findtext("vote-result") or "").strip(),
            "yea": int(tot.findtext("yea-total") or 0), "nay": int(tot.findtext("nay-total") or 0),
            "notVoting": int(tot.findtext("not-voting-total") or 0), "parties": parties, "source": url}


def senate_vote(url):
    r = ET.fromstring(get(url, "senate"))
    cnt = r.find("count")
    parties = {}
    for m in r.find("members").findall("member"):
        p = m.findtext("party")
        c = (m.findtext("vote_cast") or "").strip()
        e = parties.setdefault(p, {"party": party_word(p), "yea": 0, "nay": 0})
        if c in ("Yea", "Guilty"):
            e["yea"] += 1
        elif c in ("Nay", "Not Guilty"):
            e["nay"] += 1
    raw = (r.findtext("vote_date") or "").strip()  # e.g. "August 8, 2026,  05:42 PM"
    m = re.match(r"([A-Za-z]+ \d{1,2}, \d{4})", raw)
    d = dt.datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat() if m else None
    return {"chamber": "Senate", "roll": int(r.findtext("vote_number")), "date": d,
            "question": (r.findtext("vote_question_text") or r.findtext("question") or "").strip(),
            "result": (r.findtext("vote_result") or "").strip(),
            "yea": int(cnt.findtext("yeas") or 0), "nay": int(cnt.findtext("nays") or 0),
            "notVoting": int(cnt.findtext("absent") or 0), "parties": list(parties.values()), "source": url}


def fetch_vote(v):
    url = v.get("url") or ""
    if "clerk.house.gov" in url:
        return house_vote(url)
    if "senate.gov" in url:
        return senate_vote(url.replace("http://", "https://"))
    return None


# ---------------------------------------------------------------- step rules
# Each rule is plain text matching on the official action wording. Codes are hints only:
# no official list of action codes was readable on Nov 10 (glossary page returned 403).

RULES = {
    "houseCommittee": [r"House Committee on Appropriations reported", r"Reported \(Amended\) by the Committee on Appropriations",
                       r"^Reported by the Committee on Appropriations"],
    "senateCommittee": [r"Committee on Appropriations\. Original measure reported to Senate",
                        r"Committee on Appropriations\. Reported by Senator", r"Senate Committee on Appropriations reported"],
    "housePassed": [r"^Passed/agreed to in House", r"^On passage Passed", r"^On motion to suspend the rules and pass the bill.*Agreed to"],
    "senatePassed": [r"^Passed/agreed to in Senate", r"Passed Senate with an amendment", r"Passed Senate without amendment"],
    "resolved": [r"^Resolving differences", r"agree to the Senate amendments? Agreed to", r"concur in the Senate amendment.*Agreed to",
                 r"Senate agreed to conference report", r"House agreed to conference report", r"Conference report agreed to",
                 r"concurred in the House amendment"],
    "toPresident": [r"^Presented to President"],
    "law": [r"^Became Public Law", r"^Signed by President"],
}


def first_match(actions, key):
    pats = [re.compile(p, re.I) for p in RULES[key]]
    hits = [a for a in actions if any(p.search(a["text"]) for p in pats)]
    hits.sort(key=lambda a: a["date"] or "")
    return hits[0] if hits else None


def chamber_of(a):
    s = (a.get("system") or "").lower()
    if "senate" in s:
        return "Senate"
    if "house" in s:
        return "House"
    return None


# ---------------------------------------------------------------- build

STEPS = [
    ("sub", "Subcommittee"),
    ("committee", "Committee"),
    ("house", "House floor"),
    ("senate", "Senate floor"),
    ("final", "Final deal"),
    ("law", "Signed into law"),
]


def vote_for(action, cache):
    if not action or not action["votes"]:
        return None
    v = action["votes"][-1]
    key = v.get("url")
    if key not in cache:
        try:
            cache[key] = fetch_vote(v)
        except Exception as e:
            cache[key] = {"error": str(e), "source": key}
    return cache[key]


def summarize_bill(cfg_bill, recs, crs, cache):
    """recs: {'house': record or None, 'senate': record or None}."""
    acts = []
    for side in ("house", "senate"):
        if recs.get(side):
            for a in recs[side]["actions"]:
                acts.append(dict(a, bill=side))
    acts.sort(key=lambda a: a["date"] or "")

    hc = first_match(acts, "houseCommittee")
    sc = first_match(acts, "senateCommittee")
    hp = first_match(acts, "housePassed")
    sp = first_match(acts, "senatePassed")
    rs = first_match(acts, "resolved")
    lw = first_match(acts, "law")
    crs_h = crs.get("house", {}).get(cfg_bill["id"], {})
    crs_s = crs.get("senate", {}).get(cfg_bill["id"], {})

    def chamber_pair(h, s):
        done = [x for x in (h, s) if x]
        return "done" if len(done) == 2 else ("partial" if done else "todo")

    steps = []
    # Subcommittee: CRS table (hand-entered reference) since Congress.gov has no record of it.
    sub_h = crs_h.get("sub")
    sub_s = crs_s.get("sub")
    steps.append({"key": "sub", "label": "Subcommittee", "status": chamber_pair(sub_h, sub_s),
                  "house": {"date": sub_h["date"], "vote": sub_h.get("vote"), "source": "CRS status table"} if sub_h else None,
                  "senate": {"date": sub_s["date"], "vote": sub_s.get("vote"), "source": "CRS status table"} if sub_s else None})
    full_h = crs_h.get("full")
    steps.append({"key": "committee", "label": "Committee", "status": chamber_pair(hc, sc),
                  "house": {"date": (full_h or {}).get("date") or hc["date"], "vote": (full_h or {}).get("vote"),
                            "reported": hc["date"], "text": hc["text"].strip()} if hc else None,
                  "senate": {"date": sc["date"], "text": sc["text"].strip()} if sc else None})
    hv = vote_for(hp, cache)
    sv = vote_for(sp, cache)
    steps.append({"key": "house", "label": "House floor", "status": "done" if hp else "todo",
                  "date": hp["date"] if hp else None, "vote": hv})
    steps.append({"key": "senate", "label": "Senate floor", "status": "done" if sp else "todo",
                  "date": sp["date"] if sp else None, "vote": sv})
    fv = vote_for(rs, cache)
    steps.append({"key": "final", "label": "Final deal", "status": "done" if rs else "todo",
                  "date": rs["date"] if rs else None, "vote": fv, "text": rs["text"].strip() if rs else None})
    steps.append({"key": "law", "label": "Signed into law", "status": "done" if lw else "todo",
                  "date": lw["date"] if lw else None, "text": lw["text"].strip() if lw else None})

    stage = 0
    for i, s in enumerate(steps):
        if s["status"] != "todo":
            stage = i + 1

    # Last recorded vote of any kind on the bill (passage, recommit, cloture...).
    # Same-day actions come in no reliable order, so take the latest date, then the highest roll number.
    last_vote = None
    voted = [a for a in acts if a["votes"]]
    if voted:
        a = max(voted, key=lambda a: (a["date"] or "", max(int(v.get("roll") or 0) for v in a["votes"])))
        last_vote = vote_for(a, cache)
        if last_vote and "error" not in last_vote:
            last_vote = dict(last_vote, action=a["text"].strip())
    if last_vote is None and full_h:
        last_vote = {"chamber": "House Appropriations Committee", "date": full_h["date"], "tally": full_h.get("vote"),
                     "question": "Full committee markup", "source": "CRS status table"}

    latest = max((r["latest"] for r in recs.values() if r and r.get("latest") and r["latest"].get("date")),
                 key=lambda x: x["date"], default=None)
    out = {
        "id": cfg_bill["id"], "name": cfg_bill["name"], "short": cfg_bill["short"],
        "stage": stage, "stageLabel": STEPS[stage - 1][1] if stage else "Not started",
        "steps": steps, "latestAction": latest, "lastVote": last_vote,
        "bills": [],
    }
    for side in ("house", "senate"):
        ref = cfg_bill.get(side)
        if ref:
            r = recs.get(side)
            out["bills"].append({"chamber": side.title(), "label": label(ref), "url": congress_gov_url(ref, CONGRESS),
                                 "title": r["title"] if r else None, "updated": r["updated"] if r else None,
                                 "laws": r["laws"] if r else [], "found": bool(r)})
    return out


def main():
    global CONGRESS
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["auto", "congress", "govinfo"], default="auto")
    ap.add_argument("--only", help="comma list of bill ids to fetch (testing)")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    load_env()
    cfg = json.load(open(CONFIG, encoding="utf8"))
    CONGRESS = cfg["congress"]
    key = os.environ.get("CONGRESS_API_KEY", "").strip()
    source = args.source
    if source == "auto":
        source = "congress" if key and key != "DEMO_KEY" else "govinfo"
    if source == "congress" and not key:
        key = "DEMO_KEY"

    fallbacks = []

    def fetch(ref):
        if source == "congress":
            try:
                return actions_from_api(ref, CONGRESS, key)
            except Exception as e:
                # Same Library of Congress record, no key needed. Keeps the page current on a slow API day.
                print(f"Congress.gov API failed for {label(ref)} ({e}); using GovInfo BILLSTATUS", file=sys.stderr)
                fallbacks.append(label(ref))
        return actions_from_govinfo(ref, CONGRESS)

    flags, cache, rows, no_senate = [], {}, [], []
    only = set(args.only.split(",")) if args.only else None
    for b in cfg["bills"]:
        if only and b["id"] not in only:
            continue
        recs = {}
        for side in ("house", "senate"):
            ref = b.get(side)
            if not ref:
                if side == "senate":
                    no_senate.append(b["short"])
                continue
            try:
                recs[side] = fetch(ref)
                if str(cfg["fiscalYear"]) not in json.dumps(recs[side]["title"]) and "2027" not in recs[side]["title"]:
                    flags.append(f"{label(ref)}: title does not mention {cfg['fiscalYear']}; check the match.")
            except Exception as e:
                recs[side] = None
                flags.append(f"{b['short']}: could not read {label(ref)} ({e}).")
        rows.append(summarize_bill(b, recs, cfg.get("crsMarkups", {}), cache))

    if no_senate:
        n = len(no_senate)
        flags.append(("No Senate bill numbers yet for any of the 12 bills" if n == len(cfg["bills"]) else
                      f"No Senate bill number yet for: {', '.join(no_senate)}") +
                     " (none in Congress.gov records or the CRS table). Add them to config/bills.json when the Senate committee reports.")
    stopgaps = []
    for c in cfg.get("stopgaps", []):
        if only:
            break
        try:
            r = fetch(c["bill"])
            fake = {"id": c["id"], "name": c["name"], "short": c["name"], "house": c["bill"]}
            s = summarize_bill(fake, {"house": r}, {}, cache)
            s["through"] = c["through"]
            s["laws"] = r["laws"]
            stopgaps.append(s)
        except Exception as e:
            flags.append(f"Stopgap {label(c['bill'])}: could not read ({e}).")

    if fallbacks:
        flags.append("Congress.gov API did not answer for " + ", ".join(fallbacks) + "; read the same record from GovInfo instead.")
    counts = {k: sum(1 for r in rows if next(s for s in r["steps"] if s["key"] == k)["status"] != "todo")
              for k, _ in STEPS}
    data = {
        "lastUpdated": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "fiscalYear": cfg["fiscalYear"], "congress": CONGRESS,
        "source": {"bills": "Congress.gov API v3" if source == "congress" else "Congress.gov bill status via GovInfo BILLSTATUS",
                   "votes": "House Clerk roll-call XML and senate.gov roll-call XML",
                   "markups": f"CRS Appropriations Status Table (subcommittee and committee markup dates, as of {cfg['crsMarkups']['asOf']})",
                   "calls": CALLS},
        "deadline": cfg["deadline"], "onTimeStat": cfg["onTimeStat"],
        "steps": [{"key": k, "label": l} for k, l in STEPS],
        "counts": counts, "bills": rows, "stopgaps": stopgaps, "flags": flags,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, args.out)
    print(f"wrote {args.out} ({os.path.getsize(args.out):,} B) via {data['source']['bills']}; calls {CALLS}")
    for fl in flags:
        print("FLAG:", fl)
    return 0


CONGRESS = 119
if __name__ == "__main__":
    sys.exit(main())
