#!/usr/bin/env python3
"""Run coding-agent workers and measure them: wall time and tokens.

  agent_run.py run --task T1 --worker codex --model gpt-5-mini [--tag baseline] [--cwd DIR] -- <command...>
  agent_run.py start [--mission TEXT]      (once, at the start of a mission)
  agent_run.py report [--lead-session FILE | --no-lead]

Every run appends one line to tmp/agent-runs.jsonl and keeps its full output in
tmp/runs/. Use JSON output so tokens can be read:
  codex exec --json ...        claude -p --output-format json ...
Standard library only.
"""
import argparse, glob, json, os, re, subprocess, sys, time
from datetime import datetime

LEDGER = os.path.join("tmp", "agent-runs.jsonl")
RUNS_DIR = os.path.join("tmp", "runs")


def empty():
    return {"fresh_input": 0, "cached_input": 0, "output": 0}


def parse_tokens(text):
    """Return (tokens dict, source) from a worker's output, or (None, None)."""
    claude_result, turn_sum, last_total = None, None, None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        # claude -p --output-format json (or stream-json): final result carries totals
        if obj.get("type") == "result" and isinstance(obj.get("usage"), dict):
            u = obj["usage"]
            claude_result = {
                "fresh_input": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0),
                "cached_input": u.get("cache_read_input_tokens", 0),
                "output": u.get("output_tokens", 0),
            }
        # codex exec --json: one usage block per completed turn
        if obj.get("type") == "turn.completed" and isinstance(obj.get("usage"), dict):
            u = obj["usage"]
            cached = u.get("cached_input_tokens", 0)
            turn_sum = turn_sum or empty()
            turn_sum["fresh_input"] += u.get("input_tokens", 0) - cached
            turn_sum["cached_input"] += cached
            turn_sum["output"] += u.get("output_tokens", 0)
        # older codex event shape: running totals, keep the last one
        found = _find_key(obj, "total_token_usage")
        if isinstance(found, dict):
            cached = found.get("cached_input_tokens", 0)
            last_total = {
                "fresh_input": found.get("input_tokens", 0) - cached,
                "cached_input": cached,
                "output": found.get("output_tokens", 0),
            }
    for tokens, source in ((claude_result, "claude-json"), (turn_sum, "codex-json"), (last_total, "codex-totals")):
        if tokens:
            return tokens, source
    # plain text fallback, e.g. "tokens used: 12,345" (total only)
    m = re.findall(r"tokens? used[:\s]+([\d,]+)", text, re.I)
    if m:
        return {"fresh_input": 0, "cached_input": 0, "output": 0, "total_only": int(m[-1].replace(",", ""))}, "text"
    return None, None


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def total(t):
    if not t:
        return 0
    return t["fresh_input"] + t["cached_input"] + t["output"] + t.get("total_only", 0)


