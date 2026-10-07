# Hackathon build framework

This is the reusable operating framework for the two-hour build. It deliberately does not contain a product idea.

## 1. Choose the workflow

A strong target satisfies all six:

- Repeated often enough to hurt.
- Currently done across multiple tools or tabs.
- Has a clear input and a clear finished output.
- Can use live or convincingly realistic data.
- Has an objective proof of completion or improvement.
- Can be explained in one sentence without saying “AI assistant.”

Write the target as:

> For [specific user], replace [specific recurring workflow] with [specific finished result], reducing [measurable cost/time/error].

Reject targets that are only chat, generic summarization, a static dashboard, or a pre-written answer with no real source data.

## 2. Design the minimum loop

The demo must show this loop:

1. **Trigger** — a real event, file, request, record, or scheduled run arrives.
2. **Gather** — the agent retrieves the required source data.
3. **Decide** — it applies explicit rules and produces a decision, not just prose.
4. **Act** — it changes a record, creates a file, sends a message, opens a PR, or produces a usable artifact.
5. **Prove** — it shows the evidence, result and measurable value.

A human may approve a consequential action, but the agent must do the work before that approval.

## 3. Sponsor architecture

Use the smallest reliable architecture that makes each sponsor meaningful:

- **Agent37:** persistent isolated agent instance, files, shell/tools, streaming response, integrations or scheduled run.
- **OpenAI:** model reasoning, structured extraction, vision, or evaluation.
- **Supabase:** source records, event log, result state, and a simple public read-only view.
- **InstaCloud / InsForge:** deploy the demo service or agent-facing backend if reliable.
- **Monid:** retrieve one useful external tool or data source; validate returned names and results.

Never add a sponsor as a logo-only integration. Every claimed sponsor must appear in the actual path from trigger to result.

## 4. Build before the clock

Before the event, only prepare infrastructure and proof harnesses—not the product idea or finished product unless the rules explicitly allow it:

- Verify Agent37 API key, wallet and instance budget.
- Verify instance creation, health, chat, files, fork and deletion.
- Prepare environment-variable names and a safe demo-data directory.
- Prepare a generic event log and result schema.
- Prepare a generic recording/checklist script.
- Confirm any prebuilt code or templates are allowed by the organizers.

Keep all secret values in hsec. Never put keys in repo files, command arguments, logs, screenshots or video.

## 5. Two-hour execution plan

The official build window is approximately 2:30–4:40 PM. Use hard timeboxes:

### 2:30–2:40 — Freeze the target

- Pick one workflow.
- Write the one-sentence replacement.
- Choose one happy path and one failure/edge case.
- Define the proof metric.

### 2:40–3:00 — Make the trigger and data real

- Connect the input.
- Load only the minimum source records.
- Confirm the agent can retrieve them.
- Save a deterministic fixture as a fallback.

### 3:00–3:35 — Build the action loop

- Implement the smallest gather → decide → act path.
- Prefer structured outputs and explicit rules.
- Log every source and action.
- Do not polish UI yet.

### 3:35–3:55 — Add proof

- Add before/after state.
- Add source links or evidence.
- Add one measurable number: minutes, dollars, errors, cases, or success rate.
- Add a visible failure or human-approval boundary where appropriate.

### 3:55–4:15 — Integrate sponsors

- Verify Agent37 is genuinely in the runtime path.
- Verify at least one other sponsor is genuinely used.
- Remove any integration that is flaky or ornamental.

### 4:15–4:25 — Rehearse

- Reset to a clean state.
- Run the exact happy path.
- Capture the final demo video.
- Record a backup immediately if the run succeeds.

### 4:25–4:40 — Freeze and submit

- Stop changing the core path.
- Verify every submitted link in a private/incognito test.
- Submit the video, workflow statement, sponsor list, repo/live link and files.
- Keep a local copy of the submission contents.

## 6. Demo structure

Keep the video under two minutes:

1. **0:00–0:10 — Pain:** show the old workflow and its cost.
2. **0:10–0:20 — Trigger:** show the real input arriving.
3. **0:20–1:10 — Agent:** show source retrieval, reasoning checkpoints and the actual action.
4. **1:10–1:35 — Proof:** show the changed record/artifact and evidence links.
5. **1:35–1:50 — Value:** state the measured time, cost or error reduction.
6. **1:50–2:00 — Product:** say who would pay and why this replaces the old workflow.

Do not claim a live run if the video is prerecorded. If a result is simulated or synthetic, label it on screen.

## 7. Proof checklist

Before calling it done:

- The input is real or clearly labelled synthetic.
- The agent uses the data shown in the demo.
- The final output is independently inspectable.
- The before/after state is visible.
- The result survives a clean reset.
- The sponsor calls are observable in logs or output.
- Failure states are honest and handled.
- No secret, personal data or private customer material is visible.

## 8. Cut order

If a milestone slips by more than 10 minutes, cut in this order:

1. Extra integrations.
2. Multi-agent orchestration.
3. Polished dashboard and animations.
4. Extra workflows and edge cases.
5. Fancy metrics.
6. Live external data, only if a cached fixture is clearly labelled.

Never cut the core action, proof of result, reset path or demo recording.

## 9. Submission copy template

**Workflow replaced:** [one sentence]

**Before:** [who does what, using which tools, how long it takes]

**After:** [what the agent does and what artifact/state it produces]

**Proof:** [source data, before/after, measurable result]

**Sponsor integrations:** [each sponsor and its real role]

**Links:** [public live app] · [public repo] · [demo video]

**Limitations:** [what is synthetic, mocked, test-mode or human-approved]
