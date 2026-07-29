# Project purpose and positioning

> Status: product rationale and strategic reference
> Last reviewed: 2026-07-29
> Scope: why Agent Debate Engine exists, what it should and should not become,
> and how it differs from general agent orchestration tools
>
> Companion presentation:
> [Agent Debate Engine onboarding deck](agent-debate-engine-onboarding.html)

## Executive decision

Agent Debate Engine should continue to exist, but as a small, specialized
decision kernel rather than a general managed-agent platform.

There are already many capable agent orchestration tools. They manage agents,
tasks, workflows, runtimes, retries, histories, notifications, and human
collaboration. This project should not exist merely to schedule several agents
or move work through another workflow.

It exists for a narrower reason: general orchestration does not by itself
guarantee a bounded, ordered, and auditable decision protocol with independent
proposals, forced criticism, a strict Judge contract, and deterministic
stopping rules.

The recommended long-term relationship with a general orchestration platform
is:

```text
General agent orchestration platform
human collaboration, issues, squads, runtimes, notifications
                    |
                    | triggers a decision task
                    v
Agent Debate Engine
ordered stages, independent proposals, critique, revision,
strict Judge validation, deterministic stopping, evidence
                    |
                    | returns status, synthesis, risks, artifact links
                    v
Platform timeline and human review workflow
```

Do not grow this repository into a competing issue tracker, Agent OS, distributed
runtime, or general workflow product.

The comparison later in this document uses Multica, one of the most visible
recent projects in this category, as a representative example. Multica is not
the sole reason for this project's positioning, nor the only platform with
which it may integrate.

## Why this project exists

The project is not a response to a lack of agent orchestration tools. The
original motivation is onboarding and decision quality.

New colleagues often need to make technical decisions before they have built
enough experience or prerequisite knowledge to evaluate an AI-generated answer.
The failure mode is not that AI refuses to answer. The failure mode is that it
answers fluently and confidently, while the user lacks the basis to distinguish
a sound proposal from a plausible but incomplete one.

A common sequence is:

1. A new colleague asks one AI for a solution.
2. The AI produces a coherent plan.
3. The colleague follows it because they cannot identify missing constraints,
   hidden operational cost, weak assumptions, or unsafe shortcuts.
4. The real defects appear only during implementation, review, or production.

This is an asymmetric-risk problem:

- the AI can generate more options than the colleague can evaluate;
- confident language is easily mistaken for evidence;
- a single conversation tends to preserve its own early assumptions;
- agreement with the user does not prove correctness;
- the cost of a bad decision is paid by the team, not by the model.

Agent Debate Engine changes the interaction from:

> Ask one AI for the answer, then decide whether to trust it.

to:

> Require several independent proposals, force adversarial criticism and
> revision, then give the human a structured decision package with unresolved
> risks and traceable evidence.

The system does not manufacture truth. It raises the quality of the evidence
surface available to a less-experienced human reviewer.

## The problem it solves

The project addresses four related gaps.

### 1. Missing experience

An inexperienced colleague may not know which failure modes, trade-offs, or
operational constraints deserve attention. Distinct roles expose the decision
to more than one reasoning path.

### 2. Missing prerequisite knowledge

The user may not know enough domain vocabulary to formulate the perfect prompt.
Independent proposals and a dedicated critic help surface missing concepts and
questions.

### 3. AI anchoring and confident error

A single model conversation can anchor on its first plausible idea. A debate
forces alternative hypotheses, explicit attacks, and revision before judgment.

### 4. Weak decision accountability

A chat transcript is an opaque stream. A debate run preserves the task,
participant prompts, outputs, failures, Judge decisions, terminal status, and
unresolved issues as inspectable artifacts.

## What it is

Agent Debate Engine is:

- a local CLI orchestrator for structured technical deliberation;
- a linear sequence of ordered stages with optional intra-stage concurrency;
- a way to obtain independent proposals before cross-contamination;
- an adversarial review loop: proposal, critique, revision, judgment;
- a strict Judge protocol whose output must pass schema and semantic validation;
- a deterministic stop evaluator that owns the final run state;
- a bounded process with round, time, context, output, retry, and concurrency
  limits;
- an auditable run format with immutable invocation attempts and resumable
  checkpoints;
