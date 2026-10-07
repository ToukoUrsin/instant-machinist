# Submission: Autonomous CNC Programmer

**Workflow replaced:** CNC programming for job shops. A programmer takes a customer's 3D model and RFQ, picks tools, writes the toolpaths and G-code, then proves it in simulation before anyone cuts metal. That's hours per part, every part, every RFQ. Programmers are the bottleneck in nearly every US job shop.

**Before:** a programmer opens CAM, sets up stock, picks tools, builds each operation, simulates, finds the mistake, fixes it, posts the program. Typically 1–4 hours for a simple prismatic part.

**After:** the RFQ (the customer's 2D drawing + STEP model) and the shop's job sheet and tool crib go to an agent running on its own Agent37 computer. It:
1. reads the drawing (dimensions, tolerances, notes), writes the machining spec and cross-checks it against the STEP model,
2. writes a G-code generator and posts a Haas program,
3. runs the shop's stock-removal simulator on every attempt (real tool shapes swept through the stock and compared with the part model: gouges, leftover material, rapids through stock, holder collisions, flute length, feed/rpm limits),
4. reads what it got wrong (the report names NC lines and XY locations), fixes it, and repeats,
5. hands back only a program that verifies, plus a hand-off note for the machinist.

**Proof:** every attempt is kept. The videos are the verifier's real material-removal result for each program, frame by frame: the failed attempt shows the gouge in red, the final one passes. Reports, programs and the agent's full tool-call stream are in the repo.

**Sponsor integrations:**
- **Agent37:** each RFQ gets its own persistent, isolated agent computer (Hermes harness): we upload the workspace through the Files API, install the verifier through exec, stream the agent's reasoning and tool calls live over SSE, render the videos on the same instance, and pull the results back with the archive API. One instance per customer shop keeps its tool crib, machine and past jobs.
- **OpenAI:** the agent runs on OpenAI GPT-6.1 Sol through Agent37's model routing: it reads the drawing with vision, writes the generator and fixes its own program.
- **Monid:** prices the material live for the quote: Monid → Firecrawl pulls OnlineMetals' current 6061-T651 plate table (1.25 in plate, $48.44 per blank, 25 blanks per 24×36 sheet).

**Limitations (honest):** simulation only; nothing has been cut on a machine. One 3-axis setup, one prismatic part. Fixtures/vise aren't modelled beyond a minimum-Z limit. A machinist should still review before the first cut.

**Links:** demo video · repo (local) · `site/index.html`
