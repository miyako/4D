---
layout: default
title: Technical details
lead: How the emulator works, what it writes, and every input and output.
permalink: /technical/
---

The action is a composite GitHub Action around a single script,
[`fetch_dependencies.py`](https://github.com/miyako/4D/blob/v1/.github/actions/dependencies/fetch_dependencies.py).
It needs Python 3.10 or later and uses only the standard library. The action finds a suitable `python3`, `python` or
`py` on the runner and calls the script in `--action` mode, which reads the inputs from the environment and writes the
step outputs.

## Resolution

### 1. Declarations

1. The project package is located from `project`: a package folder, or a `.4DProject` file.
2. `Project/Sources/dependencies.json` gives the **primary** declarations.
3. The nearest `environment4d.json` is found, first in the package folder and then in each parent folder. For names
   that `dependencies.json` declares, it replaces their declaration. Its relative paths are kept relative to its own
   folder.
4. Names declared only in `environment4d.json` are dropped, with a warning.

### 2. The resolution queue

Resolution is breadth-first over a queue that starts with the primary declarations. For each item:

- **GitHub declaration:** it is classified as one of the following, then resolved, downloaded and installed.

  | Kind | Declared as |
  | --- | --- |
  | `tag` | `"tag"`, or an exact `version` |
  | `latest` | no `version`, or `"latest"` |
  | `anchor` | `"4d"`, or `"4D:N"` |
  | `range` | anything else |

  The component's own `dependencies.json` is fetched from its repository at the resolved tag, through the contents API,
  and its declarations join the queue as **secondary** declarations.
- **GitLab declaration:** resolved the same way, against the GitLab releases API of `host` (default gitlab.com).
  - The release asset is a link, followed to wherever the zip is hosted.
  - With no `version`, the kind is `highest`.
  - `latest` asks GitLab for its latest release.
  - Only runtimes that load GitLab components (4D 21 R3 and later) accept it.
- **Local declaration:** the path is resolved from the folder that contains the host package, or from the folder of the
  `environment4d.json` that supplied it. The component's own `dependencies.json` is read from disk, and its
  declarations join the queue. They also resolve from the host package's parent folder.
- **A name that is already resolved:**
  - a primary declaration always wins;
  - an identical secondary declaration is skipped;
  - a differing secondary declaration is recorded, and resolution restarts with all the declarations seen so far.

  Restarting means the final tag decides which children are resolved. Resolution settles after at most 64 passes;
  anything longer is reported as an error.

### 3. Settling differing secondary declarations

The declarations of one name are processed in the order they were met. The state starts as the first declaration, and
each later one is compared with the current state only:

1. **Conflict:** when the two can't meet, the state is kept and a conflict is recorded. Two declarations can't meet
   when:
   - a tag isn't allowed by the other declaration; or
   - two ranges or anchors have no release in common.

   `latest` never conflicts. `"4d"` allows its own major and older ones.
2. **Keep:** when either side uses the `tag` key, or the state is already a combined tag, the state is kept.
3. **Combine:** otherwise, the state becomes the highest release that satisfies both, with `latest` meaning the newest
   release. It is recorded as the lock `version`. When no release satisfies both, the state is kept.

After the last pass, every component that declared a conflicting name is marked as not loaded (errCode 21) and
reported incomplete. The dependency itself stays installed, with the conflict in its lock `errors`, which is exactly
what 4D writes.

### 4. Pins

A lock entry pins a tag when it names the same repository and the same lock `version` as the declaration, and, for a
range, the pinned tag still satisfies it. `update` ignores pins. Offline, a declaration that needs a release lookup and
has no pin fails.

## What is written

### Cache

```
<user cache>/<runtime>/Dependencies/.github/<owner>/<repo>/<tag>/      the unwrapped component
<user cache>/<runtime>/Dependencies/.github/<owner>/<repo>/<tag>.json  github, tag, htmlURL, archiveURL,
                                                                       archiveSize, dependencies
```

`<user cache>` is `~/Library/Caches` on macOS, `%LOCALAPPDATA%` on Windows and `$XDG_CACHE_HOME` or `~/.cache`
elsewhere, inside `home` when it is set. `dependencies-folder` overrides the whole path.

### Lock file

`userPreferences.<user>/dependencies-lock.json`, in 4D's format:

```json
{
  "version": 2102,
  "dependencies": {
    "hash": {
      "github": "miyako/hash",
      "version": "^20.0.0",
      "tag": "20.0.3",
      "htmlURL": "https://github.com/miyako/hash/releases/tag/20.0.3",
      "archiveURL": "https://github.com/miyako/hash/releases/download/20.0.3/hash.zip",
      "archiveSize": 123456,
      "path": "Macintosh HD:Users:…:Dependencies:.github:miyako:hash:20.0.3:",
      "dependencies": null,
      "found": true,
      "loaded": null
    }
  }
}
```

- **Top-level `version`:** the runtime version code, for example 2102 for 21.2 and 2150 for 21 R5.
- **`path`:** in the platform's native form, HFS on macOS.
- **Local components** are written as `{path, dependencies, loaded, found}`. A missing one has `path: ""` and
  `found: false`.
- **Known differences from 4D**, none of which affects what is loaded:
  - `loaded` is `null`;
  - GitHub entries also carry `archiveSize` and `dependencies`;
  - the dependencies nested inside an entry are written as declared, where 4D sometimes expands them.

### Rewritten manifest (`rewrite`)

The package copy's `dependencies.json` gets `{"github", "tag"}` for every resolved GitHub dependency, nested ones
included. Other keys in each declaration are kept. GitHub entries in an `environment4d.json` inside the copy are
rewritten the same way.

### Report

The JSON report, written to `report-file`, has:
- the runtime and its version, the platform, the package, the dependencies folder and home, the `env` variables, the
  archive store and the lock path;
- whether a token was used, and whether the run was offline;
- `resolved` (name → tag), `incomplete` (`{name, reason}`) and `warnings`;
- `dependencies`, with one entry per name: `source` (`download`, `archive`, `cache` or `local`), `reuse`, `tag`,
  `version`, `path`, `primary`, `pinned`, `overloadedBy`, `errors` and the release metadata.

## Integrity

| Mode | Behaviour |
| --- | --- |
| Archive store (default with `home`) | Each asset is stored with its SHA-256. On every run, the archive is hashed again and compared with the release's `sha256:` digest (online) or the recorded digest (offline), then extracted afresh. A mismatch is discarded and downloaded again. A deleted release or an outage falls back to the stored archive, with a warning. A rejected token never does. |
| Shared cache (`archive-store: none`) | An existing tag folder is reused as 4D reuses it: `unverified (4D-equivalent)`. A folder that is no longer a package is reported incomplete, not repaired, as in 4D. |

Downloads and extraction have these limits:
- An archive is at most 1 GiB.
- Extracted content is at most 2 GiB, in at most 50,000 entries.
- Entries with absolute paths, `..`, drive letters, backslashes or symbolic links are rejected.

Folders are installed by atomic rename. Concurrent runs that share a cache take a lock file for each tag; a lock older
than 30 minutes is treated as stale.

## Network and tokens

- **API calls** go to `api.github.com` with the `X-GitHub-Api-Version` header, and are retried on transient errors.
- **Downloads:**
  - with a token, through the API asset URL, as 4D does;
  - without one, through the browser download URL.

  The token is never sent to the redirect target.
- **Token sources:** the token comes from `github-token`, the `DEPENDENCIES_GITHUB_TOKEN` variable, or `--token-file`
  pointing at a 4D-style `github.json`. `GITHUB_TOKEN` and `GH_TOKEN` are ignored on purpose: a token that an
  organisation rejects would otherwise break public downloads.
- **A 401, or a 403 that isn't rate limiting,** fails without an anonymous retry.

## Runtime isolation

| OS | `env` output |
| --- | --- |
| macOS | `HOME`, `CFFIXED_USER_HOME` |
| Windows | `USERPROFILE`, `HOME`, `LOCALAPPDATA`, `APPDATA` (both AppData folders are created) |
| Linux | `HOME`, `XDG_CACHE_HOME` |

Real 4D on Windows ignores these overrides, so `home` is rejected there for `4D` and `4D Server`. On GitHub-hosted
runners, only `runtime: tool4d` is accepted.

## Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `project` | (required) | Package folder or `.4DProject` file |
| `runtime` | `tool4d` | `tool4d`, `4D` or `4D Server`: names the cache folder |
| `runtime-version` | | For example `21R5` or `21.2`. Anchors `"4d"` and the lock `version` |
| `runtime-path` | | Runtime executable, queried with `--version` when `runtime-version` is empty |
| `home` | | Isolated profile root; the cache goes inside it |
| `dependencies-folder` | | Explicit `Dependencies` folder |
| `archive-store` | on with `home` | Verified archive store path, or `none` |
| `github-token` | | Token for private repositories and rate limits |
| `update` | `false` | Ignore pinned tags |
| `offline` | `false` | No network |
| `write-lock` | `true` | Write the lock file |
| `rewrite` | | Package **copy** that receives an exact-tag `dependencies.json` |
| `fail-on-incomplete` | `true` | Fail the step when anything is incomplete |

## Outputs

`dependencies-folder`, `home`, `env`, `lock-file`, `resolved`, `incomplete`, `complete` and `report-file`, as described
in the [tutorial]({{ '/tutorial/' | relative_url }}#3-read-the-result).

## Exit codes (script)

| Code | Meaning |
| --- | --- |
| 0 | Every dependency installed |
| 3 | At least one dependency incomplete |
| 2 | Usage error, malformed manifest, or a runner-policy violation |

## Tests

```sh
cd .github/actions/dependencies
python -m unittest discover -s tests -v
```

The unit tests replay GitHub from memory, and include table tests that mirror the real-4D cases. The
`dependencies action` workflow runs them on Ubuntu, macOS and Windows. It then downloads tool4d on macOS and Windows,
installs the dependencies of a test project, and checks that tool4d loads every component, online and then offline.
