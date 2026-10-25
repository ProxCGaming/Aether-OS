const fs = require('fs');

const msgPath = process.argv[2];
const msg = fs.readFileSync(msgPath, 'utf8').trim();

// Check if message starts with conventional commit type
// NOTE: this list MUST match `.agents/rules/01-project-standards.md` §1
// (the standards doc is the source of truth; this hook enforces it).
// `style` is a real type here (used in history); `ci` is intentionally
// NOT a separate type — CI/workflow changes use `chore` ("changes to the
// build process or auxiliary tools", per the standards doc).
const conventionalCommitRegex = /^(feat|fix|test|refactor|docs|chore|perf|style)(\(.+\))?: /;

if (!conventionalCommitRegex.test(msg)) {
  console.error("❌ Commit message must follow Conventional Commits format");
  console.error("Example: feat(frontend): add diagnostics UI integration");
  console.error("Allowed types: feat, fix, test, refactor, docs, chore, perf, style");
  process.exit(1);
}

if (msg.length < 20) {
  console.error("❌ Commit message must be at least 20 characters");
  process.exit(1);
}

process.exit(0);
