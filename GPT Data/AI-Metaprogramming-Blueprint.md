# AI metaprogramming blueprint

Source: https://chatgpt.com/c/68993a76-d26c-832b-8f5d-8e4a4f4b7773?src=history_search

Recovered: October 7, 2026.

Messages: 2 (1 user; 1 assistant).

Recovery verification: opened the source separately, repeatedly loaded older messages to the opening, and verified the final source message. Overlapping captured batches form one continuous chronological sequence. Message IDs were deduplicated; original wording and errors are retained with formatting reconstructed as Markdown. Attachment binaries and alternate branches are not embedded.

---

## 1. User

<!-- Message ID: user-before-6c56f11b-68d4-4441-b27f-953752d159ff -->

We want to create a github clone website that allows for deeper AI metaprogramming. If we're planned to create a slim code repo website without fancy features like error correction or even metaprogramming, it would be an easy template to build from... focus on the features we need, remove the heavy restrictions for LLM metaprogramming, then build the layers of additional safety and advanced features afterwards. The AI should have full capabilities to modify files and folders and the entire repository once given permission with API calls, and even a local mode. Agentic workflows for the ai should be native. Perhaps at first we can have an AI/user environment separation, where the user sets the goals, approves tasks, workflows and saves versions. The user refines goals and tasks and the AI updates. Give me a full blueprint for creating this metaprogramming repository.

---

## 2. Assistant

<!-- Message ID: 6c56f11b-68d4-4441-b27f-953752d159ff -->

Alright, Doctor of Digits—let’s grow a GitHub that drinks espresso shots of agency. Think: a repo host that treats “AI metaprogrammer” as a first‑class citizen, where commits can be *authored by workflows*, goals are versioned artifacts, and the model has true file‑system authority—but only within capability tokens you approve. Minimal first, then stack the wizardry.

I’ll give you a crisp, end‑to‑end blueprint you can start building tomorrow: system principles, MVP scope, architecture, data models, APIs, agent DSL, UI flows, local/offline mode, safety model, and a phased roadmap. And yes—native agentic workflows, user/AI separation, and “full repo write” *after* explicit grant. ⚡️



---



# 0) Design Principles (guardrails for everything else)



- **Human goals, AI labor.** Users define goals → AI plans tasks → AI proposes or applies patches → user approves (configurable).

- **Default‑deny capability security.** The AI never gets file write perms until you mint a scoped, expiring capability token (path & operation constrained).

- **Deterministic artifacts.** Every AI run yields: inputs (prompts, goals), tools used, diffs, test logs, and a provenance bundle to re‑execute the run.

- **Repo is the source of truth.** Goals, plans, and workflow specs live *in the repo* (versioned YAML + markdown). No hidden state.

- **MVP not monolith.** Start slim: repos + auth + git ops + AI runs + approvals. Add ergonomics later (code review heuristics, static analysis, etc).



---



# 1) MVP Scope (what ships first)

**Must‑have**



- Repo hosting (private/public), branches/tags, basic issues.

- **AI sidecar**: chat + tools, can *read* repo by default.

- **GoalSpec v0** (YAML) + **Flowfile v0** (workflow DSL) stored in repo.

- **Agent runner** in a sandbox (container/VM) with tool plugins: fs.read, git.diff, git.applyPatch, run.command, test.run.

- **Capability tokens** (object‑capabilities): path‑scoped, action‑scoped, TTL; required for fs.write|delete|move, git.commit.

- **Approval queue**: user reviews AI proposals (diffs + run logs) → approve/decline. Configurable auto‑merge for non‑critical scopes.

- **Local mode** daemon for offline work.

**Nice‑to‑add (Phase 1.5)**



- Semantic code search (commit‑scoped embeddings).

- Repo‑pinned toolchain images (Devbox/Containerfile) for reproducible AI runs.

- Live patch preview in Monaco editor.

- Multi‑agent orchestration (planner ↔ editor ↔ tester).



---



# 2) Architecture Overview

