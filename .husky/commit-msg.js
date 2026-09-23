const fs = require('fs');

const msgPath = process.argv[2];
const msg = fs.readFileSync(msgPath, 'utf8').trim();

// Check if message starts with conventional commit type
const conventionalCommitRegex = /^(feat|fix|test|refactor|docs|chore|perf|ci)(\(.+\))?: /;

if (!conventionalCommitRegex.test(msg)) {
  console.error("❌ Commit message must follow Conventional Commits format");
  console.error("Example: feat(frontend): add diagnostics UI integration");
  console.error("Allowed types: feat, fix, test, refactor, docs, chore, perf, ci");
  process.exit(1);
}

if (msg.length < 20) {
  console.error("❌ Commit message must be at least 20 characters");
  process.exit(1);
}

process.exit(0);
