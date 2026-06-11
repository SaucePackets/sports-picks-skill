## Summary

This curator pass consolidated 76 agent-created skills into a leaner, class-level library. 13 skills were archived total — 8 merged into class-level umbrellas and 5 pruned as platform-unsupported builtins.

### Clusters Processed

**1. Development Methodology (6 skills → 1 umbrella)**
Created `development-workflows` as the class-level umbrella for software development lifecycle processes. This single skill with 6 labeled subsections and full reference files is far more discoverable than 6 separate skills:
- `plan` — Implementation planning
- `spike` — Feasibility validation
- `test-driven-development` — RED-GREEN-REFACTOR discipline
- `systematic-debugging` — 4-phase root cause investigation
- `requesting-code-review` — Pre-commit security/quality gate
- `simplify-code` — Parallel 3-agent cleanup

Each original's full SKILL.md content was preserved as a `references/<name>.md` file under the umbrella so no information was lost.

**2. Design Mockups (sketch → claude-design)**
`sketch` (rapid HTML mockups for visual direction-finding) is a strict subset of `claude-design`'s design workflow. Added as a labeled subsection under "Sketch Workflow — Rapid HTML Mockups" with full process documentation. Archived the standalone `sketch` skill.

**3. Codebase Analysis (codebase-inspection → github-repo-management)**
`codebase-inspection` (pygount-based LOC/ language analysis) is naturally part of repository management. Added as section 11 "Codebase Inspection" to `github-repo-management`.

**4. Apple Ecosystem (5 built-in skills — pruned)**
`apple-notes`, `apple-reminders`, `findmy`, `imessage`, `macos-computer-use` are all macOS-only, marked as platform-unsupported on this Linux host. With `prune_builtins: true` in config, all 5 were archived to `.archive/apple/`.

### Skills Left Alone (with rationale)

- **GitHub skills** (github-auth, github-code-review, github-issues, github-pr-workflow, github-repo-management) — Already well-organized under `github/` category with cross-references. Each is a distinct workflow with substantial domain-specific content.
- **Agent coding** (claude-code, codex, opencode) — Distinct CLIs with different syntax, auth, sessions, and behavior. Already categorized under `autonomous-ai-agents/`. The shared orchestration patterns (pty, background, monitoring) are too small relative to the unique content.
- **Creative/design** — `architecture-diagram`, `excalidraw`, `p5js`, `manim-video`, `ascii-art`, `ascii-video`, `baoyu-infographic`, `design-md`, `popular-web-designs`, `humanizer`, `pretext`, `pixel-art`, `songwriting-and-ai-music` — All produce fundamentally different output artifacts (SVG HTML, Excalidraw JSON, p5.js browser code, Manim Python, ASCII art, infographic PNGs, design tokens, text rewrites). Not mergeable.
- **Research** (arxiv, llm-wiki, blogwatcher, polymarket, research-paper-writing) — Distinct tools for paper search, knowledge base management, RSS monitoring, prediction markets, and paper writing. Different domains.
- **Debugging tools** (python-debugpy, node-inspect-debugger) — Language-specific debuggers. Already cross-reference `systematic-debugging` (now in the umbrella).
- **Red-teaming** (godmode, obliteratus) — Prompt-level vs weight-level uncensoring. Both have extensive support files (references, templates, scripts). Merging would be a complex multi-file relocation operation without clear discoverability benefit.

## Structured summary (required)
```yaml
consolidations:
  - from: plan
    into: development-workflows
    reason: Planning is phase 1 of the software development lifecycle; belongs under a class-level methodology umbrella
  - from: spike
    into: development-workflows
    reason: Feasibility validation is phase 0 of the dev lifecycle, complements planning
  - from: test-driven-development
    into: development-workflows
    reason: TDD is the core implementation discipline; belongs with other development methodology skills
  - from: systematic-debugging
    into: development-workflows
    reason: Debugging methodology is a development lifecycle phase; cross-referenced all other dev-methodology skills
  - from: requesting-code-review
    into: development-workflows
    reason: Pre-commit verification is the quality gate at the end of each dev cycle
  - from: simplify-code
    into: development-workflows
    reason: Post-implementation cleanup is the final dev lifecycle phase; all 6 skills form a natural class
  - from: sketch
    into: claude-design
    reason: Rapid HTML mockups are a strict subset of claude-design's design workflow (disposable variants vs production artifacts)
  - from: codebase-inspection
    into: github-repo-management
    reason: Codebase LOC/language analysis is part of repository understanding, naturally adjacent to repo management
prunings:
  - name: apple-notes
    reason: macOS-only; platform-unsupported on Linux; prune_builtins enabled
  - name: apple-reminders
    reason: macOS-only; platform-unsupported on Linux; prune_builtins enabled
  - name: findmy
    reason: macOS-only; platform-unsupported on Linux; prune_builtins enabled
  - name: imessage
    reason: macOS-only; platform-unsupported on Linux; prune_builtins enabled
  - name: macos-computer-use
    reason: macOS-only; platform-unsupported on Linux; prune_builtins enabled
```
