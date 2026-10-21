---
id: TASK-001
title: Escape raw restic output before printing with rich console
status: To Do
assignee: []
created_date: '2026-10-21 03:23'
updated_date: '2026-10-21 03:23'
labels:
  - bug
dependencies: []
priority: medium
ordinal: 1000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
cmd_passthrough and other command handlers in src/macos_backup/cli.py stream raw restic stdout/stderr lines straight into console.print(). rich's console.print() interprets square brackets as markup tags, so restic output containing literal brackets (e.g. rewrite's snapshot summary lines like 'snapshot ee5d1ef0 of [/Users/lance] at ...') raises rich.errors.MarkupError. That exception is swallowed by the sh library's background output-reader thread, so the command appears to exit 0 and print the success message even though the actual restic output was never shown and could contain real errors. Reproduced via 'mb rewrite --exclude "$HOME/.comfyui" --dry-run', whose dry-run snapshot listing has bracketed paths and triggers the crash silently.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 console.print() calls that output raw restic or subprocess text (out_handler and err_handler call sites in cmd_passthrough, cmd_backup, cmd_snapshots, cmd_check, and any other cmd_* handler with the same pattern) no longer raise rich.errors.MarkupError on input containing square brackets
- [ ] #2 Raw restic output lines are displayed to the user verbatim and not silently dropped, regardless of bracket content
- [ ] #3 A regression test reproduces a restic output line containing unbalanced or unexpected square brackets (e.g. a bracketed path list) and asserts it is printed without raising or being silently dropped
<!-- AC:END -->
