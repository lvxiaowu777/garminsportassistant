"""把评测 JSONL 转成可读报告（含每题模型回答）。

用法：
    python scripts/report_eval.py                      # 最新一次
    python scripts/report_eval.py scripts/results/xxx.jsonl
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "scripts" / "results"


def latest() -> Path:
    files = sorted(RESULTS.glob("eval60-*.jsonl"))
    if not files:
        raise SystemExit("还没有评测结果，先跑 scripts/eval_60.py")
    return files[-1]


def cell(text: str, limit: int = 60) -> str:
    text = (text or "").replace("|", "\\|").replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else latest()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    n = len(rows)
    passed = sum(1 for r in rows if r["pass"])
    lat = sorted(r["latency_ms"] for r in rows)

    out = path.with_suffix(".review.md")
    lines = [
        f"# 评测逐题记录（{path.stem}）",
        "",
        f"- 通过：**{passed}/{n}（{round(100 * passed / n, 1)}%）**",
        f"- 平均轮次：{round(sum(r['turns_used'] for r in rows) / n, 2)}",
        f"- 总延迟 P50：{lat[n // 2]}ms",
        "",
        "## 总览",
        "",
        "| id | 类别 | 问题 | 期望工具 | 实际工具 | 结果 |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        exp = r.get("expect") or {}
        must = "/".join(exp.get("must") or []) or ("或".join(exp.get("must_any") or []) or "-")
        lines.append(
            f"| {r['id']} | {r['cat']} | {cell(r['turns'][-1], 40)} | {must} | "
            f"{'/'.join(r['tools']) or '-'} | {'✅' if r['pass'] else '❌'} |"
        )

    lines += ["", "## 逐题详情", ""]
    for r in rows:
        exp = r.get("expect") or {}
        lines += [
            f"### {r['id']}　{'✅ 通过' if r['pass'] else '❌ 失败'}",
            "",
            f"**问题**：{' → '.join(r['turns'])}",
            "",
            f"- 期望工具：{exp.get('must') or '-'}（或 {exp.get('must_any') or '-'}）",
            f"- 禁止工具：{exp.get('forbid') or '-'}",
            f"- 期望事实：{exp.get('facts') or '-'}　任一：{exp.get('facts_any') or '-'}",
            f"- 实际工具：{r['tools'] or '-'}",
            f"- 轮次 {r['turns_used']}　延迟 {r['latency_ms']}ms　"
            f"token { (r.get('usage') or {}).get('prompt_tokens', '?') }/"
            f"{(r.get('usage') or {}).get('completion_tokens', '?')}",
            "",
            "```text",
            (r.get("text") or "").strip()[:600],
            "```",
            "",
        ]

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("report:", out)


if __name__ == "__main__":
    main()
