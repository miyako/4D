---
layout: default
title: 4D dependencies action
lead: Resolve and install the components that a 4D project declares, in CI, the way 4D does, so tool4d can load them on GitHub-hosted runners.
---

## Why it exists

A 4D project declares its components in `Project/Sources/dependencies.json`. In 4D and 4D Server, the dependency
manager reads that file, downloads each component's GitHub release, follows the components' own dependencies, and
loads everything.

In CI, that doesn't work:

- **4D and 4D Server can't run on GitHub-hosted runners**, because of their licensing.
- **tool4d can run there, but it has no dependency manager.** It loads components that are already in its cache, and
  only for declarations with an exact `tag`.

Without the components, the project is incomplete in CI. Code that calls a component fails to compile, and tests or
analysis that depend on it can't run.

This action fills that gap. It **emulates the 4D dependency manager**: it resolves the same versions 4D would, installs
them in the same cache layout, and writes the same lock file. tool4d then finds and loads them as if 4D had installed
them.

## What it gives you

<div class="cards">
  <div class="card"><strong>Same versions as 4D</strong><p>Tags, exact versions, ranges, <code>latest</code> and <code>"4d"</code> anchors, nested dependencies, conflicts and cycles are resolved as real 4D resolves them.</p></div>
  <div class="card"><strong>Same cache layout</strong><p>Components land in <code>Dependencies/.github/&lt;owner&gt;/&lt;repo&gt;/&lt;tag&gt;/</code> under tool4d's, 4D's or 4D Server's user cache.</p></div>
  <div class="card"><strong>Same lock file</strong><p><code>dependencies-lock.json</code> is written in 4D's format, and its pins are honoured on later runs.</p></div>
  <div class="card"><strong>Ready for tool4d</strong><p>An exact-tag copy of <code>dependencies.json</code>, plus the environment variables that point tool4d at an isolated cache.</p></div>
  <div class="card"><strong>Verified downloads</strong><p>Release assets are checked against GitHub's SHA-256 digest, extracted safely, and kept in an archive store for offline runs.</p></div>
  <div class="card"><strong>Fails clearly</strong><p>Anything that can't be resolved is reported with a reason, and the step fails by default.</p></div>
</div>

## Quick start

{% raw %}
```yaml
- uses: actions/setup-python@v6
  with:
    python-version: "3.12"
- name: Download tool4d
  id: tool4d
  uses: miyako/4D/.github/actions/tool4d-download@<commit-sha>
  with:
    platform: macos
    branch: 21.x
    version: "21.0"
    build: latest
    arch: arm
- name: Copy the project, so the checkout is never modified
  run: cp -R MyProject "$RUNNER_TEMP/MyProject"
- name: Install 4D dependencies
  id: dependencies
  uses: miyako/4D/.github/actions/dependencies@<commit-sha>
  with:
    project: ${{ runner.temp }}/MyProject
    runtime-path: ${{ steps.tool4d.outputs.tool4d_executable_path }}
    home: ${{ runner.temp }}/home
    rewrite: ${{ runner.temp }}/MyProject
    github-token: ${{ github.token }}
```
{% endraw %}

Then run tool4d with the variables from the `env` output, as shown in the [tutorial]({{ '/tutorial/' | relative_url }}).
Pin the action to a commit SHA rather than a branch, so that a later change can't alter your builds.

## Where to go next

- [**Features**]({{ '/features/' | relative_url }}): everything the action does, and what it doesn't support.
- [**Tutorial**]({{ '/tutorial/' | relative_url }}): a complete workflow, step by step, from a project with dependencies to tool4d loading them.
- [**Technical details**]({{ '/technical/' | relative_url }}): the resolution algorithm, the cache and lock formats, integrity, inputs and outputs.
- [**Parity with 4D**]({{ '/parity/' | relative_url }}): how each rule was established against real 4D, and what is still unobserved.

## Status

Every rule described on this site was checked against real 4D, case by case, and the emulator matches 4D in every
case tested. Most cases ran on 4D 21.2 and 21 R5 on macOS; earlier ones also covered 4D 21.0 and tool4d on Windows.
[Parity with 4D]({{ '/parity/' | relative_url }}) lists the cases.

**Not supported:** GitLab-hosted components. **Not yet observed in 4D:** a local `path` declared inside a GitHub
component. Both are reported as incomplete or with a warning, never guessed.

Requirements: Python 3.10 or later on the runner (only the standard library is used), and components published as
GitHub releases with a `<repo>.zip` asset, as 4D itself requires.
