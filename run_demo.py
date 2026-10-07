"""Autonomous CNC programmer on Agent37.

    hsec exec --only AGENT37_API_KEY -- python3 run_demo.py [--instance ID] [--keep] [--skip-agent]

1. Creates (or reuses) an Agent37 Hermes instance with a capped budget.
2. Uploads the shop workspace: RFQ spec, part model, tool crib, the Helios stock-removal verifier.
3. Installs the verifier's Python deps and ffmpeg on the instance.
4. Sends the agent the brief and streams its work (tool calls, reasoning) to runs/<ts>/events.jsonl.
5. Renders a material-removal video of every attempt the agent saved, on the instance, and downloads
   videos, reports, programs and the agent's summary into runs/<ts>/ for the viewer.
The API key is read from the environment only; it is never written to disk or sent to the browser.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
API = "https://api.agent37.com/v1"
KEY = os.environ.get("AGENT37_API_KEY", "")
REMOTE = "/home/node/cnc"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def req(method, url, body=None, headers=None, timeout=900, raw=False):
    h = dict(headers or {})
    data = None
    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        else:
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
    r = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            b = resp.read()
            return b if raw else (json.loads(b) if b.strip() else {})
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {url} -> {e.code}: {e.read()[:600].decode(errors='replace')}") from None


def hosting(method, path, body=None, timeout=900):
    return req(method, API + path, body, {"Authorization": f"Bearer {KEY}"}, timeout)


class Instance:
    def __init__(self, iid):
        self.id = iid
        self.url = f"https://{iid}.agent37.app"
        self.h = {"X-Agent37-Key": KEY}

    def get(self, path, timeout=60, raw=False):
        return req("GET", self.url + path, None, self.h, timeout, raw)

    def put_file(self, remote, data: bytes):
        q = urllib.parse.urlencode({"path": remote})
        return req("PUT", f"{self.url}/v1/files/content?{q}", data,
                   {**self.h, "Content-Type": "application/octet-stream"}, 120)

    def archive(self, remote) -> bytes:
        q = urllib.parse.urlencode({"path": remote})
        return req("GET", f"{self.url}/v1/files/archive?{q}", None, self.h, 600, raw=True)

    def exec(self, command, root=False, timeout=800):
        body = {"command": command}
        if root:
            body["user"] = "root"
        return hosting("POST", f"/instances/{self.id}/exec", body, timeout)

    def stream(self, body, sink, response_id=None):
        """POST /v1/responses with stream=true (or GET .../stream to reattach); yields parsed SSE events."""
        if response_id:
            r = urllib.request.Request(f"{self.url}/v1/responses/{response_id}/stream", method="GET",
                                       headers={**self.h, "Accept": "text/event-stream"})
        else:
            r = urllib.request.Request(self.url + "/v1/responses", data=json.dumps({**body, "stream": True}).encode(),
                                       method="POST", headers={**self.h, "Content-Type": "application/json",
                                                               "Accept": "text/event-stream"})
        with urllib.request.urlopen(r, timeout=3600) as resp:
            ev, data = None, []
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].strip())
                elif line == "":
                    if data:
                        try:
                            payload = json.loads("\n".join(data))
                        except json.JSONDecodeError:
                            payload = {"raw": "\n".join(data)}
                        rec = {"t": time.time(), "event": ev or payload.get("type"), "data": payload}
                        sink.write(json.dumps(rec) + "\n")
                        sink.flush()
                        yield rec
                    ev, data = None, []


def create_instance():
    body = {"template": "agent37-hermes", "resources": {"cpu": 4, "memory": 8},
            "budget": {"credit_micros": 3_000_000}, "user": "hackathon-cnc-demo"}
    try:
        inst = hosting("POST", "/instances", body, 600)
    except RuntimeError as e:
        if "tier_limit" not in str(e):
            raise
        body.pop("resources")
        inst = hosting("POST", "/instances", body, 600)
    log("instance", inst["id"], inst["status"], inst.get("resources"))
    return Instance(inst["id"])


def wait_healthy(inst, limit=300):
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            h = inst.get("/v1/health", 20)
            if h.get("healthy"):
                log("healthy after", round(time.time() - t0), "s")
                return
        except Exception:
            pass
        time.sleep(3)
    raise RuntimeError("instance never became healthy")


def upload_workspace(inst):
    files = []
    for p in sorted((ROOT / "vendor" / "checks").rglob("*.py")):
        files.append((p, f"{REMOTE}/vendor/checks/{p.relative_to(ROOT / 'vendor' / 'checks')}"))
    for name in ("part.step", "tools.json", "job.json", "shop.md", "drawing.png", "drawing.pdf"):
        files.append((ROOT / "cnc" / "job" / name, f"{REMOTE}/job/{name}"))
    for name in ("AGENT_BRIEF.md", "verify.sh", "attempt.sh", "render_video.py"):
        files.append((ROOT / "cnc" / name, f"{REMOTE}/{name}"))
    for local, remote in files:
        inst.put_file(remote, local.read_bytes())
    log("uploaded", len(files), "files")


SETUP = r"""
set -e
cd ~/cnc && chmod +x verify.sh attempt.sh
if [ ! -x .venv/bin/python ]; then
  (command -v uv >/dev/null && uv venv -q .venv) || python3 -m venv .venv