- a decision-support tool for humans, especially colleagues who cannot yet
  confidently challenge an AI answer alone.

The default workflow is:

```text
validate and preflight
        |
        v
parallel independent proposals
        |
        v
critique
        |
        v
synthesis revision
        |
        v
schema-valid Judge decision
        |
        v
deterministic stop evaluator
        |
        +--> finalized
        +--> another bounded round
        +--> exhausted / blocked / timed_out
```

The Judge recommends. The engine decides whether the recommendation satisfies
the configured stopping criteria.

## What it is not

Agent Debate Engine is not:

- a guarantee that an answer is objectively correct;
- a replacement for domain experts, testing, production evidence, or human
  accountability;
- a mechanism for declaring truth because several models agree;
- a general multi-agent chat room;
- an issue tracker, project-management system, or employee-management product;
- a distributed worker platform;
- an arbitrary DAG or visual workflow builder;
- a default environment for several agents to edit the same codebase;
- a general secret broker or operating-system sandbox;
- a reason to give write access to models for a text-only design decision.

Role diversity, even across providers, is not independent ground truth. It is a
method for generating and challenging hypotheses.

## What a new colleague should receive

The useful output is not merely a longer answer. It is a reviewable decision
package:

- the current best recommendation;
- accepted decisions and the evidence supporting them;
- rejected options and why they were rejected;
- unresolved issues with severity;
- explicit next-round focus when more evidence is required;
- a truthful terminal state such as `finalized`, `exhausted`, `blocked`, or
  `timed_out`;
- exact prompts, outputs, failures, timings, and artifact paths for later
  inspection.

This package lets a new colleague ask better follow-up questions and gives a
senior reviewer a compact place to verify the decision.

## Appropriate use cases

Use the engine when:

- choosing between two or more architectural approaches;
- reviewing a migration, storage, security, reliability, or operational plan;
- challenging an implementation proposal before code is written;
- identifying hidden assumptions in a design produced by one AI;
- preparing a decision for senior review;
- the cost of a plausible but wrong answer is materially higher than the cost
  of several model calls.

Do not use it when:

- the task has one mechanical, easily tested answer;
- a repository test or authoritative document can settle the question directly;
- the user only needs task assignment and progress visibility;
- model cost and latency exceed the value of a second opinion;
- the team has no intention of reviewing the resulting evidence.

## Human responsibility

The engine improves the decision process; it does not transfer accountability
to AI.

The human owner remains responsible for:

- supplying the real constraints and non-goals;
- identifying authoritative repository or operational evidence;
- distinguishing verified facts from model inference;
- deciding when external expertise or experiments are required;
- reviewing unresolved critical issues;
- approving any transition from analysis to code or production change.

For onboarding, the desired behavior is not “the system decides for the new
colleague.” The desired behavior is “the system makes disagreement, evidence,
and uncertainty visible enough that the colleague can learn to judge.”

## The decisive reason: an external trust anchor

The deepest difference from a general agent orchestrator is not the number of
agents, the visual workflow, or whether stages can run in parallel. It is the
trust boundary: **who is allowed to declare that an AI-produced decision is
acceptable?**

Current evidence establishes two different authorities:

- **Fact — Multica owns execution truth.** Its server tracks task state,
  timeouts, retries, runtimes, and stage barriers. Its system-managed Squad
  protocol governs routing, while an agent leader evaluates updates and decides
  what work to dispatch next. Issue status transitions remain unrestricted and
  are updated by agents, people, or integrations.
- **Fact — Agent Debate Engine owns debate-protocol truth.** Model output is
  untrusted input. A strict parser validates the Judge contract, semantic rules
  reject contradictory decisions, and a pure deterministic evaluator—not the
  Judge model—decides whether the run is `finalized`, `continue`, `blocked`,
  `timed_out`, or `exhausted`.
- **Inference — these are complementary trust anchors.** Multica can prove that
  agents were scheduled and tasks ran. That is not evidence that a proposed
  technical decision passed a defined adversarial review protocol.

This distinction matters most for new colleagues. An experienced engineer may
notice that a critic was ignored, a Judge contradicted itself, or a critical
risk was waved away. The intended user of this project often cannot. Therefore
the guard cannot rely only on that user noticing the omission, and it cannot
rely only on another model claiming that the process was followed.