`┌─────────────────────────────────────────────────────────────────┐
│ Web App (Next.js): Repo UI, AI Sidecar, Approval Queue, Diffs  │
└───────────────▲───────────────────────────────▲──────────────────┘
                │ GraphQL/REST                  │ WebSocket (events)
┌───────────────┴───────────────┐         ┌─────┴──────────────────┐
│ API Gateway (Rust/Go)         │         │ Event Bus (NATS/Kafka) │
│ AuthN/AuthZ, Capabilities     │         │ Runs, Steps, Logs      │
└───────▲───────────┬───────────┘         └───────────▲────────────┘
        │           │                                 │
        │           │                                 │
┌───────┴───┐  ┌────┴─────────┐                ┌──────┴───────────┐
│ Repo Svc  │  │ AI Orchestr. │                │ Runner Fleet     │
│ (libgit2) │  │ (planner)    │                │ (containers/VMs) │
│ git/refs  │  │ assemble plan│                │ tool plugins     │
└─────▲─────┘  └──────▲───────┘                └──────▲───────────┘
      │ Blob store     │                               │ mounts ephemeral clone
┌─────┴────────────┐  │                         ┌──────┴──────────────┐
│ Metadata (PGSQL) │  │                         │ File System Overlay │
│ users, tokens,   │  │                         │ (copy-on-write)     │
│ runs, proposals  │  │                         └─────────────────────┘
└──────────────────┘  │
                      │
               ┌──────┴───────┐
               │ Local Daemon │ (offline mode; same APIs)
               └──────────────┘`**Tech stack picks (pragmatic)**



- **Backend**: Rust (Axum) or Go (Fiber); both have strong libgit2/gix/go-git bindings.

- **DB**: Postgres (metadata), MinIO/S3 (large artifacts), disk for bare git repos.

- **Event bus**: NATS (simple, fast) to stream run logs/steps to the UI.

- **Runner**: containerized worker (Docker/Podman) with an overlay FS, no host writes without capability.

- **Frontend**: Next.js + TypeScript, Monaco editor, GraphQL (Hasura/Nexus) or clean REST + tRPC.



---



# 3) Core Data Model (entities you’ll actually query)

**User**



- id, handle, role

- keys (SSH/GPG), oidc_sub (optional)

**Repo**



- id, name, visibility

- default_branch

- settings (AI policies, required approvals, allowed tools)

- git_path (server path to bare repo)

**CapabilityToken**



- id, repo_id, issued_to (user|agent_id)

- scopes: e.g., ["fs.write:/src", "git.commit", "branch.create:ai/*"]

- ttl, issued_at, revoked_at

- constraints: branch regex, file glob, max diff size, require tests pass

**Goal**



- id, repo_id, path (YAML in repo), status (draft/active/done)

- spec_hash (content-address for reproducibility)

**Run**



- id, goal_id, agent_id, inputs_digest

- state (queued/running/succeeded/failed/needs_approval)

- artifacts (logs, plan.md, patches), provenance (tools+versions)

**Proposal**



- id, run_id, branch, diff_stats

- tests_summary

- requires_approval (bool), approved_by, merged_at

**Agent**



- id, name, tools_allowed, model_provider, container_image



---



# 4) Capability Security (object‑capability model)



- **Minting**: Users mint capability tokens (JWT/macaroons) bound to *repo*, *path globs*, *ops*, *branch regex*, *TTL*, *max diff bytes*, *require tests pass*.

- **Presentation**: The AI runner must attach the token to any write/commit API call. Gateway enforces constraints server‑side.

- **Escalation path**: Agents can request *additional* scopes; requests land in the approval queue with justification + proposed diff footprint.

**Example token (JWT claims)**

JSON`{
  "sub": "agent:refactor-bot",
  "repo": "acme/calc",
  "scopes": ["fs.write:/src/**", "git.commit", "branch.create:ai/**"],
  "constraints": {
    "branch": "ai/.*",
    "max_diff_bytes": 200000,
    "require_tests_pass": true
  },
  "exp": 1735689600
}`

---



# 5) GoalSpec v0 (YAML in the repo)

Keep it legible; capture intent, constraints, and acceptance.

YAML`version: 0
id: goal-add-ci-tests
title: "Add unit tests for Parser and enable CI"
context:
  repo_map:
    include: ["src/parser/**", "tests/**", "pyproject.toml"]
  non_goals:
    - "Refactor parser internals"
constraints:
  language: ["python>=3.11"]
  test_cmd: "pytest -q"
  coverage_min: 85
permissions:
  request:
    - "fs.write:/tests/**"
    - "git.commit"
    - "branch.create:ai/**"
acceptance:
  - "New tests pass locally"
  - "Coverage >= 85%"
  - "No changes outside tests/**"`

---



# 6) Flowfile v0 (Agentic workflow DSL)

A declarative graph that the orchestrator executes and logs step‑by‑step. Steps are *tools*; tools are versioned plugins.

