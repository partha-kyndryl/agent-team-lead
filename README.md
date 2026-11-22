# agent-team-lead

A Claude Code skill that makes Claude Code the team lead on a large change, not the only developer.

You give it a mission. It plans the work, splits it into tasks that don't step on each other, keeps the hardest one or two for itself, and hands the rest to other coding agents running cheaper models: Codex, Antigravity (`agy`) and headless Claude sessions. Every worker gets its own branch and raises a PR. The lead reviews every PR, sends back what blocks a merge, and files GitHub issues for the rest.

## Why PRs

Agents can message each other directly. This skill doesn't do that, on purpose.

PRs leave a trail. Every change carries a Definition of Done, the tests that prove it, and a review. Nobody touches `main`. Your team can read it, and an enterprise can audit it.

## What it does

- Writes a plain tracker for you at `tmp/<mission>_tasks.md`: every task, owner, model, branch, PR and status.
- Makes any shared contract (schema, data model, API shape) task 0, owned by the lead and merged first.
- Gives each worker its own git worktree and a brief that only covers its task.
- Runs every worker on the cheapest model that can do the job, never the default. Moves a task up one tier only if its PR fails review twice.
- Reviews each PR against its Definition of Done, runs the tests, and checks for scope creep and bloat.
- Does not merge unless you tell it to. By default it reviews and lists PRs in merge order for you. For less critical work, let it merge and go play golf.

## Requirements

- [Claude Code](https://claude.com/product/claude-code)
- `git` and the GitHub CLI (`gh`), signed in
- At least one worker CLI: `codex`, `agy` or `claude`. It uses whichever are installed.
- A `tmp/` folder that is git-ignored. The skill checks and adds it if missing.

## Install

Inside Claude Code:

```
/plugin marketplace add partha-kyndryl/agent-team-lead
/plugin install agent-team-lead@agent-team-lead
```

Or copy it by hand:

```bash
mkdir -p ~/.claude/skills
cp -r skills/agent-team-lead ~/.claude/skills/
```

## Use

```
/agent-team-lead Refactor the app to match the reference in tmp/new-design. Delete tmp/new-design only after I confirm production.
```

Add "you may merge" to the mission if you want it to merge PRs that pass its review, with no human in the loop. Branch protection rules on your repo still apply.

## License

MIT