Put differently:

> A general orchestrator can reliably run five agents. This project supplies a
> sixth participant that cannot be persuaded by fluent language: deterministic
> code.

That code still cannot prove objective truth. Its narrower guarantee is
procedural: required stages ran in order, the decision conforms to a strict
contract, declared convergence satisfies explicit rules, unresolved critical
issues are not silently erased, and the evidence used for resume has not been
mutated.

### Concrete failure behavior

The difference becomes visible on failure paths, not on the happy path:

| Situation | General Multica workflow | Agent Debate Engine |
| --- | --- | --- |
| Every model agrees, but a critical issue remains | Tasks can complete and the coordinator can advance the issue | `finalize` is rejected; the run continues or ends `exhausted` |
| The Judge returns persuasive prose or malformed JSON | The agent task can still have produced an output | The output fails Judge v1 validation; bounded repair or failure follows |
| The last allowed round still asks to continue | All scheduled tasks may have completed successfully | The terminal state is `exhausted`, explicitly not consensus |
| Saved evidence changes before resume | A task may resume its session and working directory | Strict hash and checkpoint verification refuses unsafe resume |
| A participant silently fails | The coordinator can decide how to proceed | `require_all_participants` can make the round fail closed |

The smallest falsification test is to inject these failures into the same
models and prompts under both approaches. If a Multica-only configuration
blocks the same false-positive completions, preserves equally inspectable
reasons, and does so without recreating a policy kernel, this project should be
retired or reduced to configuration. If it does not, the failed cases—not the
happy-path demo—are the evidence that the kernel is necessary.

The key point is not that Multica is incapable of implementing these rules.
It can host a custom agent or tool that performs all of them. The key point is
that doing so requires adding a deterministic decision-policy component. Once
strict Judge validation, convergence evaluation, bounded context, and
integrity-checked evidence are implemented, the core of Agent Debate Engine has
been recreated inside Multica.

Therefore the honest architectural choices are:

1. keep the protocol as a small independent kernel and let Multica invoke it;
2. port the kernel into Multica as an explicit policy subsystem; or
3. delete the project if real evaluations show that prompt-level coordination
   performs just as safely for the team's decisions.

“Configure a Squad” is sufficient for choreography. It is not, by itself, a
replacement for a non-bypassable decision protocol.

## Representative comparison: current Multica

Multica is used here as a concrete, recent example of the broader agent
orchestration category. The purpose of the comparison is not to claim that
Agent Debate Engine competes only with Multica. It is to test whether a capable
general platform already makes this specialized project unnecessary.

The comparison is a point-in-time snapshot of Multica `main` at commit
`9e3b661d4494cfe5a66c5fdabcc24b14890d1eda` on 2026-07-29. Recheck upstream
before making a future build-versus-adopt decision.

### What Multica already does better

Multica is a managed-agent control plane. It already provides:

- agent identities, providers, skills, and runtime configuration;
- issues, projects, comments, notifications, and human collaboration;
- Squads with a leader that routes work to agents or people;
- local and cloud runtimes;
- task lifecycle tracking, timeout handling, retries, and reruns;
- session and working-directory reuse where providers support it;
- a richer web, desktop, and operational experience;
- a system-managed, non-editable Squad routing protocol;
- stage numbers on child issues and a server-detected stage barrier.

These capabilities should not be rebuilt here.

### What Multica can approximate

With a parent issue, a coordinator or Squad leader, and staged child issues,
Multica can model the visible shape of a debate:

```text
Stage 1: Architect + Alternative
Stage 2: Critic
Stage 3: Reviewer
Stage 4: Judge
```

The server can detect when every child in a stage reaches a terminal status and
wake the parent assignee. Multica can also retain task transcripts and retry
failed executions.

### What is still different

The current Multica stage mechanism is a coordination barrier, not a complete
decision protocol:

- stage advancement is agent-driven; the server wakes the leader, and the
  leader promotes the next `backlog` children;
- issue status transitions are deliberately unrestricted;
- the platform does not natively validate a debate-specific Judge schema;
- there is no deterministic convergence evaluator equivalent to
  `min_rounds`, `confidence_threshold`, `stable_rounds`, and critical unresolved
  issues;