YAML`version: 0
name: "goal-add-ci-tests"
on:
  trigger: manual   # (later: schedule, events, PR)
runner:
  image: "ghcr.io/yourorg/py-ai-runner:0.1.0"
  cpu: "2", mem: "4Gi", timeout: "20m"
steps:
  - id: index
    tool: code.index
    with:
      paths: ["src/parser", "tests"]
  - id: plan
    tool: llm.plan
    needs: [index]
    with:
      prompt: "Propose test cases for edge conditions; output plan.md"
  - id: edit
    tool: llm.edit.applyPatch
    needs: [plan]
    with:
      target: "tests/"
      strategy: "surgical"
      capability: "fs.write:/tests/**"
  - id: test
    tool: run.command
    needs: [edit]
    with:
      cmd: "pytest -q"
    onFail: review
  - id: propose
    tool: repo.propose
    needs: [test]
    with:
      branch: "ai/tests-parser"
      title: "AI: Add Parser tests"
      requireApproval: true`**Tool contract (uniform)**

TypeScript`type ToolIO = {
  inputs: Record<string, string|binary>;
  params: Record<string, any>;
  outputs: Record<string, string|binary>;
  logs: string[];
}`

---



# 7) APIs (selected endpoints)

**Auth & tokens**



- POST /auth/login (OIDC/local)

- POST /capabilities → mint token (scopes, constraints)

- DELETE /capabilities/:id → revoke

**Repos**



- POST /repos → create

- GET /repos/:id/tree?path=...

- GET /repos/:id/file?path=...

- POST /repos/:id/patch (requires capability; server validates constraints)

- POST /repos/:id/branch (capability)

- POST /repos/:id/merge (approval gated)

**AI**



- POST /ai/runs {repo, goal_path, flow_path, agent_id}

- GET /ai/runs/:id/stream (WS/NATS proxy)

- POST /ai/proposals/:id/approve → merges branch if tests pass

- POST /ai/escalate → request new capability with justification

**Search (Phase 1.5)**



- POST /search/semantic {query, commit} → code chunks + paths



---



# 8) Runner & Tooling (how the AI actually edits code)



- **Sandbox**: each run creates an ephemeral clone at commit X with overlay FS. Tools cannot write host—only via repo.patch API using the capability token.

- **Core tools (v0)**:
  
  - code.index → builds symbol map, file list (optionally embeddings)
  
  - llm.plan → converts GoalSpec + index into a plan.md (no writes)
  
  - llm.edit.applyPatch → proposes patch files; *submits* via repo.patch
  
  - run.command → executes inside container (e.g., pytest, npm test)
  
  - repo.propose → opens a proposal (like PR), attaches artifacts

- **Determinism knobs**: pin runner image, model version, seeds, tool versions.



---



# 9) UI/UX (clean, surgical, native‑agent)



- **Repo View**: file tree, branches, diffs, issues.

- **AI Sidecar** (right panel):
  
  - Chat with context (current branch/paths).
  
  - “Request capability” button lights up when write needed.
  
  - Live step logs (Plan → Edit → Test → Propose).

- **Goal Board**: list of GoalSpecs in repo; click to run/inspect history.

- **Approval Queue**:
  
  - Proposal card: diff, tests summary, constraints satisfied, capability used.
  
  - Approve → server merges; Decline → leave inline comments; Re‑run with changed capability.

- **Local Mode Switch**: toggles remote vs local daemon seamlessly.



---



# 10) Local Mode (offline & sovereign)



- **Daemon**: metagitd with same API surface; stores repos in ~/.metagit/repos.

- **Auth**: local keyring; capabilities still enforced (you can “god mode” if owner).

- **Air‑gapped**: models and tools packaged in OCI images; cache model weights.

- **Sync**: optional push/pull to remote instance when re‑connected.



---



# 11) Safety Model (MVP → Advanced)

**MVP safety**



- **Hard capability gates** (path/ops/branch/TTL/max diff).

- **No blind writes**: all writes pass through repo.patch, which validates and records constraints.

- **Reproducible runs**: provenance bundle (goal yaml, flow yaml, model+tool versions, container digest, inputs).