fi
if command -v uv >/dev/null; then
  uv pip install -q --python .venv/bin/python numpy scipy numba "cadquery-ocp==7.9.3.1.1" trimesh matplotlib
else
  .venv/bin/pip install -q numpy scipy numba "cadquery-ocp==7.9.3.1.1" trimesh matplotlib
fi
PYTHONPATH=vendor .venv/bin/python -c "import checks.run, numba, OCP; print('verifier ready')"
"""


def setup_instance(inst):
    r = inst.exec("command -v ffmpeg || (apt-get -qq update && DEBIAN_FRONTEND=noninteractive apt-get -qq install -y "
                  "ffmpeg fonts-dejavu-core >/dev/null && echo ffmpeg-installed)", root=True)
    log("ffmpeg:", r["exit_code"], r["stdout"][-200:], r["stderr"][-300:])
    r = inst.exec("python3 --version; command -v uv; " + SETUP)
    log("python deps:", r["exit_code"], r["stdout"][-400:], r["stderr"][-800:])
    if r["exit_code"] != 0:
        raise RuntimeError("verifier setup failed")


def pick_model(inst):
    try:
        ms = inst.get("/v1/models", 30)
        ids = [m.get("id") for m in ms.get("data", ms if isinstance(ms, list) else [])]
        for want in ("gpt-6.1-sol", "gpt-6", "gpt-5", "gpt", "openai"):
            for i in ids:
                if i and want in i.lower():
                    return i, ids
        return None, ids
    except Exception as e:
        return None, [f"error: {e}"]


def run_agent(inst, run_dir, model, attach=None):
    prompt = (Path(ROOT / "cnc" / "AGENT_BRIEF.md").read_text()
              + "\n\nStart now. Work in ~/cnc. The brief is also at ~/cnc/AGENT_BRIEF.md.")
    body = {"input": prompt, "files": [f"{REMOTE}/job/drawing.png", f"{REMOTE}/job/shop.md", f"{REMOTE}/job/tools.json"],
            "reasoning_effort": "medium", "metadata": {"demo": "cnc"}}
    if model:
        body["model"] = model
    final = None
    with open(run_dir / "events.jsonl", "w") as sink:
        for rec in inst.stream(body, sink, attach):
            e, d = rec["event"] or "", rec["data"]
            if e == "response.created":
                log("response", d.get("id"), "session", d.get("session_id"))
            if e == "response.tool_call.started":
                arg = d.get("label") or json.dumps(d.get("arguments") or "")
                log(f"[tool] {d.get('tool')}: {str(arg)[:160]}")
            elif e in ("response.completed", "response.failed"):
                final = d
                log(f"[{e}] status={d.get('status') or d.get('response', {}).get('status')}")
    return final


RENDER = r"""
cd ~/cnc
rm -f out/RENDER_DONE
mkdir -p out/videos
for a in attempts/[0-9][0-9]; do
  n=$(basename $a)
  rm -rf /tmp/vjob && cp -r job /tmp/vjob && cp $a/1001.nc /tmp/vjob/1001.nc
  st=$(grep -q '"status": "pass"' $a/check_out/report.json 2>/dev/null && echo PASS || echo FAIL)
  .venv/bin/python render_video.py /tmp/vjob /tmp/vframes_$n --title "Attempt $n" \
     --subtitle "Agent-written program, swept through the stock with the real tool shapes" --frames 150 --hold 50 > /tmp/v_$n.json
  ffmpeg -loglevel error -y -framerate 30 -i /tmp/vframes_$n/frames/f%05d.png -c:v libx264 -pix_fmt yuv420p \
     -crf 20 out/videos/attempt_$n.mp4
  cp /tmp/v_$n.json out/videos/attempt_$n.json
  echo "rendered $n ($st)"