- there is no built-in debate context builder that freezes independent prompts,
  labels evidence, orders it deterministically, and applies prompt budgets;
- task retries and session resume are not the same as a validated debate-round
  checkpoint;
- task histories are useful evidence, but they are not the same as the engine's
  immutable, content-hashed run ledger.

Therefore:

- configuration-only Multica can reproduce much of the user-visible workflow;
- it cannot reproduce the same guarantees without additional implementation;
- porting all guarantees into Multica would recreate the core of this project;
- invoking Agent Debate Engine from a Multica agent is the smaller integration.

### Primary Multica evidence

- [Multica repository and product overview](https://github.com/multica-ai/multica)
- [Squads documentation](https://multica.ai/docs/squads)
- [Issues and unrestricted status transitions](https://multica.ai/docs/issues)
- [Task lifecycle, retries, and session reuse](https://multica.ai/docs/tasks)
- [Stage column migration](https://github.com/multica-ai/multica/blob/9e3b661d4494cfe5a66c5fdabcc24b14890d1eda/server/migrations/123_issue_stage.up.sql)
- [Stage barrier and parent wake implementation](https://github.com/multica-ai/multica/blob/9e3b661d4494cfe5a66c5fdabcc24b14890d1eda/server/internal/handler/issue_child_done.go)

## Recommended product boundary

### Continue investing in

- provider-safe, shell-free invocation contracts;
- ordered stage execution and independence guarantees;
- strict Judge parsing and schema repair;
- deterministic terminal-state evaluation;
- bounded context, output, time, retries, and cost;
- immutable artifacts, integrity checks, and safe resume;
- concise decision reports for new colleagues and senior reviewers;
- narrow integrations that let orchestration platforms trigger a run and
  receive its result.

### Avoid investing in

- a second issue or project tracker;
- team membership, chat, inbox, or notification systems;
- distributed runtime management;
- a general agent marketplace;
- a large standalone management UI;
- arbitrary business workflows or a visual DAG editor;
- shared concurrent code-writing worktrees.

The local dashboard may remain a read-only evidence viewer. It should not grow
into a managed-agent platform.

## Example integration with Multica

Multica demonstrates how a minimal platform integration can preserve the engine
as the source of truth:

1. A Multica issue is assigned to a dedicated Debate Agent.
2. The agent converts the issue context into a bounded UTF-8 task file.
3. It invokes the safe `agent-debate` runner in the repository workspace.
4. The engine executes and owns stage order, Judge validation, stopping, and
   artifacts.
5. The agent posts back:
   - terminal status;
   - current best synthesis;
   - accepted and rejected decisions;
   - unresolved issues;
   - exact run and evidence locations.
6. Only `finalized` is eligible for a positive completion message.
   `exhausted`, `blocked`, and `timed_out` remain explicit review states.

In this example, Multica owns the work item and human attention. Agent Debate
Engine owns the decision protocol. The same boundary can apply to other agent
orchestration platforms.

## Continue-or-stop criteria

Continue the project if real users repeatedly need all of the following:

- independent alternatives rather than one generated answer;
- forced criticism and revision;
- an inspectable reason for stopping;
- unresolved risks preserved instead of smoothed over;
- evidence that a senior reviewer can audit later.

Archive or reduce the project to a thin platform integration if:

- users only want several agents assigned to tasks;
- staged tasks and a coordinator prompt are consistently sufficient;
- nobody inspects the debate artifacts;
- decisions are low-risk and cheaply testable;
- the strict protocol does not change outcomes or prevent false confidence in
  real onboarding cases.

A useful validation experiment is to run the same set of onboarding decisions
through:

1. one AI conversation;
2. a general orchestration workflow using prompt-driven coordination, such as
   a Multica Squad;
3. Agent Debate Engine.

Compare:

- hidden risks discovered;
- unsupported claims rejected;
- human interventions required;
- stage-order violations;
- false claims of consensus;
- time and model cost;
- whether a senior reviewer can reconstruct the decision.

The project earns its maintenance cost only if the stronger protocol produces a
materially better decision surface in real work.

## Durable one-sentence positioning

> Agent Debate Engine helps colleagues who cannot yet confidently judge an AI
> answer obtain a bounded, adversarial, and auditable decision package—without
> pretending that model agreement is truth.
