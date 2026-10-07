"""Build the static demo page from a run folder: python3 build_site.py [runs/<ts>]  ->  site/index.html

Everything shown comes from the run: the agent's streamed events, the attempts it saved, the verifier
reports for each attempt and the videos rendered from the same simulator."""
from __future__ import annotations

import difflib
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def latest_run() -> Path:
    runs = sorted(p for p in (ROOT / "runs").iterdir() if (p / "remote").exists())
    return runs[-1]


def read(p: Path, limit: int = 200000) -> str:
    return p.read_text(errors="replace")[:limit] if p.exists() else ""


# ---------------------------------------------------------------- agent events

def clean_label(tool: str, label, args) -> str:
    """Short human label for a tool call: the shell command it ran, or the file it touched."""
    args = args if isinstance(args, dict) else {}
    code = args.get("code") or ""
    cmds = re.findall(r"terminal\(\s*(['\"])(.+?)\1", code)
    if cmds:
        return " ; ".join(c for _, c in cmds)
    if args.get("command"):
        return str(args["command"]).replace("cd ~/cnc; ", "").replace("cd ~/cnc && ", "")
    if args.get("path"):
        return str(args["path"]).replace("/home/node/", "~/")
    if isinstance(label, (dict, list)):
        label = json.dumps(label)
    return str(label or "")


def classify(tool: str, text: str) -> str:
    if tool in ("read_file", "search_files", "skill_view"):
        return "read"
    if tool in ("write_file", "patch", "edit_file"):
        return "write"
    if re.search(r"\bbash\s+(~/cnc/)?verify\.sh", text):
        return "verify"
    if re.search(r"\bbash\s+(~/cnc/)?attempt\.sh", text):
        return "attempt"
    if re.search(r"(cat\s*>|>\s*|sed -i|open\().*\bgen\.py", text):
        return "write"
    return "run"


def reasoning_snippet(text: str) -> str:
    text = text.strip()
    m = re.match(r"\*\*(.+?)\*\*\s*(.*)", text, re.S)
    title, body = (m.group(1), m.group(2)) if m else ("", text)
    body = re.sub(r"\s+", " ", body).strip()
    first = re.split(r"(?<=[.!?])\s", body, maxsplit=1)[0] if body else ""
    if len(first) > 170:
        first = first[:167].rsplit(" ", 1)[0] + "…"
    return (title + (": " if title and first else "") + first).strip()


def events(run: Path):
    out, finals = [], []
    f = run / "events.jsonl"
    if not f.exists():
        return out, ""
    t0, think, think_t = None, [], None

    def flush():
        nonlocal think, think_t
        s = reasoning_snippet("".join(think))
        if s:
            out.append({"t": think_t, "kind": "reasoning", "name": "thinking", "arg": s, "res": ""})
        think, think_t = [], None

    for line in f.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        t0 = t0 or r["t"]
        t = round(r["t"] - t0)
        e, d = r.get("event") or "", r.get("data") or {}
        if e == "response.reasoning.delta":
            if think_t is None:
                think_t = t
            think.append(d.get("text") or "")
        elif e == "response.output_text.delta":
            finals.append(d.get("text") or "")
        elif e == "response.tool_call.started":
            flush()
            tool = str(d.get("tool") or "tool")
            arg = clean_label(tool, d.get("label"), d.get("arguments"))
            out.append({"t": t, "kind": "started", "name": tool, "arg": arg[:400], "res": "",
                        "cls": classify(tool, arg)})
    flush()
    return out, "".join(finals).strip()


def final_text(run: Path) -> str:
    m = json.loads((run / "manifest.json").read_text()) if (run / "manifest.json").exists() else {}
    d = m.get("agent_final") or {}
    d = d.get("response", d) if isinstance(d, dict) else {}
    return d.get("output_text") or d.get("text") or ""


# ---------------------------------------------------------------- verifier report -> plain words

def nc_line(nc_lines: list[str], ref: str):
    m = re.search(r"(\d+)\s*$", ref)
    if not m:
        return None, ""
    n = int(m.group(1))
    return n, nc_lines[n - 1].strip() if 0 < n <= len(nc_lines) else ""


def fmt_in(v) -> str:
    return f"{float(v):.3f} in"


