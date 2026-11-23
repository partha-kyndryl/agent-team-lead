#!/usr/bin/env python3
"""Run coding-agent workers and measure them: wall time and tokens.

  agent_run.py run --task T1 --worker codex --model gpt-5-mini [--tag baseline] [--cwd DIR] -- <command...>
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


def lead_usage(path):
    """Sum tokens from a Claude Code session log, by model. Dedupes repeated message ids."""
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


def cmd_report(a):
    if not os.path.exists(LEDGER):
        sys.exit(f"no runs yet ({LEDGER} missing)")
    runs = [json.loads(l) for l in open(LEDGER) if l.strip()]
    main = [r for r in runs if not r.get("tag")]
    out = ["# Agent runs report", ""]

    def commits_cell(r):
        c = r.get("new_commits")
        if c is None:
            return "?"
        return f"0 (no work done)" if c == 0 and r["exit"] == 0 else str(c)
    rows = [[r["task"], r["worker"], r["model"], r.get("tag") or "", fmt_time(r["seconds"]),
             f"{total(r['tokens']):,}" if r["tokens"] else "not reported", r["exit"], commits_cell(r)] for r in runs]
    out += ["## Runs", "", table(rows, ["Task", "Worker", "Model", "Tag", "Time", "Tokens", "Exit", "New commits"]), ""]

    agg = {}
    for r in main:
        k = (r["worker"], r["model"])
        g = agg.setdefault(k, {"runs": 0, "sec": 0.0, "tok": empty()})
        g["runs"] += 1
        g["sec"] += r["seconds"]
        for f in ("fresh_input", "cached_input", "output"):
            g["tok"][f] += (r["tokens"] or {}).get(f, 0)
        g["tok"]["total_only"] = g["tok"].get("total_only", 0) + (r["tokens"] or {}).get("total_only", 0)
    rows = [[w, m, g["runs"], fmt_time(g["sec"]), f"{g['tok']['fresh_input']:,}",
             f"{g['tok']['cached_input']:,}", f"{g['tok']['output']:,}", f"{total(g['tok']):,}"]
            for (w, m), g in sorted(agg.items())]
    out += ["## Workers by model", "", table(rows, ["Worker", "Model", "Runs", "Agent time", "Input", "Cached input", "Output", "Total"]), ""]

    busy = sum(r["seconds"] for r in main)
    wall = union_seconds([(r["start"], r["end"]) for r in main])
    worker_tokens = sum(total(r["tokens"]) for r in main)
    out += ["## Time", "",
            f"- Agent time, added up: {fmt_time(busy)}",
            f"- Wall clock while any worker was running: {fmt_time(wall)}",
            f"- Saved by running in parallel: {fmt_time(busy - wall)}" + (f" ({(busy - wall) / busy:.0%})" if busy else ""),
            ""]

    lead_path = None if a.no_lead else (a.lead_session or find_lead_session())
    lead_total = 0
    if lead_path and os.path.exists(lead_path):
        lead = lead_usage(lead_path)
        lead_total = sum(total(t) for t in lead.values())
        rows = [[m, f"{t['fresh_input']:,}", f"{t['cached_input']:,}", f"{t['output']:,}", f"{total(t):,}"] for m, t in sorted(lead.items())]
        out += ["## Lead (this Claude Code session)", "", f"Source: `{lead_path}`", "",
                table(rows, ["Model", "Input", "Cached input", "Output", "Total"]), ""]

    out += ["## Where the tokens went", "",
            f"- Workers (cheaper models): {worker_tokens:,}",
            f"- Lead (heavy model): {lead_total:,}" if lead_total else "- Lead: not measured",
            ]
    if lead_total and worker_tokens:
        out.append(f"- Share of all tokens kept off the lead's model: {worker_tokens / (worker_tokens + lead_total):.0%}")
    missing = [r["task"] for r in main if not r["tokens"]]
    if missing:
        out.append(f"- Tokens not reported for: {', '.join(missing)} (time is still counted)")
    out.append("")

    tagged = [r for r in runs if r.get("tag")]
    if tagged:
        rows = []
        for t in tagged:
            base = next((r for r in main if r["task"] == t["task"]), None)
            if not base:
                continue
            rows.append([t["task"], f"{base['model']} vs {t['model']} ({t['tag']})",
                         f"{fmt_time(base['seconds'])} vs {fmt_time(t['seconds'])}",
                         f"{total(base['tokens']):,} vs {total(t['tokens']):,}"])
        if rows:
            out += ["## Same task, different model", "", table(rows, ["Task", "Models", "Time", "Tokens"]), ""]

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
    rp = sub.add_parser("report")
    rp.add_argument("--lead-session", help="Claude Code session .jsonl (default: latest for this folder)")
    rp.add_argument("--no-lead", action="store_true")
    a = p.parse_args()
    if a.cmd == "run":
        if a.command and a.command[0] == "--":
            a.command = a.command[1:]
        cmd_run(a)
    else:
        cmd_report(a)


if __name__ == "__main__":
    main()
