# Published agent skills

`jevscan-calibration/` is the portable source of the calibration skill.
It uses the [Agent Skills format](https://agentskills.io/specification):
`SKILL.md` contains metadata and the workflow, and bundled references contain
details loaded when needed.

This directory publishes the skill; it does not activate it in this
repository automatically.

The skill is distributed in the repository and the source archive (sdist),
under `skills/jevscan-calibration/`. It is not installed into an agent's
configuration by the Python wheel. Extract the matching source archive or
use the matching repository release, then copy the directory as described
below. The command-line tools themselves can be installed from the wheel.

Copy the complete `jevscan-calibration` directory into your agent's skill
discovery location:

- Codex project: `.agents/skills/jevscan-calibration/`
- Codex personal: `~/.agents/skills/jevscan-calibration/`
- Claude Code project: `.claude/skills/jevscan-calibration/`
- Claude Code personal: `~/.claude/skills/jevscan-calibration/`

Other Agent Skills clients may use different discovery locations. Follow
their installation instructions. Install a jevscan version that exposes
`--calibration-output`, `jevscan-calibration-import`, and
`jevscan-calibrate --select --apply` before using this workflow.

The skill calls the installed production tools; it does not contain a second
implementation of calibration. Capture artifacts can contain complete source
files and should not be published without authorization.