def plain(fail: str, nc_lines: list[str]) -> dict:
    """Turn one verifier failure line into {kind, title, detail, line, code, raw}."""
    refs = re.findall(r"[\w.]+\.nc:(\d+)", fail)
    line = int(refs[0]) if refs else None
    code = nc_lines[line - 1].strip() if line and line <= len(nc_lines) else ""
    m = re.search(r"`([^`]+)`", fail)
    code = m.group(1) if m else code
    xy = re.search(r"XYZ? \[([^\]]+)\]", fail)
    where = ""
    if xy:
        nums = [float(x) for x in xy.group(1).split(",")]
        where = " at " + " ".join(f"{a}{v:.3f}" for a, v in zip("XYZ", nums))
    mx = re.search(r"max ([\d.]+)", fail) or re.search(r"worst ([\d.]+)", fail)
    depth = fmt_in(mx.group(1)) if mx else ""
    cnt = re.search(r": (\d+) line\(s\)", fail)
    n = f"{cnt.group(1)} line{'s' if cnt.group(1) != '1' else ''}" if cnt else ""
    voxel = not fail.startswith(("stock_sim", "nc_lint"))
    if "gouge" in fail:
        if voxel:
            k, title = "gouge", f"3D voxel check: cut {depth} into the part" + where
            detail = "A second, independent 3D check flags the same NC line."
        else:
            k, title = "gouge", f"Gouge: the tool cut up to {depth} into the finished part" + where
            detail = "Material that belongs to the part was removed. On a machine this part is scrap."
    elif "leftover" in fail:
        k, title = "leftover", f"Left {depth} of material that should be gone" + where
        detail = "The part would be out of tolerance; a feature was not fully machined."
    elif "rapid_through_stock" in fail:
        k, title = "rapid", f"Rapid move (G00) plowed {depth} through stock" + (f" on {n}" if n else "")
        detail = "A full-speed positioning move hits metal: crash risk."
    elif "holder_collision" in fail:
        k, title = "holder", f"Tool holder or shank hit material ({depth})" + (f" on {n}" if n else "")
        detail = "The tool is reaching deeper than its holder allows: crash risk."
    elif "beyond_flute" in fail:
        k, title = "flute", f"Cut {depth} deeper than the tool's flute length"
        detail = "The non-cutting shank would rub the wall."
    elif "thread_tool_in_solid" in fail:
        k, title = "thread", "Tap entered solid metal (hole not drilled first)"
        detail = "Broken tap on a real machine."
    elif fail.startswith("nc_lint"):
        mm = re.match(r"nc_lint ([\w-]+): (.*?)(?: \([\w.]+:\d+.*\))?$", fail)
        k, title = "lint", "G-code check: " + (mm.group(2) if mm else fail)
        detail = f"Rule {mm.group(1)}" if mm else ""
    elif "missing" in fail:
        k, title, detail = "tools", "Program calls a tool that is not in the crib", ""
    else:
        k, title, detail = "other", fail, ""
    return {"kind": k, "title": title, "detail": detail, "line": line, "code": code, "raw": fail,
            "voxel": voxel}


def diff(a: str, b: str, ctx: int = 2) -> dict:
    """Compact unified line diff of a -> b as rows {t: ' '|'-'|'+'|'@', s, n}."""
    al, bl = a.splitlines(), b.splitlines()
    rows, add, rem = [], 0, 0
    for g in difflib.SequenceMatcher(None, al, bl, autojunk=False).get_grouped_opcodes(ctx):
        i1, j1 = g[0][1], g[0][3]
        rows.append({"t": "@", "s": f"line {j1 + 1}", "n": None})
        for tag, a1, a2, b1, b2 in g:
            if tag == "equal":
                rows += [{"t": " ", "s": al[i], "n": b1 + (i - a1) + 1} for i in range(a1, a2)]
            if tag in ("replace", "delete"):
                rows += [{"t": "-", "s": al[i], "n": None} for i in range(a1, a2)]
                rem += a2 - a1
            if tag in ("replace", "insert"):
                rows += [{"t": "+", "s": bl[j], "n": j + 1} for j in range(b1, b2)]
                add += b2 - b1
    return {"rows": rows[:400], "add": add, "rem": rem, "truncated": len(rows) > 400}


