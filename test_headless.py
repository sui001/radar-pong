"""Run matches with no server and no clock, and check the physics holds up.

    python test_headless.py

Players read the ball at 10 Hz, 150 ms late, with some aim error, like the sim.
Fails (exit 1) if a ball gets past a paddle that was squarely covering it, or a
match never produces rallies.
"""

import math
import random
import sys

import pong

random.seed(1)
TICK = pong.TICK


def run(minutes, people_a, people_b, aim=0.05):
    g = pong.Game(pong.Side("a", "test"), pong.Side("b", "test"))
    pong.GAME = g
    t = 0.0
    hist = []
    hits = points = wins = through = 0
    rallies = []
    last_seq = 0
    while t < minutes * 60:
        t += TICK
        if int(t / 0.1) != int((t - TICK) / 0.1):          # a radar frame
            hist.append((t, [dict(x=b["x"], y=b["y"], vy=b["vy"]) for b in g.balls]))
            while hist and t - hist[0][0] > 0.15:
                _, balls = hist.pop(0)
                for name, n in (("a", people_a), ("b", people_b)):
                    side = g.sides[name]
                    coming = [b for b in balls if (b["vy"] < 0) == (name == "a")] or balls
                    frame = []
                    for i in range(n):
                        u = 0.5
                        if coming:
                            b = coming[0]
                            u = b["x"] if name == "a" else 1 - b["x"]
                            if n > 1:
                                c = (i + 0.5) / n
                                u = c + max(-0.5 / n - 0.1, min(0.5 / n + 0.1, u - c))
                        x, y = side.from_court(min(1, max(0, u + random.gauss(0, aim))), 0.1)
                        frame.append((i, x, y))
                    side.frame(frame, t)
        before = [dict(b) for b in g.balls]
        pads = {n: g.paddles(n, t) for n in "ab"}
        rally = g.rally
        g.step(TICK, t)
        hit_now = False
        for seq, kind in g.events:
            if seq <= last_seq:
                continue
            last_seq = seq
            hits += kind == "hit"
            hit_now |= kind == "hit"
            wins += kind == "win"
            if kind in ("point", "win"):
                points += 1
                rallies.append(rally)
        # a ball centre crossing a paddle face squarely inside its width must be a hit
        if not hit_now and g.phase == "play" and len(before) == len(g.balls):
            for b0, b1 in zip(before, g.balls):
                name = "a" if b0["vy"] < 0 else "b"
                for cx, face, hw, vd, sign in pads[name]:
                    if (b0["y"] - face) * (b1["y"] - face) < 0 and abs(b0["x"] - cx) < hw * 0.6:
                        through += 1
    return dict(hits=hits, points=points, wins=wins, through=through,
                mean_rally=round(sum(rallies) / max(1, len(rallies)), 1), max_rally=max(rallies or [0]))


ok = True
for label, a, b, aim in (("1 v 1", 1, 1, 0.05), ("1 v 2", 1, 2, 0.05), ("3 v 3 sloppy", 3, 3, 0.12)):
    r = run(10, a, b, aim)
    print(f"{label:14} {r}")
    if r["through"]:
        print("  FAIL: ball went through a paddle that covered it"); ok = False
    if r["hits"] < r["points"]:
        print("  FAIL: fewer hits than points, rallies are not happening"); ok = False
sys.exit(0 if ok else 1)
