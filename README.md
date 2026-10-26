# Radar Pong

Pong played by walking. Two LD2450 presence radars, one at each end of a shared court. Every person a radar sees is a paddle on that end: walk left and right to move it, step back from the sensor to push it up the court. The sensors can be in different rooms. Each end only needs a radar stream and a screen.

On your own page your end is at the bottom, like standing at your end of a table. **my end** in the bar moves it to the top, left or right instead; each screen remembers its choice, or add `?own=left` to the address. You see the other end's paddles move but can only drive your own.

## Run

```bash
python pong.py --a sim --b sim:2
```

Then open `http://<this machine>:8097/split`.

| Page | Shows |
|---|---|
| `/a`, `/b` | one end each, own paddle at the bottom (or where **my end** says) |
| `/split` | both ends on one screen, each the right way up for its player (stacked when ends are left/right) |
| `/watch` | whole court, A where **my end** says |

## Where people come from

```bash
python pong.py --a http://plotterpi:8090/stream#0 --b http://plotterpi:8090/stream#1
python pong.py --a http://roomA:8095/targets --b http://roomB:8095/targets
```

- `URL` is plotterpi's `/stream` or `ld2450/serve.py`'s `/targets`. `#N` takes only sensor N from plotterpi's Matrix.
- `sim` / `sim:3` are fake players who chase the ball 150 ms late through the same mm to court mapping.
- `none` leaves that end to keys or touch.

No radar handy: keys (A end **A D W S**, B end **arrows**) or drag on your own court. That paddle leaves a couple of seconds after you let go.

## How it plays

- **Teams**: up to three people per radar, three paddles. *Narrower paddles with more people* keeps it fair if one end is crowded.
- **Depth**: standing further from your sensor pushes your paddle up the court, up to the coloured dashed line. Lunging forward into the ball is a **smash** and adds speed.
- **Ball**: the old square. Starts slow, 7% faster per hit, angle from where it hits the paddle. At a 12-hit rally a second ball drops in.
- **One end empty** for 3 s: it becomes a wall and the other end plays squash, counting the rally. Nobody at either end: attract mode, ghost paddles rally by themselves. Someone walks in, it starts.
- **Sound** plays on whichever device has it switched on (square-wave beeps).

Settings (gear button) are shared by every page and saved to `settings.json`: paddle width, ball size and speeds, depth zone, multiball, points to win, and per sensor the reach (mm either side of centre that spans the court), centre offset, mirror, near and far. The small box in the corner of each end is that sensor's view, turned the same way as the court, for setting reach.

## Check

```bash
python test_headless.py
```

Runs 10 minutes of 1 v 1, 1 v 2 and sloppy 3 v 3 with no clock and exits 1 if a ball passes through a paddle that squarely covered it.

## Known

- Not tried on a real radar yet. Radar latency (100 to 200 ms) is why paddles are wide and the serve is slow; tune on the day.
- Radar slots are not people: if two people cross, their paddles can swap.
- Two LD2450s pointing into the same space may interfere. Different rooms avoids it.
- Windows sometimes reserves the port range around 8097 (WinError 10013). Use `--http 5210` or any free port.
