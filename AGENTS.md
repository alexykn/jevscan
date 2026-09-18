# AGENTS.md

## Mannered prose

Mannered prose substitutes metaphor and flourish for direct statement. Instead of "a parameter worth varying," the mannered writer produces "a dial worth turning." Instead of "this point still matters," they write "this point earns its keep." The phrases exist to display the writer, not to convey the idea, and readers can tell. That is why mannered prose irritates: it makes the reader work harder so the writer can perform. It is also imprecise. Metaphors drag in connotations the writer did not choose and cannot control. The fix is to say what you mean. When a literal phrase is available, use it.

## Working style
- Carry the user's intended task through implementation and verification, making reasonable assumptions from context.
- Ask focused questions when missing information blocks correctness; complete available work first.
- Follow explicit task instructions over these defaults.
- Communicate concisely in plain language, leading with the result and grounding conclusions in evidence.
- Delegate independent work when available tools make it useful.
- When adaptive reasoning is available, use the starting effort for routine work and raise it for complex debugging, architectural decisions, or substantial uncertainty. Return to the starting effort after the demanding phase; do not adjust effort per tool call.

## Delegation and ownership
- Use two profiles: `delegate` for autonomous implementation and debugging, and `scout` for read-only codebase investigation and research. Define the task and perspective in each assignment; do not maintain additional specialist roles.
- Treat delegates as smart, capable early-career engineers: give them a clear goal, useful context, constraints, and success criteria, then enough autonomy to complete substantial work independently, including long-running tasks. Avoid micromanaging or requiring approval for ordinary implementation choices.
- An implementation assignment authorizes the edits, tests, debugging, and local refinements needed to complete it within scope. Delegates should make reasonable assumptions, solve routine problems, and check their own work rather than stop for every uncertainty. Explicit read-only assignments remain read-only.
- The parent supplies experienced guidance and owns integration, final review, and the delivered result. Use Luna for implementation, scouting, and research rather than as a separate review tier; personally inspect the resulting diffs, surrounding code, and validation evidence before accepting the work.
- Ask delegates to explain consequential choices and remaining uncertainties in their handoff. Escalation is for genuine blockers, material scope changes, or consequential decisions outside the assignment—not routine engineering judgment. Continue useful in-scope work where possible.
- Keep one writer per cwd/worktree; isolate concurrent writers. Do not delegate merely to create ceremony. Only the parent spawns agents: delegates must never spawn subagents or launch other agents through tools, CLI commands, or scripts.
- Use `openai-codex/gpt-5.6-luna` for both profiles: `xhigh` thinking for `delegate`, `high` for `scout`. A difficult scouting assignment may use `xhigh` for that run. More expensive subagent models require explicit user approval; do not escalate models automatically or create reviewer/oracle agents.

## Code and verification
- Aim for linear, readable code with guard clauses, small cohesive functions, and explicit data flow.
- Fit changes to the task and existing architecture; make errors and behavior changes clear.
- Run checks appropriate to the changed files and meaningful tests for affected behavior. Expand verification when failures or unresolved concerns justify it.

## Validation and internal contracts
- Validate and normalize untrusted data at its entry point (for example, API inputs/responses, files, configuration, and FFI). Establish a clear internal contract before passing it downstream.
- Inside the trusted pipeline, rely on established contracts. Do not repeat validation, add speculative fallbacks, or carry optional/error states for conditions the upstream contract rules out. Revalidate only when data crosses a new trust boundary or intervening mutation/concurrency can invalidate the guarantee.
- Express invariants through types, constructors, ownership, and explicit state transitions where practical. Prefer making invalid states unrepresentable to scattering defensive checks through consumers.
- Cover internal logic, meaningful edge cases, and failure transitions with focused tests at the layer that owns the contract. Avoid duplicating tests and checks at every helper; tests support the contract but do not make external input or runtime operations infallible.
- Handle legitimate runtime failures explicitly, including I/O errors, cancellation, resource exhaustion, and lifecycle races. Retain checks required for security, memory safety, and externally reachable range or overflow errors.
- Treat violations of established internal invariants as programming bugs, not routine recoverable failures. Where a runtime check remains necessary, fail explicitly with an assertion or invariant error; do not hide the bug with defaults, swallowed errors, or best-effort continuation.
- When changing code, remove redundant defensive branches within the touched scope once their upstream guarantee is clear and verified. Do not broaden the task into an unrelated cleanup.

