"""Radar Pong: two presence radars, two ends of one court.

Every person a sensor sees is a paddle on that sensor's end. Walk left and right
to move it, step away from the sensor to push it up the court. The sensors can
be in different rooms: each end only needs a radar stream and a screen.

    python pong.py --a sim --b sim                     # no sensors, two fake players
    python pong.py --a http://100.68.139.69:8090/stream#0 --b http://100.68.139.69:8090/stream#1
    python pong.py --a http://roomA:8095/targets --b http://roomB:8095/targets

    --a / --b   where each end's people come from:
                  URL        plotterpi /stream or ld2450 serve.py /targets (Server-Sent Events)
                  URL#N      only lines from sensor N (plotterpi's Matrix tags each line "s":N)
                  sim / sim:2  fake players who chase the ball, sent through the same
                             mm -> court mapping a real radar uses
                  none       nobody; that end can still play with keys or touch

Pages (any device that can reach this machine):
    /a /b      one end each, your own paddle at the top
    /split     both ends side by side on one screen
    /watch     the whole court, A at the top

No sensor in reach? Keys or a finger on the page drive a paddle for that end.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

VERSION = "0.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))

L = 1.4             # court length; width is 1. A's end line is y = 0, B's is y = L
PAD_T = 0.025       # paddle thickness
END_GAP = 0.04      # paddle face distance from its end line at depth 0
TICK = 1 / 120      # physics step
SEND = 1 / 30       # state broadcast
GONE_S = 1.0        # a body unseen this long has left
EMPTY_S = 3.0       # an end with nobody this long stops counting as a team

SETTINGS = {
    "paddleWidth": 0.22,   # court widths, per person
    "teamShrink": False,   # more people on an end -> narrower each (width / sqrt n)
    "ballSize": 0.035,
    "speedStart": 0.55,    # court widths per second
    "speedUp": 1.07,       # times faster per hit
    "speedMax": 1.9,
    "maxAngle": 60,        # degrees off straight, at the paddle tip
    "depth": True,         # distance from the sensor moves your paddle up the court
    "zone": 0.28,          # how far up the court depth can take a paddle
    "smash": True,         # paddle moving into the ball adds speed
    "multiball": 12,       # rally length that drops a second ball in, 0 = never
    "winScore": 7,
    "squash": True,        # one end empty: it becomes a wall and you rally against it
    "smoothing": 0.08,     # seconds; radar is ~10 Hz so paddles glide between frames
}
SIDE_DEFAULTS = {
    "reach": 1500,   # mm either side of centre that spans the court width
    "centre": 0,     # mm, shift if the sensor is off to one side of the play area
    "mirror": False, # flip left/right if the sensor faces the other way
    "near": 700,     # mm from the sensor: paddle on its end line
    "far": 3000,     # mm: paddle at the top of its zone
}


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


class Side:
    def __init__(self, name, source):
        self.name = name
        self.source = source
        self.cfg = dict(SIDE_DEFAULTS)
        self.bodies = {}        # slot -> {"tu","td","u","d","seen","vd"}
        self.frames = 0
        self.fps = 0
        self.status = "waiting"
        self.last_person = 0.0
        self.raw = []           # latest people in mm, for the page's radar strip

    # radar -> court frame for this end: u 0..1 left to right as this end sees it
    def to_court(self, x, y):
        c = self.cfg
        u = (x - c["centre"]) / (2 * c["reach"]) + 0.5
        if c["mirror"]:
            u = 1 - u
        d = clamp((y - c["near"]) / max(1, c["far"] - c["near"]), 0, 1)
        return clamp(u, 0, 1), d

    def from_court(self, u, d):
        c = self.cfg
        if c["mirror"]:
            u = 1 - u
        return ((u - 0.5) * 2 * c["reach"] + c["centre"], c["near"] + d * (c["far"] - c["near"]))

    def frame(self, people, now):
        """people: list of (slot, x_mm, y_mm), one radar frame."""
        self.frames += 1
        self.raw = [(round(x), round(y)) for _, x, y in people]
        for slot, x, y in people:
            u, d = self.to_court(x, y)
            b = self.bodies.get(slot)
            if b is None:
                self.bodies[slot] = {"tu": u, "td": d, "u": u, "d": d, "seen": now, "vd": 0.0}
            else:
                b.update(tu=u, td=d, seen=now)

    def hands(self, u, d, now):
        b = self.bodies.get("h")
        if b is None:
            self.bodies["h"] = {"tu": u, "td": d, "u": u, "d": d, "seen": now, "vd": 0.0}
        else:
            b.update(tu=u, td=d, seen=now)

    def step(self, dt, now, smoothing):
        for k in [k for k, b in self.bodies.items() if now - b["seen"] > (2.5 if k == "h" else GONE_S)]:
            del self.bodies[k]
        a = 1 - math.exp(-dt / max(0.001, smoothing))
        for b in self.bodies.values():
            u0, d0 = b["u"], b["d"]
            b["u"] += (b["tu"] - b["u"]) * a
            b["d"] += (b["td"] - b["d"]) * a
            b["vd"] = (b["d"] - d0) / dt
            b["vu"] = (b["u"] - u0) / dt
        if self.bodies:
            self.last_person = now

    def team(self, now):
        return bool(self.bodies) or now - self.last_person < EMPTY_S


class Game:
    def __init__(self, a, b):
        self.sides = {"a": a, "b": b}
        self.s = dict(SETTINGS)
        self.lock = threading.Lock()
        self.score = {"a": 0, "b": 0}
        self.balls = []
        self.phase = "attract"
        self.phase_t = 0.0
        self.serve_to = "a"
        self.rally = 0
        self.best = 0
        self.mode = "attract"
        self.events = []        # [seq, kind] for the pages' beeps
        self.seq = 0
        self.msg = ""
        self.ghost = {"a": 0.5, "b": 0.5}
        self.new_ball(toward="a", still=True)

    def event(self, kind):
        self.seq += 1
        self.events = (self.events + [[self.seq, kind]])[-20:]

    def new_ball(self, toward, still=False):
        ang = math.radians(random.uniform(-25, 25))
        sp = self.s["speedStart"]
        vy = -1 if toward == "a" else 1
        ball = {"x": 0.5, "y": L / 2, "vx": sp * math.sin(ang), "vy": vy * sp * math.cos(ang), "sp": sp}
        if still:
            self.balls = [ball]
        else:
            self.balls.append(ball)
        return ball

    # paddles in court coordinates: (cx, face_y, half_w, vd, dir) where dir is the
    # vy sign a ball must have to hit this face
    def paddles(self, name, now):
        side = self.sides[name]
        bodies = list(side.bodies.values())
        w = self.s["paddleWidth"]
        if self.s["teamShrink"] and len(bodies) > 1:
            w /= math.sqrt(len(bodies))
        if not bodies and self.mode == "attract":
            bodies = [{"u": self.ghost[name], "d": 0.0, "vd": 0.0}]
        out = []
        for b in bodies:
            d = b["d"] * self.s["zone"] * L if self.s["depth"] else 0.0
            if name == "a":
                out.append((b["u"], END_GAP + d, w / 2, b["vd"], -1))
            else:
                out.append((1 - b["u"], L - END_GAP - d, w / 2, b["vd"], 1))
        return out

    def set_mode(self, now):
        ta, tb = self.sides["a"].team(now), self.sides["b"].team(now)
        mode = "match" if ta and tb else ("squash_a" if ta else "squash_b") if (ta or tb) and self.s["squash"] else \
               "attract" if not (ta or tb) else "waiting"
        if mode != self.mode:
            self.mode = mode
            self.score = {"a": 0, "b": 0}
            self.rally = 0
            self.serve_to = "a" if mode != "squash_b" else "b"
            self.start_serve(now)
            self.event("mode")

    def start_serve(self, now):
        self.phase, self.phase_t = "serve", now
        self.new_ball(self.serve_to, still=True)
        self.balls[0].update(vx=0.0, vy=0.0)
        self.rally = 0

    def step(self, dt, now):
        for side in self.sides.values():
            side.step(dt, now, self.s["smoothing"])
        self.set_mode(now)

        if self.mode == "attract":   # ghosts keep a lazy rally going
            for n in "ab":
                if self.balls:
                    bx = self.balls[0]["x"] if n == "a" else 1 - self.balls[0]["x"]
                    self.ghost[n] += clamp(bx - self.ghost[n], -0.5 * dt, 0.5 * dt)

        if self.phase == "serve":
            self.msg = {"match": "", "attract": "walk in to play", "waiting": "waiting for players",
                        "squash_a": "squash: A against the wall", "squash_b": "squash: B against the wall"}[self.mode]
            if now - self.phase_t > (1.2 if self.mode == "attract" else 2.0):
                self.phase = "play"
                self.new_ball(self.serve_to, still=True)
                self.event("serve")
            return
        if self.phase in ("point", "win"):
            if now - self.phase_t > (1.2 if self.phase == "point" else 4.0):
                if self.phase == "win":
                    self.score = {"a": 0, "b": 0}
                self.start_serve(now)
            return
        if self.phase != "play":
            return

        wall = {"squash_a": "b", "squash_b": "a"}.get(self.mode)
        pads = {n: self.paddles(n, now) for n in "ab" if n != wall}
        bs = self.s["ballSize"] / 2
        for ball in list(self.balls):
            px, py = ball["x"], ball["y"]
            ball["x"] += ball["vx"] * dt
            ball["y"] += ball["vy"] * dt
            if ball["x"] < bs or ball["x"] > 1 - bs:
                ball["x"] = clamp(ball["x"], bs, 1 - bs)
                ball["vx"] = -ball["vx"]
                self.event("wall")
            for n, plist in pads.items():
                for cx, face, hw, vd, sign in plist:
                    if (ball["vy"] > 0) != (sign > 0):
                        continue      # moving away from this face
                    edge0 = py + sign * bs
                    edge1 = ball["y"] + sign * bs
                    if not (min(edge0, edge1) <= face <= max(edge0, edge1)
                            or (sign < 0 and face - PAD_T <= edge1 <= face)
                            or (sign > 0 and face <= edge1 <= face + PAD_T)):
                        continue
                    if abs(ball["x"] - cx) > hw + bs:
                        continue
                    self.hit(ball, n, cx, face, hw, vd, sign)
                    break
            if wall and ((wall == "a" and ball["y"] < bs) or (wall == "b" and ball["y"] > L - bs)):
                ball["y"] = bs if wall == "a" else L - bs
                ball["vy"] = -ball["vy"]
                self.event("wall")
            if ball["y"] < -0.05 or ball["y"] > L + 0.05:
                self.balls.remove(ball)
                if not self.balls:
                    self.point("b" if ball["y"] < 0 else "a", now)
                    return

    def hit(self, ball, n, cx, face, hw, vd, sign):
        s = self.s
        rel = clamp((ball["x"] - cx) / (hw + s["ballSize"] / 2), -1, 1)
        if n == "b":
            rel = -rel     # angle is as the hitter sees it; the maths is in A's frame
        sp = min(ball["sp"] * s["speedUp"], s["speedMax"])
        if s["smash"] and vd > 0.3:          # moving up the court into the ball
            sp = min(sp + 0.25 * vd * s["zone"] * L, s["speedMax"] * 1.25)
            self.event("smash")
        ball["sp"] = sp
        ang = math.radians(rel * s["maxAngle"])
        out = -sign
        ball["vx"] = sp * math.sin(ang) * (1 if n == "a" else -1)
        ball["vy"] = out * sp * math.cos(ang)
        ball["y"] = face + out * (s["ballSize"] / 2 + 0.001)
        self.rally += 1
        self.best = max(self.best, self.rally) if self.mode.startswith("squash") else self.best
        self.event("hit")
        if s["multiball"] and self.rally == s["multiball"] and len(self.balls) == 1:
            nb = self.new_ball("b" if n == "a" else "a")
            nb["x"] = random.uniform(0.2, 0.8)
            self.event("multi")

    def point(self, winner, now):
        if self.mode == "attract":
            self.serve_to = "b" if winner == "a" else "a"
            self.start_serve(now)
            return
        if self.mode.startswith("squash"):
            self.msg = f"rally {self.rally}" + (f", best {self.best}" if self.best else "")
            self.event("miss")
            self.phase, self.phase_t = "point", now
            return
        self.score[winner] += 1
        self.serve_to = "b" if winner == "a" else "a"   # the end that lost the point receives
        if self.score[winner] >= self.s["winScore"]:
            self.phase, self.phase_t = "win", now
            self.msg = f"{winner.upper()} wins {self.score['a']} : {self.score['b']}"
            self.event("win")
        else:
            self.phase, self.phase_t = "point", now
            self.msg = f"point {winner.upper()}"
            self.event("point")

    def state(self, now):
        def side_state(n):
            sd = self.sides[n]
            return {"pads": [{"u": round(b["u"], 4), "d": round(b["d"], 4), "slot": str(k)}
                             for k, b in sd.bodies.items()],
                    "ghost": round(self.ghost[n], 4) if self.mode == "attract" and not sd.bodies else None,
                    "status": sd.status, "fps": sd.fps, "raw": sd.raw, "cfg": sd.cfg,
                    "source": sd.source}
        return {"v": VERSION, "L": L, "endGap": END_GAP, "padT": PAD_T, "t": now,
                "balls": [{k: round(b[k], 4) for k in ("x", "y", "vx", "vy")} for b in self.balls],
                "score": self.score, "phase": self.phase, "mode": self.mode, "msg": self.msg,
                "rally": self.rally, "best": self.best, "events": self.events,
                "settings": self.s, "a": side_state("a"), "b": side_state("b")}

    def run(self):
        last = time.monotonic()
        while True:
            now = time.monotonic()
            n = 0
            while last + TICK <= now and n < 12:
                with self.lock:
                    self.step(TICK, last + TICK)
                last += TICK
                n += 1
            if n == 12:
                last = now
            time.sleep(TICK / 2)


# ---------- where people come from ----------

def parse_source(spec):
    if spec in ("none", ""):
        return {"kind": "none"}
    if spec.startswith("sim"):
        return {"kind": "sim", "n": int(spec.split(":")[1]) if ":" in spec else 1}
    url, _, s = spec.partition("#")
    return {"kind": "url", "url": url, "s": int(s) if s else None}


def listen_url(url, sinks):
    """sinks: list of (side, s or None). One connection per URL, shared by both ends."""
    while True:
        try:
            for side, _ in sinks:
                side.status = "connecting"
            with urllib.request.urlopen(url, timeout=10) as r:
                for side, _ in sinks:
                    side.status = "listening"
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    line = line[5:].strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if not msg.get("t"):
                        continue
                    s = msg.get("s", 0)
                    people = [(i, t[0], t[1]) for i, t in enumerate(msg["t"]) if t]
                    now = time.monotonic()
                    for side, want in sinks:
                        if want is None or want == s:
                            with GAME.lock:
                                side.frame(people, now)
        except Exception as e:  # noqa: BLE001  any drop: say so and retry
            for side, _ in sinks:
                side.status = f"no stream ({type(e).__name__})"
            time.sleep(2)


def sim_players(side, n):
    """Fake people in mm, chasing the ball late and imprecisely, at radar rate."""
    people = [{"x": random.uniform(-800, 800), "y": random.uniform(900, 2000), "err": 0.0,
               "pace": random.uniform(0.7, 1.1), "lane": i} for i in range(n)]
    hist = []
    side.status = f"sim x{n}"
    while True:
        now = time.monotonic()
        with GAME.lock:
            balls = list(GAME.balls)
            name = side.name
        coming = [b for b in balls if (b["vy"] < 0) == (name == "a")] or balls
        for p in people:
            if random.random() < 0.05:
                p["err"] = random.gauss(0, 0.09)
            if coming:
                b = min(coming, key=lambda b: b["y"] if name == "a" else L - b["y"])
                u = b["x"] if name == "a" else 1 - b["x"]
                if n > 1:   # a team spreads out: lanes, each chases only near its own
                    lane_c = (p["lane"] + 0.5) / n
                    u = lane_c + clamp(u - lane_c, -0.5 / n - 0.1, 0.5 / n + 0.1)
            else:
                u = 0.5
            tx, _ = side.from_court(clamp(u + p["err"], 0, 1), 0)
            step = 1700 * p["pace"] * 0.1
            p["x"] += clamp(tx - p["x"], -step, step)
            p["y"] += random.gauss(0, 60)
            p["y"] = clamp(p["y"], 800, 2800)
        frame = [(i, p["x"] + random.gauss(0, 40), p["y"] + random.gauss(0, 40)) for i, p in enumerate(people)]
        hist.append((now, frame))
        while hist and now - hist[0][0] > 0.15:    # the radar's lag
            _, f = hist.pop(0)
            with GAME.lock:
                side.frame(f, time.monotonic())
        time.sleep(0.1)


def fps_counter():
    while True:
        time.sleep(1)
        for side in GAME.sides.values():
            side.fps, side.frames = side.frames, 0


# ---------- HTTP ----------

PAGES = {"/", "/a", "/b", "/split", "/watch"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in PAGES:
            with open(os.path.join(HERE, "pong.html"), "rb") as f:
                self._send(200, "text/html; charset=utf-8", f.read())
        elif path == "/state":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    with GAME.lock:
                        st = GAME.state(time.monotonic())
                    self.wfile.write(f"data: {json.dumps(st, separators=(',', ':'))}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(SEND)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return
        else:
            self._send(404, "text/plain", b"not here\n")

    def do_POST(self):
        path = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        try:
            d = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, "application/json", b'{"ok":false}')
        with GAME.lock:
            if path == "/hands" and d.get("side") in GAME.sides:
                GAME.sides[d["side"]].hands(clamp(float(d.get("u", 0.5)), 0, 1),
                                            clamp(float(d.get("d", 0)), 0, 1), time.monotonic())
            elif path == "/settings":
                for k, v in d.get("game", {}).items():
                    if k in GAME.s:
                        GAME.s[k] = type(SETTINGS[k])(v)
                for n_, cfg in d.get("sides", {}).items():
                    if n_ in GAME.sides:
                        for k, v in cfg.items():
                            if k in SIDE_DEFAULTS:
                                GAME.sides[n_].cfg[k] = type(SIDE_DEFAULTS[k])(v)
                save_settings()
            elif path == "/reset":
                GAME.score = {"a": 0, "b": 0}
                GAME.best = 0
                GAME.start_serve(time.monotonic())
            else:
                return self._send(404, "application/json", b'{"ok":false}')
        self._send(200, "application/json", b'{"ok":true}')


SETTINGS_FILE = os.path.join(HERE, "settings.json")


def save_settings():
    with open(SETTINGS_FILE, "w") as f:
        json.dump({"game": GAME.s, "sides": {n: s.cfg for n, s in GAME.sides.items()}}, f, indent=1)


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return
    GAME.s.update({k: v for k, v in d.get("game", {}).items() if k in SETTINGS})
    for n, cfg in d.get("sides", {}).items():
        if n in GAME.sides:
            GAME.sides[n].cfg.update({k: v for k, v in cfg.items() if k in SIDE_DEFAULTS})


GAME: Game


def main():
    global GAME
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--a", default="sim", help="end A's people: URL[#sensor], sim[:n] or none")
    p.add_argument("--b", default="sim", help="end B's people")
    p.add_argument("--listen", default="0.0.0.0")
    p.add_argument("--http", type=int, default=8097)
    args = p.parse_args()

    print(f"=== Radar Pong v{VERSION} ===")
    print("Two presence radars, two ends of one court; every person is a paddle")
    print("Local repo ~/radar-pong (not on GitHub yet)")

    srcs = {"a": parse_source(args.a), "b": parse_source(args.b)}
    GAME = Game(Side("a", args.a), Side("b", args.b))
    load_settings()
    urls = {}
    for n, src in srcs.items():
        side = GAME.sides[n]
        if src["kind"] == "url":
            urls.setdefault(src["url"], []).append((side, src["s"]))
        elif src["kind"] == "sim":
            threading.Thread(target=sim_players, args=(side, src["n"]), daemon=True).start()
        else:
            side.status = "no sensor (keys/touch)"
    for url, sinks in urls.items():
        threading.Thread(target=listen_url, args=(url, sinks), daemon=True).start()
    threading.Thread(target=GAME.run, daemon=True).start()
    threading.Thread(target=fps_counter, daemon=True).start()

    srv = ThreadingHTTPServer((args.listen, args.http), Handler)
    srv.daemon_threads = True
    print(f"A: {args.a}   B: {args.b}")
    print(f"open http://localhost:{args.http}/split  (or /a, /b, /watch)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