- **Rollback**: proposals are on ai/* branches; merges are fast‑revertable.

**Phase 2+**



- Static analysis & policy (e.g., “never touch infra/ without owner approval”).

- Test coverage gates (coverage diff awareness).

- Secrets scanner (fails proposals if leakage).

- Policy‑as‑code (Rego/Cedar) for approvals and capability issuance.



---



# 12) Implementation Milestones (build order)



1. **Skeleton host**: users, repos, branches, diffs, basic PR/proposal.

2. **Runner + tool API**: container runs, stream logs, code.index, run.command.

3. **Capabilities**: minting UI + gateway enforcement + patch API.

4. **AI sidecar**: chat wired to read‑only tools; escalate on write.

5. **GoalSpec + Flowfile** in‑repo; “Run Flow” button; approval queue.

6. **Local daemon** with identical APIs.

7. **Semantic search** (embeddings per commit) + fast code nav.

8. **Multi‑agent orchestration** + policy gates.



---



# 13) Example End‑to‑End (what it feels like)



1. You add /goals/add-parser-tests.yaml & /flows/add-parser-tests.yaml.

2. In the UI, click **Run**. Sidecar plans test cases → shows plan.md.

3. Agent asks for capability: fs.write:/tests/**, git.commit, branch.create:ai/**, TTL 2h.

4. You approve. Agent generates patches → submits via repo.patch.

5. Runner executes pytest → passes; opens **Proposal #17** on ai/tests-parser.

6. You review diff + logs → Approve. Server merges → provenance bundle attached to merge commit.



---



# 14) Minimal OpenAPI Snips (to ground the contract)

YAML`paths:
  /repos/{id}/patch:
    post:
      security: [{ bearerAuth: [] }]
      requestBody:
        content:
          application/json:
            schema:
              type: object
              required: [baseCommit, patches]
              properties:
                baseCommit: { type: string }
                branch: { type: string }
                patches:
                  type: array
                  items:
                    type: object
                    properties:
                      path: { type: string }
                      action: { enum: [create, update, delete, move] }
                      contentBase64: { type: string }
      responses:
        "200": { description: "Patch applied as staged proposal" }
  /ai/runs:
    post:
      requestBody:
        content:
          application/json:
            schema:
              type: object
              required: [repo, goalPath, flowPath, agentId]
      responses:
        "201": { description: "Run created" }`

---



# 15) Agent Tooling: contracts you’ll implement first



- code.index(paths) → returns file list, sizes, maybe a symbol outline.

- llm.plan(prompt, context) → pure function (no writes).

- llm.edit.applyPatch(target, strategy, capability) → yields unified diffs; *submits* via API.

- run.command(cmd) → captures stdout/stderr, exit code.

- repo.propose(branch, title) → creates proposal and attaches artifacts.

**Design choice**: keep LLMs stateless; store “memory” as repo artifacts (/docs/ai/plan.md, /docs/ai/context.json) so agents inherit context by reading the repo.



---



# 16) Observability (don’t skip this)



- **Per‑step logs** to event bus; UI streams live.

- **Metrics**: run duration, tokens used, pass/fail rate, revert rate, diff size percentiles.

- **Audit**: who minted which capability, used by which run, for which patches.



---



# 17) Extensions (after MVP lands)



- **Graph planner**: Goal dependency graph (build → test → deploy) with cross‑repo orchestration.

- **PR heuristics**: auto‑split large diffs into logical commits.

- **Agent marketplace**: installable agents with declared tool needs & policies.

- **Policy simulation**: “Dry‑run policy” to see what would be allowed/blocked.

- **Binary Decomposition Interface (future)**: map capability ops to low‑level substrate on your BDI OS; agents talk directly to smart kernels—fun later.



---



# 18) Threat Model (fast but not reckless)



- **Model misbehavior**: contained by capabilities + sandbox + approval gates.

- **Supply chain**: pin runner images & tool hashes; provenance bundle in merges.

- **Secrets**: never mount credentials into runner unless explicitly declared in Flowfile with secret IDs; scan outputs before proposal.



---



# 19) Dev Ergonomics (DX so good it feels illegal)



- metagit CLI: metagit goal run, metagit token mint, metagit propose approve.

- Repo bootstrap: metagit init --with-ai creates /goals/ and /flows/ templates.

- Local mode: metagit d start spins the daemon; UI connects automatically.



---



# 20) Sample Repo Layout

Code`.
├─ .metagit/               # repo-level config (policies, allowed tools)
├─ goals/
│  └─ add-parser-tests.yaml
├─ flows/
│  └─ add-parser-tests.flow.yaml
├─ docs/ai/
│  ├─ plan.md
│  └─ run-logs/
└─ src/, tests/, ...`

---



# 21) Build Notes (what to actually code first)



- Start with **git service** using libgit2/gix: list tree, read file, create branch, apply patch (server‑side), produce proposals as refs + metadata row.

- Implement **capability middleware** at the API Gateway; reject any write without a valid token meeting constraints.

- Ship **Runner** with only three tools: code.index, llm.plan, llm.edit.applyPatch. Keep edit tool dumb: it emits unified diffs; *server* validates and stages.

- Wire **Approval queue** to a simple “merge if green” button; CI = run.command inside runner against the proposal’s branch.

- Add **Local daemon** by reusing the same services in single‑process mode.



---

If you want, I can cough up a reference repo (server, runner, web) skeleton next—Rust API + Next.js UI + Python runner—with the exact directory structure, build scripts, and a working end‑to‑end flow using a dummy llm.plan mock. Just say **/mode Tech Surgeon** and I’ll drop code you can compile. 🧪🛠️
