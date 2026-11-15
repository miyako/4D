---
layout: default
title: Tutorial
lead: From a 4D project with dependencies to tool4d loading them on a GitHub-hosted runner, one step at a time.
permalink: /tutorial/
---

The workflow below follows the action's own CI test, which runs it on macOS and Windows runners and checks that tool4d
loads every component. Replace `<commit-sha>` with a commit of [miyako/4D](https://github.com/miyako/4D), and
`MyProject` with your package folder.

## 1. Check your project

The project declares its components in `MyProject/Project/Sources/dependencies.json`:

```json
{
  "dependencies": {
    "4D SVG": {"github": "4d/4D-SVG", "version": "4d"},
    "hash": {"github": "miyako/hash", "version": "^20.0.0"},
    "tcp": {"github": "miyako/tcp"}
  }
}
```

Each component must be published as a GitHub release with an asset named `<repo>.zip`, which is what 4D itself
expects. The action follows these components' own `dependencies.json` files, so you only declare what the project uses
directly.

## 2. Add the workflow

Create `.github/workflows/tool4d.yml`:

{% raw %}
```yaml
name: tool4d

on: [push, pull_request]

permissions:
  contents: read

jobs:
  test:
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v5

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

      - name: Copy the project
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

      - name: Run tool4d with the components
        shell: python
        env:
          DEPENDENCIES_ENV: ${{ steps.dependencies.outputs.env }}
          TOOL4D: ${{ steps.tool4d.outputs.tool4d_executable_path }}
        run: |
          import json, os, subprocess, sys
          from pathlib import Path
          env = dict(os.environ, **json.loads(os.environ["DEPENDENCIES_ENV"]))
          project = next(Path(os.environ["RUNNER_TEMP"], "MyProject", "Project").glob("*.4DProject"))
          command = [os.environ["TOOL4D"], f"--project={project}", "--startup-method=run_tests",
                     "--dataless", "--skip-onstartup"]
          sys.exit(subprocess.run(command, env=env).returncode)
```
{% endraw %}

What each step does:

| Step | Purpose |
| --- | --- |
| Download tool4d | Fetches tool4d. Its path is used both to run it and, through `runtime-path`, to read its version. The version anchors `"version": "4d"`. |
| Copy the project | `rewrite` changes `dependencies.json`, so it must work on a copy, never on the checkout. |
| Install 4D dependencies | Resolves and installs everything into `home`, writes the lock file, and rewrites the copy's `dependencies.json` with exact tags. |
| Run tool4d | Starts tool4d with the variables from the `env` output, so it reads the cache inside `home`. `run_tests` stands for your own startup method. End that method with `QUIT 4D`, as the action's test method does. |

## 3. Read the result

The step fails when a dependency can't be resolved or installed. Its log names each one with a reason:

```
dependency tcp incomplete: latest release not found in miyako/tcp (a token may be required for private repositories)
```

The outputs let later steps react:

| Output | Content |
| --- | --- |
| `complete` | `true` when every dependency was installed |
| `resolved` | JSON object, name → tag |
| `incomplete` | JSON list of `{name, reason}` |
| `env` | JSON object of variables that point the runtime at `home` |
| `lock-file` | Path of the `dependencies-lock.json` that was written |
| `report-file` | Path of the full JSON report |

To carry on despite missing components, for example to report them yourself, set `fail-on-incomplete: "false"` and
check `steps.dependencies.outputs.complete`.

## 4. Run on Windows too

The same job runs on `windows-latest`. Change the tool4d platform, and copy the project with a shell that exists there:

{% raw %}
```yaml
    strategy:
      matrix:
        include:
          - os: macos-latest
            platform: macos
          - os: windows-latest
            platform: windows
    runs-on: ${{ matrix.os }}
    steps:
      # ...
      - name: Download tool4d
        id: tool4d
        uses: miyako/4D/.github/actions/tool4d-download@<commit-sha>
        with:
          platform: ${{ matrix.platform }}
          branch: 21.x
          version: "21.0"
          build: latest
          arch: arm
      - name: Copy the project
        shell: bash
        run: cp -R MyProject "$RUNNER_TEMP/MyProject"
```
{% endraw %}

The `env` output already holds the right variables for each OS. On Windows, these are `USERPROFILE`, `HOME`,
`LOCALAPPDATA` and `APPDATA`, and the action creates both AppData folders, which tool4d requires.

## 5. Keep downloads between runs

GitHub-hosted runners start empty, so every run downloads every release again. Keep the archive store with
`actions/cache`, and point the action at it:

{% raw %}
```yaml
      - uses: actions/cache@v4
        with:
          path: ${{ runner.temp }}/archives
          key: 4d-dependencies-${{ runner.os }}-${{ hashFiles('MyProject/Project/Sources/dependencies.json') }}
          restore-keys: 4d-dependencies-${{ runner.os }}-

      - name: Install 4D dependencies
        id: dependencies
        uses: miyako/4D/.github/actions/dependencies@<commit-sha>
        with:
          project: ${{ runner.temp }}/MyProject
          runtime-path: ${{ steps.tool4d.outputs.tool4d_executable_path }}
          home: ${{ runner.temp }}/home
          archive-store: ${{ runner.temp }}/archives
          rewrite: ${{ runner.temp }}/MyProject
          github-token: ${{ github.token }}
```
{% endraw %}

A restored archive is hashed again and checked against the release digest before use, so the cache can save a
download but can never change what is installed.

## 6. Work offline

`offline: "true"` runs with no network at all, using only the archive store. As in 4D, an offline run needs a pin for
every range or `latest`, and that pin comes from the lock file that an online run writes into the package. The simplest
reliable pattern, used by the action's own CI, is a second run in the same job, against the same package and archive
store:

{% raw %}
```yaml
      - name: Re-run offline from the archive store
        uses: miyako/4D/.github/actions/dependencies@<commit-sha>
        with:
          project: ${{ runner.temp }}/MyProject
          runtime-path: ${{ steps.tool4d.outputs.tool4d_executable_path }}
          home: ${{ runner.temp }}/home-offline
          archive-store: ${{ runner.temp }}/archives
          offline: "true"
          write-lock: "false"
```
{% endraw %}

Across separate jobs or runs, an offline run also needs that package's `userPreferences.<user>/dependencies-lock.json`
from the online run. `<user>` is the runner's account name.

## 7. Private components

`github.token` can read public repositories, and the repository that runs the workflow. For components in other
private repositories, create a fine-grained token with read access to their contents, store it as a secret, and pass it
as `github-token`:

{% raw %}
```yaml
          github-token: ${{ secrets.COMPONENTS_TOKEN }}
```
{% endraw %}

The token is sent to `api.github.com` only, never written to reports, and never retried anonymously if GitHub
rejects it.

## 8. Update pinned versions

The lock file pins each resolved tag, and later runs reuse a pin while its declaration is unchanged, as 4D does. To
pick up newer releases, run once with `update: "true"`, or change the declaration.

## 9. Run it locally

The action is one Python script with no dependencies outside the standard library:

```sh
python fetch_dependencies.py --project MyProject --runtime tool4d --runtime-version 21.0 \
  --home /tmp/4d-home --output report.json
```

- **Exit code 0:** everything was installed.
- **Exit code 3:** at least one dependency is incomplete.
- **Exit code 2:** a usage error, or a malformed manifest.

`python fetch_dependencies.py --help` lists every option.

## Troubleshooting

| Message | Meaning |
| --- | --- |
| `… not found in owner/repo (a token may be required …)` | The repository or release doesn't exist, or it's private and the token can't read it. |
| `release … has no <repo>.zip asset` | The release isn't packaged the way 4D expects. |
| `HTTP 401` / `HTTP 403` | GitHub rejected the token, for example because of SAML enforcement. There is no anonymous retry. |
| `offline … no verified archive is stored` | The offline run needs an archive store from an earlier online run. |
| `not loaded by 4D: its dependency X is disabled by a version conflict` | Two components declare X in ways that can't be combined. 4D wouldn't load them either. |
| `GitLab dependencies are not supported` | Install that component another way, for example by copying it into `Components/`. |
| `runtime '4D' is only allowed on self-hosted runners` | Use `runtime: tool4d` on GitHub-hosted runners. |
