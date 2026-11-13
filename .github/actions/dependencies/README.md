# 4D dependencies action

Emulates the 4D dependency manager. It resolves the GitHub dependencies of a 4D project, downloads them, and installs them
in the cache layout that 4D, 4D Server and tool4d use. Any runner can use it, GitHub-hosted or self-hosted.

Why it exists: tool4d has no dependency manager. It loads components that are already in its cache, and only for exact
`tag` declarations. 4D and 4D Server do fetch, but GitHub-hosted runners cannot run them.

```yaml
- uses: miyako/4D/.github/actions/dependencies@<commit-sha>
  id: dependencies
  with:
    project: ${{ runner.temp }}/MyProject     # a copy, when `rewrite` is used
    runtime-path: ${{ steps.tool4d.outputs.tool4d_executable_path }}
    home: ${{ runner.temp }}/home              # isolated cache
    rewrite: ${{ runner.temp }}/MyProject      # exact-tag dependencies.json for tool4d
    github-token: ${{ secrets.COMPONENTS_TOKEN }}  # optional
```

Then run tool4d with the variables in the `env` output (a JSON object), e.g. `HOME` and `CFFIXED_USER_HOME` on macOS, or
`USERPROFILE`, `HOME`, `LOCALAPPDATA` and `APPDATA` on Windows. The action needs Python 3.10 or later; it uses only the standard library.
The script, `fetch_dependencies.py`, also runs on its own: `python fetch_dependencies.py --help`.

## Inputs and outputs

See [`action.yml`](action.yml). The outputs are `dependencies-folder`, `home`, `env`, `lock-file`, `resolved` (name → tag),
`incomplete` (list of `{name, reason}`), `complete`, and `report-file` (the full JSON report).

By default the step fails when a dependency cannot be resolved or installed (`fail-on-incomplete`).

## Behaviour

Each rule below follows what real 4D does. The evidence IDs refer to experiments run against 4D 21R5, 4D 21.2, 4D Server
21.2 and tool4d 21.0 on macOS, and 4D 21.0 and tool4d on Windows.

| Rule | Evidence |
| --- | --- |
| The cache is `<user cache>/<runtime>/Dependencies/.github/<owner>/<repo>/<tag>/`, holding the unwrapped release asset, plus `<tag>.json` with `github`, `tag`, `htmlURL`, `archiveURL`, `archiveSize` and `dependencies`. `<runtime>` is `4D`, `4D Server` or `tool4d`. | D1, D16 |
| The release asset must be `<repo>.zip`. A single `.4dbase` wrapper is removed. | D1 |
| `tag` is used literally. An exact `version` resolves to that tag. | D7 |
| No `version`, or `latest`, resolves to the latest release. The lock file records `latest`. | D7, D9 |
| `"4d"` anchors on the **running** runtime version, not `compatibilityVersion`: 21R5 → `4D:21R5` → 21R5.1; 21.2 → `4D:21` → 21.5. | D7, D8 |
| Ranges (`^`, `~`, `>=`, `x`, `-`, `\|\|`) pick the highest matching release. Prereleases and drafts are ignored. | D7 |
| Child dependencies come from the component's `Project/Sources/dependencies.json` at the resolved tag, recursively. The lock file is flat. | D8 |
| A direct declaration wins over a child declaration of the same name. | D14 |
| A tag pinned in `dependencies-lock.json` is reused while the declaration is unchanged. `update` ignores pins. | D11, D12 |
| Offline, a range or `latest` without a pin fails. There is no fallback to an older cached tag. | D10 |
| Old tags are never removed from the cache. | D9 |
| A copy in `Components/` with the same name takes precedence. The dependency is still installed and reported with `overloadedBy` and errCode 19, but it is not incomplete. | D13 |
| `environment4d.json` in the package folder or a parent folder overrides `dependencies.json`. | 4D docs |
| A rejected token (401, or 403 such as SAML enforcement) fails without an anonymous retry. | D15 |
| tool4d loads exact `tag` entries only and does not recurse, hence `rewrite`. | E-series |
| tool4d honours `HOME`/`CFFIXED_USER_HOME` (macOS) and `USERPROFILE`, `LOCALAPPDATA` and `APPDATA` together (Windows; both AppData folders must exist or tool4d quits without loading). Real 4D on Windows ignores these overrides. | E-series, W1, W6, debug run 43464410687 |

GitLab dependencies are reported as incomplete. Local-path dependencies are reported with `source: local` and left to the
caller.

## Integrity

Real 4D trusts a cached tag folder as long as it exists: it neither verifies nor repairs it (D2, D5). This action has
two modes.

- **Archive store** (the default when `home` is set). Each downloaded asset is kept in the store with its SHA-256. On
  every run the stored archive is hashed again and compared with the release digest from GitHub (or with the stored digest
  offline), then **extracted afresh** into the cache. A mismatched archive is discarded and downloaded again. Cached folders
  are therefore never trusted, only re-created from verified archives.
- **Shared cache** (`archive-store: none`). An existing tag folder is reused as 4D would reuse it. The report labels this
  reuse `unverified (4D-equivalent)`.

Downloads are checked against the release asset's size and its `sha256:` digest. Extraction rejects absolute paths,
`..`, drive letters, backslashes and symbolic links. Folders are installed atomically, and concurrent runs that share a
cache are serialised with a lock file.

## Tokens

`github-token` is optional. It is needed for private repositories and helps with rate limits.

- It is sent to `api.github.com` only. Asset downloads follow redirects without it.
- When it is set, assets are downloaded through the API asset URL, as 4D does when it has a token.
- The script reads the token from `DEPENDENCIES_GITHUB_TOKEN`, or from a 4D-style `github.json` (`--token-file`). It
  deliberately ignores `GITHUB_TOKEN` and `GH_TOKEN`, because a token that is rejected by an organisation (for example SAML)
  would otherwise break public downloads.

## Runners

- **GitHub-hosted runners must use tool4d.** Any other `runtime` fails there.
- Self-hosted runners may use 4D or 4D Server. On Windows, real 4D ignores `LOCALAPPDATA`/`APPDATA`, so `home` is
  rejected for those runtimes. Isolate them with a dedicated user account instead.

## Tests

```sh
cd .github/actions/dependencies
python -m unittest discover -s tests -v
```

The `dependencies action` workflow also downloads tool4d on macOS and Windows, installs the dependencies of
[`tests/fixtures/Probe`](tests/fixtures/Probe), and checks that tool4d loads every component.
