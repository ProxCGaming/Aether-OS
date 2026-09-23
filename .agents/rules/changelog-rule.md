# Auto-Update Changelog

Whenever you execute a code change, bug fix, or new feature implementation, you MUST automatically update the `CHANGELOG.md` file located at the project root. 

Follow these strict requirements for every change:
1. Locate the `CHANGELOG.md` file. If it does not exist, create it.
2. Add a new entry under the "[Unreleased]" section (create the section at the top if it's missing).
3. Use the format (including the current local timestamp):
   - `- [YYYY-MM-DD HH:MM] Fixed: [Description]` (for bug fixes)
   - `- [YYYY-MM-DD HH:MM] Added: [Description]` (for new features)
   - `- [YYYY-MM-DD HH:MM] Changed: [Description]` (for refactors/modifications)
4. Execute this update immediately alongside your code changes, without asking for permission or a secondary prompt.
5. If you are creating a git commit, ensure the `CHANGELOG.md` file is staged and included in the exact same commit.
