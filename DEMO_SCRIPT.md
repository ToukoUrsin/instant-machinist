# 90-second recording script

Open `site/index.html` (served with `python3 -m http.server 8080 -d site`, then http://localhost:8080).

1. **0:00–0:12 (header):** "Every job shop has the same bottleneck: CNC programming. A customer sends a 3D model, and someone spends hours turning it into G-code. We gave that job to an agent."
2. **0:12–0:25 (scroll to the RFQ at the bottom, then back up):** "This is the RFQ: an aluminium plate, a pocket, four holes, a Haas mill and the shop's tool crib. That's all the agent gets."
3. **0:25–0:45 (timeline on the right):** "It runs on its own Agent37 computer. Here's its live stream: it reads the spec, writes a G-code generator, saves an attempt, runs the shop's stock-removal simulator."
4. **0:45–1:05 (Attempt 01 video):** "This is its first program, swept through the stock with the real tool shapes. The simulator caught it: red is where it would have cut into the part. Nobody told it about this mistake."
5. **1:05–1:20 (click the green attempt):** "It read the report, which names the NC lines and the location, fixed its own program and re-ran. Verified: no gouge, no leftover, no collisions."
6. **1:20–1:30 (posted program + hand-off note):** "Out comes a Haas program and a hand-off note for the machinist. Hours of programming, done unattended. Agent37 plus OpenAI."

If the run passed on the first attempt, change steps 4–5 to: "The simulator checked every move; first attempt verified."
