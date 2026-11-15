---
layout: default
title: Parity with 4D
lead: Every rule the emulator follows was observed in real 4D first. This page lists the evidence and what is still unobserved.
permalink: /parity/
---

## How the rules were established

Each behaviour was tested in real 4D, then the emulator was made to match:

1. **A small case project** declares a specific situation: a version range, two components disagreeing about a
   dependency, a local path, and so on. Copies of components are placed at every folder that a path could plausibly
   resolve from, so the result shows which one 4D chose.
2. **Real 4D runs the project headless,** with an isolated home folder, so its own cache is never touched. A startup
   method records which components loaded, and 4D writes its `dependencies-lock.json`.
3. **The emulator runs on the same project.** Its lock file and the set of components it installs are compared with
   4D's.
4. **The case becomes a unit test,** so the behaviour can't regress.

The runtimes involved are 4D 21 R5 and 21.2, 4D Server 21.2, and tool4d 21.0 on macOS; and 4D 21.0 and tool4d on
Windows. The case files and comparisons are recorded in miyako/4D pull requests
[#5](https://github.com/miyako/4D/pull/5), [#7](https://github.com/miyako/4D/pull/7),
[#8](https://github.com/miyako/4D/pull/8), [#9](https://github.com/miyako/4D/pull/9) and
[#10](https://github.com/miyako/4D/pull/10), in issue [#6](https://github.com/miyako/4D/issues/6), and, for Windows,
in [miyako/4d-static-analysis#4](https://github.com/miyako/4d-static-analysis/issues/4).

## Rules and their evidence

| Rule | Cases |
| --- | --- |
| Cache layout and release metadata | D1, D16, W1 |
| `<repo>.zip` asset; a single `.4dbase` wrapper removed | D1 |
| `tag` used literally; exact `version` resolves to that tag | D7, W7 |
| No `version` or `latest` → latest release; the lock records `latest` | D7, D9 |
| `"4d"` anchors on the running runtime | D7, D8, W7 |
| Ranges pick the highest match; prereleases and drafts ignored | D7 |
| Children read at the resolved tag, recursively; the lock is flat | D8, W7, W8 |
| A direct declaration wins over a child declaration | D14, W14 |
| Differing child declarations combine, keep the first, or conflict | C6a–C6c, L1–L5, F1–F7, T1–T4, V1, V2, E1, A1, A2 |
| …settled one at a time against the current state | G1–G3, M1–M5 |
| A conflict disables the declaring components, not the dependency | C6c, T3, T4, V1, V2, A1, M2–M4 |
| Local components' dependencies are resolved | C6ctl |
| Relative local paths: from the package's parent folder, or from the folder of `environment4d.json` | R1–R3, R6–R8 |
| Local entries in the lock file; missing ones are `path ""` | C6ctl, R3, R5, R8, R9 |
| An empty `{}` declaration is not looked up next to the package | D1 (local) |
| Dependency cycles load every component once | Y1–Y4 |
| GitLab: supported from 21 R3, not in 21.x; `highest` default; `latest` is the most recent release; the first listed link; the token in `gitlab.json`; subfolder manifests ignored; cache and lock format | GL1–GL8, H1–H7, K1t–K3t, M1t |
| Pins reused while the declaration is unchanged; `update` ignores them | D11, D12, W9, W11 |
| Offline: a range or `latest` without a pin fails | D10 |
| Old tags are never removed | D9 |
| A `Components/` copy takes precedence; its children are still resolved | D13, W13, C5 |
| `environment4d.json` overrides declared names only | 4D documentation, D17, C4a–C4d |
| A rejected token fails without an anonymous retry | D15, W15 |
| A damaged cached tag is not loaded and not repaired | D5, W5 |
| tool4d loads exact tags only and doesn't recurse | E-series |
| Runtime isolation variables per OS | E-series, W1, W6 |

## Case families

| Family | What it covers |
| --- | --- |
| D, W | Core behaviour on macOS (D) and Windows (W): versions, cache, lock, offline, tokens, `Components/` |
| E-series | tool4d's own loading behaviour (unrelated to case E1 below) |
| C4–C6 | `environment4d.json`, `Components/` precedence, differing child declarations, local components' children |
| R1–R9 | How local paths resolve, and how local components appear in the lock |
| L, F, T, V, E, A | Two components declaring one dependency with `latest`, `"4d"`, tags, ranges and exact versions |
| G, M | Ranges with no common release; three differing declarations |
| Y | Dependency cycles |
| GL, H | GitLab components, against [miyako-group/wavplay](https://gitlab.com/miyako-group/wavplay) on 4D 21.1, 21.2 and 21 R3–R5 |

## Not yet observed

These are handled conservatively, never guessed:

| Situation | What the emulator does |
| --- | --- |
| A GitLab token for a self-hosted `host` | Not sent |
| A GitLab component with a root `Project/Sources/dependencies.json`, and upcoming GitLab releases | Root manifest read, as for GitHub; upcoming releases skipped |
| A `path` declared inside a GitHub component | Leaves it unresolved, with a warning |
| A malformed `dependencies.json` or `environment4d.json` | Stops the run |
| A cycle that passes through GitHub releases | Resolves it like a local cycle (Y1–Y4); not tested with releases |

## Deliberate differences

| Area | Difference |
| --- | --- |
| Integrity | By default, archives are verified and extracted afresh on every run; 4D trusts its cache. `archive-store: none` behaves like 4D. |
| Lock file | `loaded` is `null`. GitHub entries also carry `archiveSize` and `dependencies`. Nested entries are written as declared. Pins and what loads are unaffected. |
| Local components | Resolved and reported, but not copied: placing them is left to the caller. |
| Tokens | `GITHUB_TOKEN` and `GH_TOKEN` are ignored, so a token an organisation rejects can't break public downloads. |