def cmd_run(a):
    if not a.command:
        sys.exit("give the worker command after --")
    os.makedirs(RUNS_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = os.path.join(RUNS_DIR, f"{a.task}-{a.worker}-{stamp}.log")
    head_before = git_head(a.cwd)
    start = time.time()
    # Worker output goes to the log only. Echoing it would pour the worker's
    # whole JSON stream into the lead's context and burn the lead's tokens.
    with open(log_path, "w") as log:
        code = subprocess.call(a.command, cwd=a.cwd, stdout=log, stderr=subprocess.STDOUT)
    end = time.time()
    with open(log_path) as f:
        text = f.read()
    tokens, source = parse_tokens(text)
    head_after = git_head(a.cwd)
    commits = None
    if head_before and head_after:
        out = subprocess.run(["git", "rev-list", "--count", f"{head_before}..{head_after}"],
                             cwd=a.cwd, capture_output=True, text=True)
        commits = int(out.stdout.strip()) if out.returncode == 0 else None
    if code != 0:
        tail = "\n".join(text.splitlines()[-15:])
        print(f"[agent_run] worker failed, last lines of {log_path}:\n{tail}", file=sys.stderr)
    rec = {"task": a.task, "worker": a.worker, "model": a.model, "tag": a.tag,
           "start": start, "end": end, "seconds": round(end - start, 1), "exit": code,
           "tokens": tokens, "token_source": source, "new_commits": commits, "log": log_path}
    with open(LEDGER, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"\n[agent_run] {a.task} {a.worker}/{a.model}: {fmt_time(rec['seconds'])}, "
          f"{total(tokens):,} tokens ({source or 'not reported'}), exit {code}, "
          f"{'?' if commits is None else commits} new commits", file=sys.stderr)
    if code == 0 and commits == 0:
        print(f"[agent_run] WARNING: {a.task} exited 0 but made no commits. Treat it as not done.", file=sys.stderr)
    sys.exit(code)


def git_head(cwd):
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else None


def parse_ts(value):
    """Epoch seconds from a Claude Code ISO timestamp like 2026-11-22T23:01:02.123Z."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def lead_usage(path, since=None):
    """Sum tokens from a Claude Code session log, by model, from `since` on.
    Dedupes repeated message ids."""
    by_model, seen = {}, set()
    with open(path) as f:
        for line in f:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            msg = obj.get("message") or {}
            u = msg.get("usage")
            if obj.get("type") != "assistant" or not isinstance(u, dict):
                continue
            if since is not None:
                ts = parse_ts(obj.get("timestamp"))
                if ts is None or ts < since:
                    continue
            key = (msg.get("id"), obj.get("requestId"))
            if key in seen:
                continue
            seen.add(key)
            t = by_model.setdefault(msg.get("model", "unknown"), empty())
            t["fresh_input"] += u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
            t["cached_input"] += u.get("cache_read_input_tokens", 0)
            t["output"] += u.get("output_tokens", 0)
    return by_model


def find_lead_session():
    enc = re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())
    files = glob.glob(os.path.expanduser(f"~/.claude/projects/{enc}/*.jsonl"))
    return max(files, key=os.path.getmtime) if files else None


def fmt_time(s):
    s = int(round(s))
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s"


def union_seconds(intervals):
    total_s, cur = 0.0, None
    for s, e in sorted(intervals):
        if cur and s <= cur[1]:
            cur[1] = max(cur[1], e)
        else:
            if cur:
                total_s += cur[1] - cur[0]
            cur = [s, e]
    if cur:
        total_s += cur[1] - cur[0]
    return total_s


def table(rows, headers):
    w = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    line = lambda r: "| " + " | ".join(str(x).ljust(n) for x, n in zip(r, w)) + " |"
    return "\n".join([line(headers), "|" + "|".join("-" * (n + 2) for n in w) + "|"] + [line(r) for r in rows])


def work(t):
    """New input + output: the tokens that reflect work. Cached input is the same
    context re-read every turn, so it is shown but kept out of comparisons."""
    if not t:
        return 0
    return t["fresh_input"] + t["output"] + t.get("total_only", 0)


def n(x):
    return f"{x:,}"


def cmd_start(a):
    os.makedirs("tmp", exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({"event": "mission_start", "time": time.time(), "mission": a.mission}) + "\n")
    print(f"[agent_run] mission start recorded at {datetime.now():%Y-%m-%d %H:%M}", file=sys.stderr)


def cmd_report(a):
    if not os.path.exists(LEDGER):
        sys.exit(f"no runs yet ({LEDGER} missing)")
    records = [json.loads(l) for l in open(LEDGER) if l.strip()]
    starts = [r["time"] for r in records if r.get("event") == "mission_start"]
    runs = [r for r in records if not r.get("event")]
    main = [r for r in runs if not r.get("tag")]
    if not runs:
        sys.exit("no worker runs recorded yet")

    since, since_note = None, ""
    if starts:
        since = starts[-1]
        main = [r for r in main if r["start"] >= since]
        runs = [r for r in runs if r["start"] >= since]
        since_note = f"from mission start, {datetime.fromtimestamp(since):%Y-%m-%d %H:%M}"
    elif runs:
        since = min(r["start"] for r in runs)
        since_note = (f"from the first worker run, {datetime.fromtimestamp(since):%Y-%m-%d %H:%M}. "
                      "The lead's planning before that isn't counted; run `agent_run.py start` at the "
                      "beginning of a mission to include it")

    agg = {}
    for r in main:
        g = agg.setdefault((r["worker"], r["model"]), {"runs": 0, "sec": 0.0, "tok": empty()})
        g["runs"] += 1
        g["sec"] += r["seconds"]
        for f in ("fresh_input", "cached_input", "output", "total_only"):
            g["tok"][f] = g["tok"].get(f, 0) + (r["tokens"] or {}).get(f, 0)

    lead_path = None if a.no_lead else (a.lead_session or find_lead_session())
    lead = lead_usage(lead_path, since) if lead_path and os.path.exists(lead_path) else {}

    busy = sum(r["seconds"] for r in main)
    wall = union_seconds([(r["start"], r["end"]) for r in main])
    worker_work = sum(work(g["tok"]) for g in agg.values())
    lead_work = sum(work(t) for t in lead.values())

    out = ["# Agent runs report", ""]
    if since_note:
        out += [f"Counted {since_note}.", ""]

    # Summary
    rows = []
    for (w, m), g in sorted(agg.items()):
        rows.append([f"Worker: {w}", m, g["runs"], fmt_time(g["sec"]), n(g["tok"]["fresh_input"] + g["tok"].get("total_only", 0)),
                     n(g["tok"]["output"]), n(work(g["tok"])), n(g["tok"]["cached_input"])])
    for m, t in sorted(lead.items()):
        rows.append(["Lead", m, "", "not measured", n(t["fresh_input"]), n(t["output"]), n(work(t)), n(t["cached_input"])])
    out += ["## Summary", "",
            table(rows, ["Who", "Model", "Runs", "Agent time", "New input", "Output", "Work tokens", "Cached input"]), "",
            "Work tokens = new input + output. Cached input is the same context re-read on every turn: "
            "it is counted by the tools and much cheaper, but it isn't work, so it stays out of the share below.", ""]
    if worker_work and lead_work:
        out.append(f"- **Share of work tokens done by workers: {worker_work / (worker_work + lead_work):.0%}** "
                   f"({n(worker_work)} of {n(worker_work + lead_work)})")
    out += [f"- Worker time, added up: {fmt_time(busy)}",
            f"- Wall clock while any worker was running: {fmt_time(wall)}",
            f"- Saved by running workers in parallel: {fmt_time(busy - wall)}" + (f" ({(busy - wall) / busy:.0%})" if busy else "")]
    missing = [r["task"] for r in main if not r["tokens"]]
    if missing:
        out.append(f"- Tokens not reported for: {', '.join(missing)} (their time is still counted)")
    idle = [r["task"] for r in main if r.get("new_commits") == 0 and r["exit"] == 0]
    if idle:
        out.append(f"- Exited 0 but made no commits (no work done): {', '.join(idle)}")
    out.append("")

    # Per run
    def commits_cell(r):
        c = r.get("new_commits")
        if c is None:
            return "?"
        return "0 (no work done)" if c == 0 and r["exit"] == 0 else str(c)
    rows = []
    for r in runs:
        t = r["tokens"]
        rows.append([r["task"], r["worker"], r["model"], r.get("tag") or "", fmt_time(r["seconds"]),
                     n(t["fresh_input"] + t.get("total_only", 0)) if t else "not reported",
                     n(t["output"]) if t else "", n(work(t)) if t else "",
                     n(t["cached_input"]) if t else "", r["exit"], commits_cell(r)])
    out += ["## Runs", "", table(rows, ["Task", "Worker", "Model", "Tag", "Time", "New input", "Output",
                                         "Work tokens", "Cached input", "Exit", "New commits"]), ""]

    tagged = [r for r in runs if r.get("tag")]
    rows = []
    for t in tagged:
        base = next((r for r in main if r["task"] == t["task"]), None)
        if base:
            rows.append([t["task"], f"{base['model']} vs {t['model']} ({t['tag']})",
                         f"{fmt_time(base['seconds'])} vs {fmt_time(t['seconds'])}",
                         f"{n(work(base['tokens']))} vs {n(work(t['tokens']))}"])
    if rows:
        out += ["## Same task, different model", "", table(rows, ["Task", "Models", "Time", "Work tokens"]), ""]

    if lead_path:
        out += [f"Lead source: `{lead_path}`", ""]

    report = "\n".join(out)
    path = os.path.join("tmp", "agent-runs-report.md")
    with open(path, "w") as f:
        f.write(report + "\n")
    print(report)
    print(f"(saved to {path})", file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--task", required=True)
    r.add_argument("--worker", required=True, help="codex, agy, claude")
    r.add_argument("--model", required=True)
    r.add_argument("--tag", default="", help="label a comparison run, e.g. baseline")
    r.add_argument("--cwd", default=None, help="worktree to run in")
    r.add_argument("command", nargs=argparse.REMAINDER)
    st = sub.add_parser("start", help="record the start of a mission")
    st.add_argument("--mission", default="")
    rp = sub.add_parser("report")
    rp.add_argument("--lead-session", help="Claude Code session .jsonl (default: latest for this folder)")
    rp.add_argument("--no-lead", action="store_true")
    a = p.parse_args()
    if a.cmd == "run":
        if a.command and a.command[0] == "--":
            a.command = a.command[1:]
        cmd_run(a)
    elif a.cmd == "start":
        cmd_start(a)
    else:
        cmd_report(a)


if __name__ == "__main__":
    main()