# ---------------------------------------------------------------- main

def main():
    run = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_run()
    remote = next((run / "remote").glob("*"), run / "remote")
    site = ROOT / "site"
    if site.exists():
        shutil.rmtree(site)
    (site / "media").mkdir(parents=True)
    attempts = []
    for a in sorted((remote / "attempts").glob("[0-9][0-9]")):
        n = a.name
        rep = {}
        if (a / "check_out" / "report.json").exists():
            rep = json.loads((a / "check_out" / "report.json").read_text())
        fails = [f for s in rep.get("setups", []) for f in s.get("failures", [])]
        fails += (rep.get("voxel") or {}).get("failures") or []
        warns = [w for s in rep.get("setups", []) for w in s.get("warnings", [])]
        vid = remote / "out" / "videos" / f"attempt_{n}.mp4"
        if vid.exists():
            shutil.copy(vid, site / "media" / vid.name)
        maps = []
        for png in sorted((a / "check_out").glob("*.png")):
            shutil.copy(png, site / "media" / f"attempt_{n}_{png.name}")
            maps.append(f"media/attempt_{n}_{png.name}")
        nc = read(a / "1001.nc")
        nc_lines = nc.splitlines()
        stats = [lf.get("stats") or {} for s in rep.get("setups", []) for lf in s.get("nc_lint", [])]
        cycle = round(sum(st.get("cycle_time_min_est") or 0 for st in stats), 2)
        moves = sum(st.get("moves") or 0 for st in stats)
        tools = sorted({t for st in stats for t in st.get("tools_used") or []})
        caught = [plain(f, nc_lines) for f in fails]
        attempts.append({"n": n, "status": rep.get("status", "unknown"), "fails": fails[:6],
                         "caught": caught[:8], "warnings": warns[:4],
                         "video": f"media/attempt_{n}.mp4" if vid.exists() else None,
                         "maps": maps, "nc": nc, "gen": read(a / "gen.py", 60000),
                         "time": cycle, "moves": moves, "tools": tools,
                         "runtime": rep.get("runtime_s")})
    for i in range(1, len(attempts)):
        p, c = attempts[i - 1], attempts[i]
        c["diff_gen"] = diff(p["gen"], c["gen"]) if p["gen"] or c["gen"] else None
        c["diff_nc"] = diff(p["nc"], c["nc"]) if p["nc"] or c["nc"] else None
    summary = read(remote / "out" / "summary.md")
    ev, streamed_final = events(run)
    manifest = json.loads((run / "manifest.json").read_text()) if (run / "manifest.json").exists() else {}
    data = {"attempts": attempts, "events": ev, "summary": summary,
            "final": final_text(run) or streamed_final,
            "model": manifest.get("model"), "seconds": manifest.get("seconds"), "instance": manifest.get("instance"),
            "tool_calls": sum(1 for e in ev if e["kind"] == "started"),
            "spec": read(remote / "job" / "spec.md"), "drawing": None, "monid": None}
    for src in (remote / "job" / "drawing.png", ROOT / "cnc" / "job" / "drawing.png"):
        if src.exists():
            shutil.copy(src, site / "media" / "drawing.png")
            data["drawing"] = "media/drawing.png"
            break
    cl = ROOT / "sponsors_out" / "claims.json"
    data["claims"] = json.loads(cl.read_text()) if cl.exists() else {}
    mq = ROOT / "sponsors_out" / "monid_quote.json"
    if mq.exists():
        q = json.loads(mq.read_text())
        data["monid"] = {k: q.get(k) for k in ("per_part_material_usd", "qty_total_material_usd", "qty", "source_url",
                                                "source_title", "fetched_at", "monid_spend_usd", "query")}
        data["monid"]["tool"] = (q.get("tool") or {}).get("providerName")
    tpl = (ROOT / "site_template.html").read_text()
    (site / "index.html").write_text(tpl.replace("/*__DATA__*/null", json.dumps(data).replace("</", "<\\/")))
    print(f"site/index.html  attempts={len(attempts)} events={len(ev)} statuses={[a['status'] for a in attempts]}")


if __name__ == "__main__":
    main()
