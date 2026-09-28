#!/usr/bin/env python3
"""Measure how much Claude Code token spend goes to subagents, and how much of
that is subagents rewriting their prompt cache after sitting idle.

Reads the local transcripts under ~/.claude/projects and prints totals only.
Nothing leaves the machine and no transcript content is printed.

Usage:
    python3 measure.py [--days N] [--projects-dir PATH]
"""

import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# API list prices in US dollars per million tokens, as published at
# https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-09-28).
# Columns: input, 5-minute cache write (1.25x input), 1-hour cache write
# (2x input), cache read, output. Cache reads are 0.1x input except on
# Opus 5.5 (0.05x) and Fable 5.1 (0.025x).
# A model ID matches a key exactly or with a -YYYYMMDD date suffix.
PRICES = {
    "claude-fable-5-1": (10.00, 12.50, 20.00, 0.25, 50.00),
    "claude-fable-5": (10.00, 12.50, 20.00, 1.00, 50.00),
    "claude-opus-5-5": (4.00, 5.00, 8.00, 0.20, 20.00),
    "claude-opus-5": (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4-8": (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4-7": (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4-6": (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4-5": (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-sonnet-5-5": (2.00, 2.50, 4.00, 0.20, 10.00),
    "claude-sonnet-5": (2.00, 2.50, 4.00, 0.20, 10.00),
    "claude-sonnet-4-6": (3.00, 3.75, 6.00, 0.30, 15.00),
    "claude-sonnet-4-5": (3.00, 3.75, 6.00, 0.30, 15.00),
    "claude-haiku-4-5": (1.00, 1.25, 2.00, 0.10, 5.00),
}

# Idle-reread rule: a subagent call after its first one whose cache write is
# above this many tokens, larger than its cache read, and more than
# IDLE_SECONDS after the same subagent's previous call.
IDLE_WRITE_MIN = 20_000
IDLE_SECONDS = 300

# Token categories, in report order. "write_unknown" holds cache writes whose
# log has no 5m/1h breakdown; they are priced at the 5-minute rate.
CATEGORIES = ("input", "read", "write_5m", "write_1h", "write_unknown", "output")
LABELS = {
    "input": "uncached input",
    "read": "cache reads",
    "write_5m": "5m cache writes",
    "write_1h": "1h cache writes",
    "write_unknown": "cache writes, TTL not logged",
    "output": "output",
}
PRICE_INDEX = {"input": 0, "write_5m": 1, "write_1h": 2, "write_unknown": 1, "read": 3, "output": 4}


def price_for(model):
    """Return the price tuple for a model ID, or None if it is not in PRICES."""
    if not model:
        return None
    if model in PRICES:
        return PRICES[model]
    base, _, suffix = model.rpartition("-")
    if base in PRICES and len(suffix) == 8 and suffix.isdigit():
        return PRICES[base]
    return None


def parse_ts(value):
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def tokens_of(usage):
    """Split one usage block into the token categories."""
    cc = usage.get("cache_creation")
    total_write = usage.get("cache_creation_input_tokens") or 0
    t = {
        "input": usage.get("input_tokens") or 0,
        "read": usage.get("cache_read_input_tokens") or 0,
        "output": usage.get("output_tokens") or 0,
        "write_5m": 0,
        "write_1h": 0,
        "write_unknown": 0,
    }
    if isinstance(cc, dict):
        t["write_5m"] = cc.get("ephemeral_5m_input_tokens") or 0
        t["write_1h"] = cc.get("ephemeral_1h_input_tokens") or 0
        # Anything in the total that the breakdown does not account for.
        t["write_unknown"] = max(0, total_write - t["write_5m"] - t["write_1h"])
    else:
        t["write_unknown"] = total_write
    return t


def cost_of(tokens, prices):
    return {c: tokens[c] * prices[PRICE_INDEX[c]] / 1e6 for c in CATEGORIES}


def load_calls(projects_dir, cutoff):
    """Read every transcript and return (calls, stats).

    Each call is a dict with keys: scope ("main" or "subagent"), agent (a key
    identifying one subagent, or None), model, ts, tokens. Records logged more
    than once for the same API response are merged on (message.id, requestId),
    keeping the largest value of each token count: later records of one
    response can carry a higher output count, never a second copy of the input.
    """
    seen = {}
    calls = []
    stats = {"files": 0, "bad_lines": 0, "duplicates": 0, "no_ids": 0}
    pattern = os.path.join(projects_dir, "**", "*.jsonl")
    cutoff_epoch = cutoff.timestamp()
    for path in glob.glob(pattern, recursive=True):
        try:
            if os.path.getmtime(path) < cutoff_epoch:
                continue  # untouched since the cutoff, so nothing in the window
        except OSError:
            continue
        stats["files"] += 1
        rel = os.path.relpath(path, projects_dir)
        in_subagent_dir = "subagents" in rel.split(os.sep)[:-1]
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for lineno, line in enumerate(fh):
                try:
                    rec = json.loads(line)
                except ValueError:
                    stats["bad_lines"] += 1
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "assistant":
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                ts = parse_ts(rec.get("timestamp"))
                if ts is None:
                    stats["bad_lines"] += 1
                    continue
                tokens = tokens_of(usage)
                if not any(tokens.values()):
                    continue  # e.g. "<synthetic>" records with no API call behind them
                mid, rid = msg.get("id"), rec.get("requestId")
                if mid is None or rid is None:
                    stats["no_ids"] += 1
                    key = ("no-id", path, lineno)
                else:
                    key = (mid, rid)
                if key in seen:
                    stats["duplicates"] += 1
                    kept = seen[key]
                    for cat in CATEGORIES:
                        kept["tokens"][cat] = max(kept["tokens"][cat], tokens[cat])
                    kept["ts"] = min(kept["ts"], ts)
                    continue
                is_sub = in_subagent_dir or rec.get("isSidechain") is True
                seen[key] = call = {
                    "scope": "subagent" if is_sub else "main",
                    "agent": (path, rec.get("agentId")) if is_sub else None,
                    "model": msg.get("model") or "(no model)",
                    "ts": ts,
                    "tokens": tokens,
                }
                calls.append(call)
    return calls, stats


def mark_idle_rereads(calls):
    """Set call["idle"] on subagent calls that match the idle-reread rule.

    Gaps are measured over each subagent's full history, so a call just inside
    the window still sees a predecessor that falls before it.
    """
    by_agent = defaultdict(list)
    for c in calls:
        c["idle"] = False
        if c["scope"] == "subagent":
            by_agent[c["agent"]].append(c)
    for seq in by_agent.values():
        seq.sort(key=lambda c: c["ts"])
        for prev, cur in zip(seq, seq[1:]):
            t = cur["tokens"]
            write = t["write_5m"] + t["write_1h"] + t["write_unknown"]
            gap = (cur["ts"] - prev["ts"]).total_seconds()
            if write > IDLE_WRITE_MIN and write > t["read"] and gap > IDLE_SECONDS:
                cur["idle"] = True


def summarize(calls, cutoff, end=None):
    """Aggregate calls from the cutoff up to end (default now) into report totals."""
    end = end or datetime.now(timezone.utc)
    s = {
        "tokens": defaultdict(lambda: defaultdict(int)),   # scope -> category -> tokens
        "cost": defaultdict(lambda: defaultdict(float)),   # scope -> category -> dollars
        "models": defaultdict(lambda: {"calls": 0, "tokens": 0, "cost": 0.0, "priced": True}),
        "unpriced_tokens": 0,
        "idle": {"calls": 0, "write_tokens": 0, "write_cost": 0.0, "call_cost": 0.0},
        "calls": defaultdict(int),
    }
    for c in calls:
        if not cutoff <= c["ts"] <= end:
            continue
        scope, t = c["scope"], c["tokens"]
        s["calls"][scope] += 1
        for cat in CATEGORIES:
            s["tokens"][scope][cat] += t[cat]
        prices = price_for(c["model"])
        m = s["models"][c["model"]]
        m["calls"] += 1
        m["tokens"] += sum(t.values())
        if prices is None:
            m["priced"] = False
            s["unpriced_tokens"] += sum(t.values())
            cost = None
        else:
            cost = cost_of(t, prices)
            for cat in CATEGORIES:
                s["cost"][scope][cat] += cost[cat]
            m["cost"] += sum(cost.values())
        if c.get("idle"):
            idle = s["idle"]
            idle["calls"] += 1
            idle["write_tokens"] += t["write_5m"] + t["write_1h"] + t["write_unknown"]
            if cost is not None:
                idle["write_cost"] += cost["write_5m"] + cost["write_1h"] + cost["write_unknown"]
                idle["call_cost"] += sum(cost.values())
    return s


def pct(part, whole):
    return f"{100 * part / whole:5.1f}%" if whole else "    -"


def render(s, days, stats):
    out = []
    total_cost = sum(sum(v.values()) for v in s["cost"].values())
    total_tokens = sum(sum(v.values()) for v in s["tokens"].values())
    out.append(f"Claude Code token spend, last {days} day(s)")
    out.append(f"{stats['files']} transcript files read, "
               f"{s['calls']['main'] + s['calls']['subagent']} API calls "
               f"({s['calls']['main']} main, {s['calls']['subagent']} subagent), "
               f"{stats['duplicates']} duplicate records merged")
    if stats["bad_lines"] or stats["no_ids"]:
        out.append(f"{stats['bad_lines']} unreadable lines skipped, "
                   f"{stats['no_ids']} records without IDs counted without deduplication")
    out.append("")
    if not total_tokens:
        out.append("No assistant calls with token usage found in this window.")
        return "\n".join(out)

    out.append("Weighted spend by bucket (share of list-price-weighted total)")
    out.append(f"  {'bucket':<40}{'tokens':>16}{'list $':>12}{'share':>8}")
    for scope in ("main", "subagent"):
        for cat in CATEGORIES:
            tok = s["tokens"][scope][cat]
            if not tok:
                continue
            cost = s["cost"][scope][cat]
            out.append(f"  {scope + ' ' + LABELS[cat]:<40}{tok:>16,}{cost:>12,.2f}{pct(cost, total_cost):>8}")
    for scope in ("main", "subagent"):
        cost = sum(s["cost"][scope].values())
        out.append(f"  {scope + ' total':<40}{sum(s['tokens'][scope].values()):>16,}{cost:>12,.2f}{pct(cost, total_cost):>8}")
    out.append(f"  {'all priced models':<40}{'':>16}{total_cost:>12,.2f}")
    out.append("")

    idle = s["idle"]
    out.append(f"Idle rereads (subagent call after its first, cache write > {IDLE_WRITE_MIN:,} tokens "
               f"and > its cache read, more than {IDLE_SECONDS} s after that subagent's previous call)")
    out.append(f"  calls: {idle['calls']:,}   cache-write tokens: {idle['write_tokens']:,}")
    out.append(f"  cache-write spend: ${idle['write_cost']:,.2f} ({pct(idle['write_cost'], total_cost).strip()} of total)")
    out.append(f"  whole-call spend:  ${idle['call_cost']:,.2f} ({pct(idle['call_cost'], total_cost).strip()} of total)")
    out.append("  These are a subset of the subagent rows above, not an extra bucket.")
    out.append("")

    sub = s["tokens"]["subagent"]
    w5, w1, wu = sub["write_5m"], sub["write_1h"], sub["write_unknown"]
    wall = w5 + w1 + wu
    out.append("Subagent cache writes by TTL (after the fix, 1h should dominate)")
    out.append(f"  5m: {w5:>16,} {pct(w5, wall)}")
    out.append(f"  1h: {w1:>16,} {pct(w1, wall)}")
    if wu:
        out.append(f"  TTL not logged: {wu:>16,} {pct(wu, wall)}")
    out.append("")

    out.append("By model")
    out.append(f"  {'model':<32}{'calls':>9}{'tokens':>16}{'list $':>12}{'share':>8}")
    for model, m in sorted(s["models"].items(), key=lambda kv: (-kv[1]["cost"], -kv[1]["tokens"])):
        if m["priced"]:
            out.append(f"  {model:<32}{m['calls']:>9,}{m['tokens']:>16,}{m['cost']:>12,.2f}{pct(m['cost'], total_cost):>8}")
        else:
            out.append(f"  {model:<32}{m['calls']:>9,}{m['tokens']:>16,}{'not priced':>12}{'':>8}")
    if s["unpriced_tokens"]:
        out.append(f"  {s['unpriced_tokens']:,} tokens from models not in the price table are counted "
                   "in tokens but left out of every dollar figure and share above.")
    out.append("")
    out.append("Note: dollar figures weight tokens at API list prices. That is a proxy. Anthropic does not")
    out.append("publish how subscription plan limits weight tokens, so shares of plan usage may differ.")
    out.append("Cache writes with no logged TTL are priced at the 5-minute rate.")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, default=7, help="look back this many days (default 7)")
    ap.add_argument("--projects-dir", default=os.path.expanduser("~/.claude/projects"),
                    help="Claude Code transcripts folder (default ~/.claude/projects)")
    args = ap.parse_args(argv)
    if args.days <= 0:
        ap.error("--days must be positive")
    if not os.path.isdir(args.projects_dir):
        ap.error(f"no such folder: {args.projects_dir}")
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    calls, stats = load_calls(args.projects_dir, cutoff)
    mark_idle_rereads(calls)
    print(render(summarize(calls, cutoff), f"{args.days:g}", stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