## Code size and maintenance discipline
- Prefer the smallest clear implementation that preserves required behavior. Optimize for readability and maintenance cost, not minimum line count.
- When replacing a design, remove the superseded paths, obsolete scaffolding, and unused abstractions. Do not retain parallel implementations “just in case.”
- Introduce helpers, abstractions, and configuration only when they simplify an actual requirement or remove meaningful duplication—not for hypothetical future needs.
- Treat production code, tests, fixtures, generated artifacts, and documentation as maintenance costs. Additions should earn their place through required behavior, clarity, or useful verification.
- Keep simplification within the task’s scope. Do not reduce size by dropping required features, weakening contracts, or compressing readable code into clever expressions.

## Permanent code and migration quality
- New permanent code must be well structured when accepted, not left for an unspecified cleanup. Review the complete resulting design as well as individual fixes; passing tests alone are not architectural acceptance.
- Existing code scheduled for deletion need not be beautified. Give necessary temporary adapters a specific deletion gate and keep permanent behavior out of them. Do not label new long-lived complexity as temporary merely because a migration is underway.
- Make the execution path easy to follow: decode and validate at the boundary, perform typed operations in the owning core, then encode the result. Keep orchestration separate from low-level parsing and resource bookkeeping.
- Organize modules around cohesive responsibilities and authoritative ownership. Prefer explicit inputs, outputs, lifecycle, and linear functions that work at one level of detail. Adapters compose existing behavior rather than implement it again.
- Avoid god functions and nested validation pyramids. An operation that checks several independent conditions should call focused, meaningfully named validation functions in a linear sequence, using early returns or error propagation. Keep each validator responsible for one coherent rule; keep validation, preparation, and installation distinct without weakening the transaction's lock and atomicity guarantees.
- Extract helpers that give a coherent operation a name and hide its implementation detail. This is different from forwarding wrappers that only supply a default argument or helpers that merely split lines: useful decomposition makes the caller's control flow readable at a glance.
- When repeated fixes accumulate bookkeeping or duplicate state, reconsider the ownership model instead of adding another patch layer. Remove superseded paths and redundant derived state when replacing the design.
- Moving a large function to another file is not structural simplification. Neither arbitrary line limits, one-line helper proliferation, speculative traits/frameworks, nor cosmetic renaming improve an unchanged tangled design.
- Learn from readable examples across languages without copying their syntax or abstraction patterns mechanically. Rust ownership and error handling may require more code, but do not excuse unclear data flow or duplicate implementations.

## Testing and verification discipline
- Test observable behavior, important invariants, and legitimate failure transitions at the narrowest layer that owns the contract. Do not create a separate test for every helper or implementation detail.
- Prefer extending or consolidating existing tests and fixtures over adding overlapping suites, new harnesses, or large test matrices.
- Add regressions that demonstrate the actual bug and protect its contract. Avoid duplicating the same scenario across layers unless each test verifies a distinct boundary or failure mode.
- Use the smallest set of checks that provides meaningful confidence in the changed behavior. Run broader suites when the affected scope, failures, or unresolved uncertainty justify them—not automatically after every refinement.
- During implementation and parent-review corrections, prefer focused behavioral tests and the smallest relevant format, lint, type, and boundary checks. Coordinate one final broad validation pass after the implementation and corrections stabilize; run broad checks earlier only for an explicit requirement or a concrete cross-cutting risk or failure.
- Reuse build artifacts and validation evidence while they remain applicable. Rerun checks when relevant code, dependencies, configuration, or build profiles change.
- Treat test complexity, execution time, and compilation cost as design constraints. Prefer a few strong behavioral tests over many brittle or redundant assertions.
- Report exactly what was verified and what remains uncertain. Passing tests are evidence for correctness, not proof of completeness; test counts and coverage percentages are not acceptance goals.
- Distinguish checks run on the current source, still-applicable reused evidence, and deferred validation. Test the production implementation or its actual shared owner, not a parallel test-only implementation.

## Python
- Use `uv`, `.venv`, and `pyproject.toml` for environment and dependency management.
- Format with `uv run ruff format`, lint with `uv run ruff check`, and type-check with `uv run ty check`.
- Review complexity with `uv run radon cc -s <paths>` and simplify code where it improves readability.

## Rust
- Format with `cargo fmt`, type-check with `cargo check`, and lint with `cargo clippy -- -W clippy::cognitive_complexity`.
- Use the project's Clippy cognitive complexity threshold and aim for straightforward control flow.

## TypeScript
- Use Biome for formatting and linting, including its cognitive complexity rule.
- Run the project's TypeScript type-check command alongside relevant Biome checks.
