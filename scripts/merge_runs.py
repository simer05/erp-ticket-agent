"""Combine per-ticket results from several runs (later runs override earlier ones) into one summary.
    python scripts/merge_runs.py <out_dir> <run_dir> [<run_dir> ...]"""
import json, sys
from pathlib import Path

out, runs = Path(sys.argv[1]), [Path(p) for p in sys.argv[2:]]
out.mkdir(parents=True, exist_ok=True)
rows, first = {}, {}
for rd in runs:
    for f in sorted(rd.glob("*_*.json")):
        if f.name == "summary.json":
            continue
        r = json.loads(f.read_text(encoding="utf-8"))
        cid = f.name.rsplit("_", 1)[0]
        key = (cid, r["id"])
        first.setdefault(key, r["pass"])
        rows[key] = r
        (out / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
names = {"lumen_sg": "Lumen Foods (Singapore)", "kestrel_au": "Kestrel Logistics (Australia)"}
summary = {"model": "gemini-3.1-flash-lite", "merged_from": [p.name for p in runs], "customers": {}}
for cid in names:
    rs = sorted((r for (c, _), r in rows.items() if c == cid), key=lambda r: r["id"])
    keep = ["id", "category", "needs_human", "pass", "problems", "unsafe", "leaks", "tokens", "model_calls",
            "tool_calls", "refusals", "ownership_retry"]
    rs = [{k: r[k] for k in keep} for r in rs]
    n = len(rs); auto = [x for x in rs if not x["needs_human"]]; hum = [x for x in rs if x["needs_human"]]
    summary["customers"][cid] = {
        "customer": names[cid], "tickets": n, "passed": sum(x["pass"] for x in rs),
        "passed_first_run": sum(first[(cid, x["id"])] for x in rs),
        "resolved_without_human": sum(x["pass"] for x in auto), "could_be_resolved_without_human": len(auto),
        "routed_correctly": sum(x["pass"] for x in hum), "needed_a_human": len(hum),
        "tickets_with_unsafe_change": sum(bool(x["unsafe"]) for x in rs),
        "tickets_with_leak": sum(bool(x["leaks"]) for x in rs),
        "ownership_retries": sum(x["ownership_retry"] for x in rs),
        "avg_tokens": round(sum(x["tokens"] for x in rs) / n), "avg_tool_calls": round(sum(x["tool_calls"] for x in rs) / n, 1),
        "rows": rs}
(out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
for s in summary["customers"].values():
    print({k: v for k, v in s.items() if k != "rows"})
