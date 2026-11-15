---
layout: default
title: Features
lead: What the action does, grouped by area, and what it deliberately leaves out.
permalink: /features/
---

Each feature follows what real 4D does. The case IDs in brackets refer to the experiments listed on
[Parity with 4D]({{ '/parity/' | relative_url }}).

## Declarations

- **`dependencies.json`** in `Project/Sources/` declares the project's components. Each declaration is a GitHub
  component (`"github": "owner/repo"`) or a local one (`"path": "…"`).
- **`environment4d.json`**, in the package folder or the nearest parent folder, overrides the names that
  `dependencies.json` declares. In it, a string value is a path. A name declared *only* in `environment4d.json` is
  neither fetched nor loaded by 4D, so the action ignores it, with a warning [C4].
- **Malformed manifests** are never guessed:
  - A `dependencies.json` or `environment4d.json` that can't be parsed, or whose top level or `dependencies` isn't an
    object, stops the run.
  - A declaration that isn't an object is reported incomplete.

## Versions

| Declaration | Resolves to | Lock `version` |
| --- | --- | --- |
| `"tag": "21.3"` | that tag, used literally | none |
| `"version": "21.3"` (exact) | that tag | `21.3` |
| `"version": "^1.2.0"`, `~`, `>=`, `x`, `-`, `\|\|` | the highest matching release | the range as declared |
| no `version`, or `"latest"` | the latest release | `latest` |
| `"version": "4d"` | the newest release for the **running** runtime: 21.2 → `4D:21` → newest 21.x; 21 R5 → newest 21.x or 21 Rk with k ≤ 5 | `4D:21`, `4D:21R5` |

- Prereleases and drafts are ignored.
- Release tags such as `21R5` never match a range.
- `"4d"` falls back to the newest older major when the anchor's own major has no release.

## Nested dependencies

- **Components' own dependencies:** a component's `Project/Sources/dependencies.json` is read from its repository at
  the resolved tag, and what it declares is resolved recursively. The lock file is flat [D8].
- **Direct declarations win:** a name that the project declares directly wins over the same name declared by a
  component [D14].
- **Differing declarations by components** are settled one at a time, in declaration order, each against the current
  result [C6, L, F, T, V, E, A, G, M]:
  - Two `version` declarations combine into the highest release that satisfies both. The lock records that tag.
  - When a `tag` is involved, the first declaration is kept.
  - When they can't be combined, the dependency is still installed, but the components that declared it are not
    loaded, as in 4D. They are reported incomplete.
- **Cycles:** dependency cycles (X → Y → X, X → X, longer ones) are resolved without error. Each component is loaded
  once [Y1–Y4].

## Local components

- **Components declared by `path`** are resolved like 4D resolves them [R1–R9]:
  - A relative path in `dependencies.json` resolves from the folder that **contains the project package**, also when
    another local component declares it.
  - A relative path in `environment4d.json` resolves from that file's folder.
  - An empty declaration such as `"MyComp": {}` is not looked up next to the package [D1].
- **What local components declare is resolved too:** their own GitHub dependencies are installed [C6ctl].
- **The local folders themselves are not copied.** They're reported with their resolved `path`, and placing them is
  left to you.
- **Missing local components** are a warning, written to the lock as `path: ""`, `found: false`, as in 4D.

## GitLab components

4D 21 R3 and later load components hosted on GitLab; 4D 21.1 and 21.2 don't [GL1]. The action follows that: with a 21.x
runtime, including tool4d 21.0, a GitLab declaration is reported incomplete.

```json
"wavplay": {"gitlab": "miyako-group/wavplay", "version": "^20.0.0"}
```

- **`host`** declares a self-hosted GitLab, for example `"host": "https://gitlab.example.com"`. The default is
  gitlab.com [GL8].
- **No `version` means `highest`**, the highest semantic version. **`latest`** is GitLab's latest release, the most
  recently released one, which can be a lower version [H1–H3]. Ranges, exact versions, `tag` and `"4d"` work as for
  GitHub [H4–H7].
- **Assets are links.** A GitLab release links to the component's zip, which can be hosted anywhere. Links are used
  whatever their name. With several, the first one GitLab lists, the most recently added, is used, even when another is
  named `<project>.zip` [GL1, K1t, M1t].
