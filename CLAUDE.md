# CLAUDE.md

Rules for AI coding agents working in this repository.

## What this is

PayOps Agent: an AI agent that investigates stuck payouts against a mock payments API, cites runbooks, and only performs money-moving actions after human approval. It is also a learning project; the milestone plan is in `docs/learn/00-roadmap.md`.

## Hard rules

1. **Synthetic data only.** Never add real company, partner, customer or person data, and never copy code or documents from an employer or any third party. Use the existing fictional placeholders (Northwind, Tailspin, Wingtip, Fourth Coffee, Fabrikam; `bank_alpha`/`bank_beta`/`bank_gamma`).
2. **Secrets stay in `.env`** (git-ignored). Never print, log, commit or paste a secret value, and never read `.env` into the conversation. Document new variables in `.env.example` with empty values.
3. **Money is `Decimal`**, serialized as a string. Never use floats for amounts.
4. **Side effects need approval.** Retrying a payout or resending a webhook goes through the two-phase approval flow (Milestone 6). Until that exists, agent tools are read-only.
5. **Labels never reach the agent.** `evals/` holds ground truth; runtime code must not read it.

## Workflow

- Start each milestone or feature with a plan in `docs/plans/NN-<slug>.md`: goal, design, test plan, acceptance criteria.
- Write tests first for domain rules (state machine, retry policy, idempotency), then implement.
- One coherent change per PR. Conventional commit messages that say what changed and why.
- PRs that touch money movement (retry policy, idempotency, approvals, ledger) get an independent review by a separate agent that sees only the code, not the author's conclusions.
- Prefer invariant tests over examples where a rule can be stated as one (e.g. "a balance traced back from today never goes negative"). For core rules, mutation-check the tests: break the rule on purpose and confirm a test fails. An assertion that also holds for an empty result needs a non-empty assertion next to it.
- When an AI mistake is worth learning from, add an entry to `docs/ai-dev-log.md`: what happened, how it was detected, the fix, and how to prevent it.

## Check facts that change

Model names, API parameters, prices and dependency versions change faster than an AI's training data. Look them up in current docs or the latest release (`gh api repos/<owner>/<repo>/releases/latest`) before writing them down, and confirm with a real call when one is cheap (for example, listing models). Record the source and the date next to facts that will go stale, such as prices.

## Verify before claiming done

Run `make check` (ruff lint and format check, mypy strict, pytest) and report the actual result. For LLM features, also run `make test-live` when a key is configured. Do not say something works without having run it.

## Boundaries

- `services/mock-pay` is the "external system". The agent talks to it over HTTP only and must never import `mock_pay`.
- `services/agent` holds the agent and the LLM adapter.

## Language

Code, comments, commit messages, PRs and the README are in English. Learning notes, plans and the AI dev log (`docs/learn`, `docs/plans`, `docs/ai-dev-log.md`) are in Chinese.
