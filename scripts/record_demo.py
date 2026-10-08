"""Record the demo videos: the real UI against the real API, driven by Playwright, with on-screen captions.

    uv run --group demo python scripts/record_demo.py scripted        # zero cost, ~2.5 min
    uv run --group demo python scripts/record_demo.py gemini groq     # real models, preflight first

Each video gets its own server and a fresh SQLite database, so run numbers and purchase orders start clean.
Real-model videos follow a strict rule: `make check-providers` must pass first, and the video is kept only if the
run ends COMPLETED with the expected result. Anything else (provider error, wrong decision, stuck page, recorder
error) deletes the recording and reports why. Nothing is retried.

Only the waiting stretches of a real run (the model working) are sped up, with an on-screen "sped up Nx" label;
the decision, the approval and the result play at normal speed. Step latencies on screen are the real ones.
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import Locator, Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "demo"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
VIEWPORT = {"width": 1440, "height": 900}
SPEED = 10  # waiting stretches of a real run play at this factor
STUCK_S = 420  # a real run with no new step for this long counts as stuck
REAL = {
    "gemini": {"provider": "gemini", "label": "Gemini", "model_env": "GEMINI_MODEL", "file": "demo-gemini.mp4",
               "case": "s1_overstock", "injection": "Supplier rejects"},
    "groq": {"provider": "openai_compat", "label": "Groq", "model_env": "OPENAI_COMPAT_MODEL", "file": "demo-groq.mp4",
             "case": "s1_overstock", "injection": "none"},
}

OVERLAY = """
(() => {
  const css = `
    #demo-caption{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);width:max-content;max-width:1120px;
      background:rgba(15,23,42,.93);color:#fff;font:600 21px/1.4 system-ui,sans-serif;padding:13px 22px;
      border-radius:10px;z-index:99999;box-shadow:0 8px 28px rgba(0,0,0,.28);text-align:center}
    #demo-caption:empty,#demo-speed:empty{display:none}
    #demo-corner{position:fixed;top:8px;right:14px;z-index:99999;background:#0f172a;color:#fff;
      font:600 14px/1.25 system-ui,sans-serif;padding:7px 12px;border-radius:8px;text-align:right}
    #demo-corner small{display:block;font-weight:400;opacity:.8}
    #demo-speed{position:fixed;top:64px;left:50%;transform:translateX(-50%);z-index:99999;background:#f59e0b;
      color:#111827;font:700 18px/1 system-ui,sans-serif;padding:9px 16px;border-radius:999px}
    .demo-focus{outline:4px solid #f59e0b !important;outline-offset:3px;border-radius:6px}`;
  const state = {title: '', sub: '', start: null};
  const fmt = (s) => String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(Math.floor(s % 60)).padStart(2, '0');
  const draw = () => {
    const c = document.getElementById('demo-corner');
    if (!c) return;
    const clock = state.start ? ' · real time ' + fmt((Date.now() - state.start) / 1000) : '';
    c.innerHTML = state.title + '<small>' + state.sub + clock + '</small>';
  };
  window.addEventListener('DOMContentLoaded', () => {
    const st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);
    for (const id of ['demo-caption', 'demo-corner', 'demo-speed']) {
      const d = document.createElement('div'); d.id = id; document.body.appendChild(d);
    }
    setInterval(draw, 250); draw();
  });
  window.__demo = {
    caption: (t) => { document.getElementById('demo-caption').textContent = t; },
    speed: (t) => { document.getElementById('demo-speed').textContent = t; },
    corner: (title, sub) => { state.title = title; state.sub = sub; draw(); },
    startClock: () => { state.start = Date.now(); },
  };
})();
"""


class Failed(Exception):
    """The recording must be discarded."""


# --------------------------------------------------------------------------- server and API


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """The real API serving the built UI, on a fresh database."""

    def __init__(self, workdir: Path) -> None:
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = {**os.environ, "DATABASE_URL": f"sqlite:///{workdir / 'demo.db'}"}
        self.log = open(workdir / "server.log", "w")
        self.proc = subprocess.Popen(
            ["uv", "run", "uvicorn", "app.main:create_app", "--factory", "--port", str(self.port)],
            cwd=ROOT / "backend", env=env, stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(60):
            try:
                self.get("/api/health")
                return
            except OSError:
                time.sleep(0.5)
        self.stop()
        raise Failed("the API did not start (see server.log)")

    def get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read())

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()


# --------------------------------------------------------------------------- recorder


class Recorder:
    def __init__(self, page: Page, server: Server) -> None:
        self.page, self.server = page, server
        self.t0 = time.monotonic()  # the video starts when the page is created
        self.fast: list[tuple[float, float]] = []
        self.marks: dict[str, float] = {}
        self._fast_from: float | None = None

    def now(self) -> float:
        return time.monotonic() - self.t0

    def hold(self, seconds: float) -> None:
        self.page.wait_for_timeout(seconds * 1000)

    def caption(self, text: str, hold: float = 0) -> None:
        self.page.evaluate("t => window.__demo.caption(t)", text)
        if hold:
            self.hold(hold)

    def corner(self, title: str, sub: str) -> None:
        self.page.evaluate("([a, b]) => window.__demo.corner(a, b)", [title, sub])

    def fast_start(self) -> None:
        self.page.evaluate("t => window.__demo.speed(t)", f"⏩ sped up {SPEED}x")
        self._fast_from = self.now()

    def fast_stop(self) -> None:
        if self._fast_from is not None:
            self.fast.append((self._fast_from, self.now()))
            self._fast_from = None
        self.page.evaluate("() => window.__demo.speed('')")

    def focus(self, target: Locator, caption: str | None = None, hold: float = 5) -> None:
        target = target.first
        target.wait_for(state="visible", timeout=15000)
        target.evaluate("e => e.scrollIntoView({behavior: 'smooth', block: 'center'})")
        self.hold(0.8)
        target.evaluate("e => e.classList.add('demo-focus')")
        if caption:
            self.caption(caption)
        self.hold(hold)
        target.evaluate("e => e.classList.remove('demo-focus')")

    def nav(self, label: str) -> None:
        self.page.locator("header nav").get_by_role("button", name=label, exact=True).click()
        self.hold(0.8)

    def launch(self, case: str, provider: str = "scripted", injection: str = "none") -> int:
        self.nav("Scenarios")
        selects = self.page.locator("select")
        selects.nth(0).select_option(provider)
        selects.nth(1).select_option(label=injection)
        row = self.page.locator("li").filter(has=self.page.get_by_text(case, exact=True))
        row.first.evaluate("e => e.scrollIntoView({behavior: 'smooth', block: 'center'})")
        self.hold(0.6)
        row.get_by_role("button", name="Run", exact=True).click()
        heading = self.page.get_by_role("heading", name=re.compile(r"^Run #\d+"))
        heading.wait_for(timeout=30000)
        return int(re.search(r"#(\d+)", heading.inner_text()).group(1))

    def run(self, run_id: int) -> dict:
        return self.server.get(f"/api/runs/{run_id}")

    def wait_while_running(self, run_id: int, timeout_s: float, follow: bool = False) -> dict:
        """Poll the API until the run leaves RUNNING. `follow` keeps the newest timeline step in view."""
        start, last_change, seen = time.monotonic(), time.monotonic(), -1
        while True:
            r = self.run(run_id)
            if r["status"] != "RUNNING":
                return r
            if len(r["steps"]) != seen:
                seen, last_change = len(r["steps"]), time.monotonic()
                if follow:
                    rows = self.page.locator("ol li ul li")
                    if rows.count():
                        rows.last.evaluate("e => e.scrollIntoView({behavior: 'smooth', block: 'center'})")
            if time.monotonic() - last_change > STUCK_S:
                raise Failed(f"run #{run_id} stuck: no new step for {STUCK_S} s")
            if time.monotonic() - start > timeout_s:
                raise Failed(f"run #{run_id} still RUNNING after {timeout_s:.0f} s")
            self.hold(1.5)

    def answered(self, run_id: int) -> None:
        """Wait until the inbox answer has been processed (the API resumes the run in the background)."""
        for _ in range(60):
            if not any(a["status"] == "PENDING" for a in self.run(run_id)["approvals"]):
                return
            self.hold(0.5)
        raise Failed(f"the approval answer for run #{run_id} was not processed")

    def step(self, text: str) -> Locator:
        return self.page.locator("ol li ul li").filter(has_text=text)


# --------------------------------------------------------------------------- video 1: scripted


def scripted_video(rec: Recorder) -> None:
    p = rec.page
    rec.corner("Provider: scripted", "offline replay · zero cost")
    rec.caption("AI Purchasing Agent: it makes, executes and validates purchasing decisions.", 4)
    rec.caption("Scripted mode replays a recorded agent trajectory through the real tools, engine, "
                "policy gate and database. No key, same result every time.", 5)

    # a. s1_overstock
    rec.marks["a"] = rec.now()
    row = p.locator("li").filter(has=p.get_by_text("s1_overstock", exact=True))
    rec.focus(row, "Scenario 1: the system recommends 800 units of milk for Bogotá. "
                   "The agent never trusts it: it computes its own requirement.", 5)
    run_id = rec.launch("s1_overstock")
    rec.wait_while_running(run_id, 60)
    rec.caption("Every state, model turn and tool call is on the timeline, with its real latency.", 4)
    rec.step("calculate_net_requirement").first.locator("button").first.click()
    rec.focus(rec.step("calculate_net_requirement"),
              "Click a step for its input and output: here the engine computes the requirement: 140 units.", 6)
    rec.step("calculate_net_requirement").first.locator("button").first.click()
    rec.focus(p.get_by_text("240 units", exact=True),
              "Decision: MODIFY the recommendation to 240 units (requirement 140, raised to the supplier minimum).", 6)
    rec.focus(p.get_by_role("heading", name="Recommendation as is").locator("xpath=following-sibling::div[1]"),
              "The recommended 800 fails case pack, storage, budget and days-of-cover checks.", 6)
    rec.focus(p.get_by_role("heading", name="Inventory projection (end of day)").locator("../.."),
              "Projection: doing nothing vs the chosen order vs what the supplier confirmed. No stockout.", 6)

    # b. x_supplier_rejects
    rec.marks["b_start"] = rec.now()
    rec.caption("Feedback loop: what happens when the supplier rejects the order?", 3)
    run_id = rec.launch("x_supplier_rejects")
    if rec.wait_while_running(run_id, 60)["status"] != "AWAITING_APPROVAL":
        raise Failed("x_supplier_rejects did not pause for approval")
    rec.focus(rec.step("REJECTED"), "Alquería rejects the PO for 240. The outcome check fails, "
                                    "so the agent replans without that supplier.", 6)
    rec.focus(p.get_by_text("The policy gate needs a human decision."),
              "The replan picks the alternate supplier Andina (+3.57%). Policy: a non-primary supplier needs approval.", 5)
    p.get_by_role("button", name="Open the approvals inbox").click()
    rec.hold(1)
    rec.focus(p.locator("div.border-amber-200"), "The approvals inbox shows the request and why it needs a human.", 5)
    rec.caption("Approve.", 1.5)
    p.get_by_role("button", name="Approve", exact=True).first.click()
    rec.answered(run_id)
    if rec.wait_while_running(run_id, 60)["status"] != "COMPLETED":
        raise Failed("x_supplier_rejects did not complete after approval")
    rec.hold(1.5)
    rec.focus(rec.step("CONFIRMED"), "The run resumes: Andina confirms 144 units and the outcome check passes.", 5)
    rec.nav("Purchase orders")
    rec.caption("Purchase orders: the rejected Alquería PO and the confirmed Andina PO, each with its events.", 6)
    rec.marks["b_end"] = rec.now()

    # c. s4_budget_override_rejected
    rec.caption("Scenario 4: a budget constraint. Covering the full need takes a budget override.", 3)
    run_id = rec.launch("s4_budget_override_rejected")
    if rec.wait_while_running(run_id, 60)["status"] != "AWAITING_APPROVAL":
        raise Failed("s4_budget_override_rejected did not pause for approval")
    p.get_by_role("button", name="Open the approvals inbox").click()
    rec.hold(1)
    rec.focus(p.locator("div.border-amber-200 table"),
              "Side by side: 714 units with a budget override, or 342 within budget and the stockout it leaves.", 7)
    p.get_by_placeholder("comment (optional)").first.fill("Stay within budget this week")
    rec.caption("The buyer rejects the override.", 1.5)
    p.get_by_role("button", name="Reject", exact=True).first.click()
    rec.answered(run_id)
    if rec.wait_while_running(run_id, 60)["status"] != "COMPLETED":
        raise Failed("s4_budget_override_rejected did not complete after the rejection")
    rec.hold(1.5)
    rec.focus(p.get_by_text("Residual risk:", exact=False),
              "The agent executes 342 within budget and states the residual risk: a stockout on day 7.", 7)

    # d. evaluations
    rec.nav("Evaluations")
    rec.focus(p.get_by_role("heading", name="Pass rate by provider").locator("../.."),
              "Evaluations: scripted runs pass 51/51 on every dimension; real-model results are reported as they are.", 7)
    rec.focus(p.get_by_role("heading", name="Results by case").locator("../.."),
              "Each case is graded on six deterministic dimensions: decision, information, constraints, "
              "action, validation and recovery.", 7)
    rec.caption("Everything shown is in the repo: make demo, make eval, docker compose up.", 3)


# --------------------------------------------------------------------------- videos 2 and 3: real models


def real_video(rec: Recorder, cfg: dict, model: str) -> dict:
    p = rec.page
    rec.corner(f"Provider: {cfg['label']} (live)", model)
    rec.caption(f"Live run on {cfg['label']} ({model}): the model chooses every tool call; "
                "the engine does the math; the policy gate decides what needs a human.", 5)
    if cfg["injection"] != "none":
        rec.caption("Injected failure: the primary supplier will reject the order, so the model must replan live.", 4)
    started = time.monotonic()
    run_id = rec.launch(cfg["case"], cfg["provider"], cfg["injection"])
    p.evaluate("() => window.__demo.startClock()")
    approvals = 0
    while True:
        rec.fast_start()
        rec.caption(f"{cfg['label']} is working: each row is a real model call or tool call, with its real latency.")
        r = rec.wait_while_running(run_id, 1500, follow=True)
        rec.fast_stop()
        if r["status"] != "AWAITING_APPROVAL":
            break
        approvals += 1
        if approvals > 3:
            raise Failed("more than three approval requests")
        rec.hold(1)
        rec.focus(p.get_by_text("The policy gate needs a human decision."),
                  "The policy gate needs a human: the model's new plan uses a non-primary supplier.", 5)
        p.get_by_role("button", name="Open the approvals inbox").click()
        rec.hold(1)
        rec.focus(p.locator("div.border-amber-200"), "The request and its reasons, in the approvals inbox.", 5)
        rec.caption("Approve.", 1.5)
        p.get_by_role("button", name="Approve", exact=True).first.click()
        rec.answered(run_id)
        p.get_by_role("heading", name=re.compile(rf"^Run #{run_id}\b")).wait_for(timeout=15000)
    duration = time.monotonic() - started

    if r["status"] != "COMPLETED":
        raise Failed(f"run #{run_id} ended {r['status']}" + (f" · {r['reason']}" if r.get("reason") else ""))
    pos = rec.server.get("/api/purchase-orders")["purchase_orders"]
    check_expected(cfg, r, pos)

    rec.hold(1.5)
    d = r["decision"]
    rec.focus(p.get_by_role("heading", name="Decision", exact=True).locator("../.."),
              f"Result: {d['outcome']} {d['quantity']} units, executed and confirmed by the supplier.", 7)
    if r["projection"]:
        rec.focus(p.get_by_role("heading", name="Inventory projection (end of day)").locator("../.."),
                  "Predicted vs confirmed inventory: no stockout.", 5)
    rec.nav("Purchase orders")
    summary = "; ".join(f"{po['supplier']} {po['lines'][0]['qty_ordered']} {po['status']}" for po in pos)
    rec.caption(f"Purchase orders: {summary}.", 6)
    return {"run_id": run_id, "duration_s": round(duration), "outcome": d["outcome"], "quantity": d["quantity"],
            "replans": r["replans"], "pos": summary}


def check_expected(cfg: dict, run: dict, pos: list[dict]) -> None:
    """The only results that count as a successful demo."""
    if cfg["provider"] == "gemini":
        rejected = [po for po in pos if po["status"] == "REJECTED"]
        alternate = [po for po in pos if po["status"] == "CONFIRMED"
                     and all(po["supplier"] != r["supplier"] for r in rejected)]
        if run["replans"] < 1 or not rejected or not alternate:
            raise Failed(f"expected a replan and a confirmed alternate-supplier PO; got replans={run['replans']}, "
                         f"POs={[(po['supplier'], po['status']) for po in pos]}")
    else:
        d = run["decision"] or {}
        if (d.get("outcome"), d.get("quantity")) != ("MODIFY", 240) or not any(
                po["status"] == "CONFIRMED" and po["lines"][0]["qty_ordered"] == 240 for po in pos):
            raise Failed(f"expected MODIFY 240 confirmed; got {d.get('outcome')} {d.get('quantity')}, "
                         f"POs={[(po['supplier'], po['status']) for po in pos]}")


# --------------------------------------------------------------------------- video processing


def ffmpeg(*args: str) -> None:
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def duration_of(path: Path) -> float:
    return imageio_ffmpeg.count_frames_and_secs(str(path))[1]


def encode(raw: Path, out: Path, fast: list[tuple[float, float]]) -> None:
    """webm -> mp4, playing the `fast` stretches at SPEED and everything else at normal speed."""
    cfr = raw.with_suffix(".cfr.mp4")
    ffmpeg("-i", str(raw), "-vf", "fps=25", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", str(cfr))
    total = duration_of(cfr)
    cuts, t = [], 0.0
    for a, b in sorted(fast):
        a, b = max(a, t), min(b, total)
        if b - a < 1:
            continue
        cuts += [(t, a, 1), (a, b, SPEED)]
        t = b
    cuts.append((t, total, 1))
    cuts = [c for c in cuts if c[1] - c[0] > 0.05]
    chains = [f"[0:v]trim={a:.3f}:{b:.3f},setpts=(PTS-STARTPTS)/{k},fps=25[v{i}]" for i, (a, b, k) in enumerate(cuts)]
    graph = ";".join(chains) + ";" + "".join(f"[v{i}]" for i in range(len(cuts))) + f"concat=n={len(cuts)}:v=1[out]"
    ffmpeg("-i", str(cfr), "-filter_complex", graph, "-map", "[out]", "-c:v", "libx264", "-preset", "slow",
           "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out))
    cfr.unlink()


def make_gif(video: Path, start: float, end: float, out: Path) -> None:
    for width, fps in ((960, 6), (800, 5), (720, 4)):
        vf = f"fps={fps},scale={width}:-1:flags=lanczos"
        ffmpeg("-ss", f"{start:.2f}", "-to", f"{end:.2f}", "-i", str(video), "-filter_complex",
               f"{vf},split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4", str(out))
        if out.stat().st_size < 8 * 1024 * 1024:
            return
    raise Failed(f"{out.name} is over 8 MB even at 720 px / 4 fps")


# --------------------------------------------------------------------------- main


def record(name: str, workdir: Path) -> dict:
    server = Server(workdir)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport=VIEWPORT, record_video_dir=str(workdir / "raw"),
                                      record_video_size=VIEWPORT, device_scale_factor=1)
            ctx.add_init_script(OVERLAY)
            page = ctx.new_page()
            rec = Recorder(page, server)
            page.goto(server.base + "/")
            page.get_by_text("AI Purchasing Agent").first.wait_for()
            rec.hold(0.5)
            info: dict = {}
            try:
                if name == "scripted":
                    scripted_video(rec)
                else:
                    cfg = REAL[name]
                    info = real_video(rec, cfg, model_name(cfg["model_env"]))
                rec.hold(1)
            except Exception:
                page.screenshot(path=str(Path(tempfile.gettempdir()) / f"demo-failure-{name}.png"))
                raise
            finally:
                raw = Path(page.video.path())
                ctx.close()
                browser.close()
        return {"raw": raw, "fast": rec.fast, "marks": rec.marks, **info}
    finally:
        server.stop()


def model_name(var: str) -> str:
    """The configured model, from the environment or .env, as the API will use it."""
    sys.path.insert(0, str(ROOT / "backend"))
    from app.config import get_settings
    return getattr(get_settings(), var.lower())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("videos", nargs="+", choices=["scripted", "gemini", "groq"])
    args = ap.parse_args()
    if not (ROOT / "frontend" / "dist" / "index.html").exists():
        print("build the UI first: cd frontend && npm install && npm run build")
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    status = 0
    for name in args.videos:
        out = OUT / ("demo-scripted.mp4" if name == "scripted" else REAL[name]["file"])
        workdir = Path(tempfile.mkdtemp(prefix=f"demo-{name}-"))
        try:
            if name != "scripted":
                pre = subprocess.run(["make", "-s", "check-providers", f"ONLY={REAL[name]['provider']}"], cwd=ROOT)
                if pre.returncode != 0:
                    raise Failed("provider preflight is not OK")
            res = record(name, workdir)
            encode(res["raw"], out, res["fast"])
            meta = {"video": out.name, "length_s": round(duration_of(out), 1),
                    "size_mb": round(out.stat().st_size / 1e6, 1), "speed_up": SPEED if res["fast"] else 1,
                    **{k: v for k, v in res.items() if k not in ("raw", "fast", "marks")}}
            if name == "scripted":
                make_gif(out, res["marks"]["b_start"], res["marks"]["b_end"], OUT / "replan.gif")
                meta["gif_mb"] = round((OUT / "replan.gif").stat().st_size / 1e6, 1)
            if meta["size_mb"] >= 25:
                raise Failed(f"{out.name} is {meta['size_mb']} MB (limit 25)")
            print("KEPT", json.dumps(meta))
        except Exception as e:  # the strict rule: any failure discards the video, no retry
            out.unlink(missing_ok=True)
            if name == "scripted":
                (OUT / "replan.gif").unlink(missing_ok=True)
            print(f"SKIPPED {name}: {type(e).__name__}: {e}")
            status = 1
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    return status


if __name__ == "__main__":
    sys.exit(main())