done
touch out/RENDER_DONE
"""


def render_and_download(inst, run_dir):
    r = inst.exec("nohup bash -c '" + RENDER.replace("'", "'\"'\"'") + "' > ~/cnc/render.log 2>&1 & echo started")
    log("render:", r["stdout"].strip())
    for _ in range(120):
        time.sleep(10)
        r = inst.exec("tail -3 ~/cnc/render.log; [ -f ~/cnc/out/RENDER_DONE ] && echo RENDER-DONE")
        log("render:", r["stdout"].strip().replace("\n", " | ")[-200:])
        if "RENDER-DONE" in r["stdout"]:
            break
    download(inst, run_dir)


def download(inst, run_dir):
    data = inst.archive(REMOTE)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        keep = [m for m in tf.getmembers() if not any(x in m.name for x in ("/.venv/", "/vendor/", "__pycache__"))
                and not m.name.endswith((".npz",))]
        keep = [m for m in keep if (m.isfile() or m.isdir()) and not m.name.startswith("/") and ".." not in m.name]
        try:
            tf.extractall(run_dir / "remote", members=keep, filter="data")
        except TypeError:  # Python < 3.12: members already restricted to plain files/dirs inside the tree
            tf.extractall(run_dir / "remote", members=keep)
    log("downloaded workspace to", run_dir / "remote")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance")
    ap.add_argument("--keep", action="store_true", help="keep the instance after the run")
    ap.add_argument("--skip-agent", action="store_true")
    ap.add_argument("--skip-setup", action="store_true")
    ap.add_argument("--model", help="model id for the agent turn (default: OpenAI Sol 6.1)")
    ap.add_argument("--tag", default="")
    ap.add_argument("--download-only", action="store_true", help="with --render-only: skip rendering, just download")
    ap.add_argument("--render-only", help="re-render + download using events/manifest from this run dir")
    ap.add_argument("--attach", help="reattach to a running response id instead of starting a turn")
    a = ap.parse_args()
    if not KEY:
        sys.exit("AGENT37_API_KEY missing: run with  hsec exec --only AGENT37_API_KEY -- python3 run_demo.py")
    run_dir = ROOT / "runs" / (time.strftime("%Y%m%d-%H%M%S") + (f"-{a.tag}" if a.tag else ""))
    run_dir.mkdir(parents=True)
    t0 = time.time()
    inst = Instance(a.instance) if a.instance else create_instance()
    manifest = {"instance": inst.id, "started": time.strftime("%FT%T%z")}
    if a.render_only:
        src = Path(a.render_only)
        for f in ("events.jsonl", "manifest.json"):
            if (src / f).exists():
                (run_dir / f).write_text((src / f).read_text())
        manifest = json.loads((src / "manifest.json").read_text())
        if a.download_only:
            download(inst, run_dir)
        else:
            inst.put_file(f"{REMOTE}/render_video.py", (ROOT / "cnc" / "render_video.py").read_bytes())
            render_and_download(inst, run_dir)
        log("run dir:", run_dir)
        return
    try:
        wait_healthy(inst)
        if not a.skip_setup:
            upload_workspace(inst)
            setup_instance(inst)
        model, ids = pick_model(inst)
        model = a.model or model
        manifest.update(model=model, models_available=ids[:40])
        log("model:", model or "instance default")
        if not a.skip_agent:
            final = run_agent(inst, run_dir, model, a.attach)
            manifest["agent_final"] = final
        render_and_download(inst, run_dir)
    finally:
        manifest["seconds"] = round(time.time() - t0)
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
        if not a.keep and not a.instance:
            try:
                hosting("DELETE", f"/instances/{inst.id}")
                log("deleted instance", inst.id)
            except Exception as e:
                log("delete failed:", e)
    log("run dir:", run_dir)


if __name__ == "__main__":
    main()
