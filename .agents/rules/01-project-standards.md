---
name: Project Standards and Guardrails
description: Mandatory rules for committing and testing
---

# Project Standards and Guardrails

As an AI agent operating in this repository, you **MUST** strictly adhere to the following rules at all times. Failure to follow these rules violates the core project constraints.

## 1. Commit Message Standards (Conventional Commits)
All commit messages you write MUST follow the Conventional Commits format. The repository uses Husky to enforce this, and your commits will fail if you ignore this rule.

**Format:**
```
<type>(<scope>): <subject>

<body (optional)>
```

**Types Allowed:**
- `feat`: A new feature
- `fix`: A bug fix
- `docs`: Documentation only changes
- `style`: Changes that do not affect the meaning of the code (white-space, formatting, etc)
- `refactor`: A code change that neither fixes a bug nor adds a feature
- `perf`: A code change that improves performance
- `test`: Adding missing tests or correcting existing tests
- `chore`: Changes to the build process or auxiliary tools

**Requirements:**
- The message MUST accurately describe what *actually changed in the diff*, not just the intent.
- Do NOT say "implement X" if X already existed and you merely modified it.
- Do NOT say "Deprecate Y" if you didn't actually remove Y.

## 2. Mandatory Frontend Testing
There is zero tolerance for untested React components in the Electron frontend (`desktop_app`).

**Rule:**
- If you create or significantly modify a React component in `desktop_app/src/`, you **MUST** ensure there is a corresponding Jest test file (e.g., `ComponentName.test.jsx`) that renders the component and verifies it receives/displays data correctly.
- Do NOT submit a frontend feature without verifying it with a test. The pattern of "looks wired, but isn't" must be caught by automated tests.

## 3. Truthfulness in Documentation
When updating architectural documentation or summarizing work:
- Do NOT hallucinate changes.
- Ensure all architectural decisions are documented in `docs/adr/`, NOT `ADR/`.
