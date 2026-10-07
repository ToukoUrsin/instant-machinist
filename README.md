# Instant Machinist

An AI agent that programs the machines that cut metal parts.

A shop forwards a customer's drawing and 3D model. The agent reads the drawing, writes the CNC program (G-code for a Haas mill), and tests it in a stock-removal simulator before any metal is cut. If the simulator finds a gouge, leftover material or a collision, the agent reads the report, fixes its program and runs it again. The shop only gets programs that pass.

Built at the Build an Agent Hackathon, October 7, 2026 (San Francisco).

## Stack

- Agent37 Cloud: one isolated agent computer per job (Files API, exec, live SSE stream, archive API)
- OpenAI GPT-6.1 Sol: reads the drawing, writes and debugs the program
- Monid: live material pricing from supplier sites for the quote
- Supabase: job log and live view of every attempt

## Layout

- `run_demo.py`: creates the Agent37 instance, uploads the job, streams the agent, downloads results
- `cnc/`: agent brief, the job (drawing, STEP, shop sheet), simulator wrappers, video renderer
- `vendor/checks/`: the stock-removal simulator and G-code checks
- `runs/`: every run, with the agent's full event stream
- `site/`: the demo page (`python3 -m http.server 8080 -d site`)

Simulation only: nothing here has been cut on a real machine yet.