- **Cache:** `.gitlab/<group>/<project>/<tag>/`. When `host` is declared, the host name is added:
  `.gitlab/<host>/<group>/<project>/<tag>/` [GL8].
- **Tokens:** 4D reads a GitLab token from `gitlab.json` in its active folder [K1t]. Pass one with the `gitlab-token`
  input, or `DEPENDENCIES_GITLAB_TOKEN`. It's needed for private projects and avoids gitlab.com's per-IP rate limit
  (HTTP 429). It is sent to gitlab.com only.
- **A component's own dependencies aren't followed.** Unlike GitHub components, 4D installs none of the dependencies a
  GitLab component declares, whether its `dependencies.json` is at the repository root or in a subfolder [M1t, M2t].
  The action does the same. Declare those dependencies in your own project instead.

## Cache, lock and runtimes

- **Cache layout:** components are installed in 4D's layout:
  `<user cache>/<runtime>/Dependencies/.github/<owner>/<repo>/<tag>/`, plus `<tag>.json` with the release metadata.
  `<runtime>` is `tool4d`, `4D` or `4D Server`.
- **Release archives:** the release asset must be `<repo>.zip`. A single `.4dbase` wrapper is removed.
- **Lock file:** `userPreferences.<user>/dependencies-lock.json` is written in 4D's format.
  - A pinned tag is reused while its declaration is unchanged.
  - `update: true` ignores pins.
- **`Components/` takes precedence:** a copy in the project's `Components/` with the same name wins. The dependency is
  still installed and reported with `overloadedBy`, and its own dependencies are still resolved [D13, C5].
- **Old tags stay:** cached tags are never removed.
- **Isolated home:** `home` gives the runtime a private profile, so CI never touches a real user cache. The `env` output
  holds the variables that point the runtime at it:
  - macOS: `HOME` and `CFFIXED_USER_HOME`;
  - Windows: `USERPROFILE`, `HOME`, `LOCALAPPDATA` and `APPDATA`.
- **tool4d support:** tool4d loads exact-tag declarations only and doesn't follow nested dependencies. `rewrite` writes
  a flat, exact-tag `dependencies.json` into a **copy** of the package, so tool4d loads exactly what was resolved.
- **Runner policy:** GitHub-hosted runners must use tool4d. 4D and 4D Server are accepted on self-hosted runners.

## Integrity and offline use

- **Verified downloads:** each download is checked against the release asset's size and its `sha256:` digest.
- **Archive store:** on by default when `home` is set. Each archive is kept with its SHA-256, hashed again on every
  run, and extracted afresh, so a cached folder is never trusted.
- **Shared-cache mode:** `archive-store: none` reuses cached folders as 4D does, labelled
  `unverified (4D-equivalent)`.
- **Offline runs:** `offline: true` uses only pinned or stored tags. A range or `latest` without a pin fails, as in 4D
  [D10].
- **Safe extraction:** it rejects absolute paths, `..`, drive letters, backslashes and symbolic links, and enforces
  size limits.
- **Concurrency:** installs are atomic, and concurrent runs that share a cache are serialised with a lock file.

## Tokens

- **`github-token` is optional.** It's needed for private repositories and helps with rate limits.
- **It is sent to `api.github.com` only.** Asset downloads follow redirects without it.
- **A rejected token** (401, or 403 such as SAML enforcement) fails, without an anonymous retry, as in 4D [D15, W15].
- **`GITHUB_TOKEN` and `GH_TOKEN` are deliberately ignored.** The script reads only `DEPENDENCIES_GITHUB_TOKEN`, or a
  4D-style `github.json`.

## Not supported

| Item | Behaviour |
| --- | --- |
| A GitLab token for a self-hosted `host` | Not sent: 4D's `gitlab.json` holds one token without a host |
| GitLab components with a 4D 21.x runtime (including tool4d 21.0) | Reported incomplete, as 4D 21.1 and 21.2 don't load them |
| A `path` declared inside a GitHub component | Not resolved, with a warning; not yet observed in 4D |
| `home` with 4D or 4D Server on Windows | Rejected: those runtimes ignore the profile overrides |
| Copying local components | Left to the caller |
| `loaded` in the lock file | Written as `null`: the emulator can't know what the runtime loads |
