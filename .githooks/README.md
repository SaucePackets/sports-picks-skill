# Git Hooks

This repo uses `.githooks/` (committed directory) instead of `.git/hooks/` so the hooks are version-controlled.

## What's here

- **pre-commit** — checks trailing whitespace and merge conflict markers before every commit
- **pre-push** — blocks direct pushes to `main`; use feature branches + PRs

## Enable on fresh clone

```bash
git config core.hooksPath .githooks
```

Run once per clone. After that, hooks activate automatically on `git commit` and `git push`.
