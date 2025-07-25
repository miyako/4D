# [Bump](https://github.com/miyako/4D/blob/v1/.github/workflows/bump.yml) workflow

Increment the version in `package.json` at the root of the current repository.

* runner: `ubuntu-latest`
* trigger: `workflow_call` only
* inputs: `mode`: patch, minor, major
* outputs: `version`: bumped version

## Note

The version information from `package.json` is used by the [`compiler`](https://github.com/miyako/4d-class-compiler) project when `localbuild` is specified.
   
# [Create Release](https://github.com/miyako/4D/blob/v1/.github/workflows/create-release.yml) workflow

Create a new release for the current repository.

* runner: `ubuntu-latest`
* trigger: `workflow_call` only
* inputs: `version`: release name, tag name
* outputs: `upload_url`: the url to upload assets 

# [Download tool4d](https://github.com/miyako/4D/blob/v1/.github/actions/tool4d-download/action.yml) action

Cross-platfrom workflow to download `tool4d` from 4D.com.

* runner: `macos` `windows` `self-hosted`
* trigger: `workflow_call` only
* inputs: see below
* outputs: `tool4d_download_url` `tool4d_executable_path`

## Note

You should checkout your repository before running this action.

e.g.

```yml
jobs:

  test:
    runs-on: [macos-latest]
    steps:
      - name: get
        uses: miyako/4D/.github/actions/get-tool@v1
        with:
          platform: macos
          branch: 20.x
          version: 20.2
          build: 100959
```

e.g. with a `strategy`

```yml
jobs:     

  get:
    strategy:
      fail-fast: false
      matrix:
        TOOL4D_PLATFORM: ["windows-latest", "macos-latest"]
        TOOL4D_BRANCH: [20.x]
        TOOL4D_VERSION: [20.2]
        TOOL4D_BUILD: [latest] 
    runs-on: ${{ matrix.TOOL4D_PLATFORM }}
    steps:
    
      - name: get tool4d
        id: get
        uses: miyako/4D/.github/actions/get-tool@v1
        with:
          platform: ${{ matrix.TOOL4D_PLATFORM }}
          branch: ${{ matrix.TOOL4D_BRANCH }}
          version: ${{ matrix.TOOL4D_VERSION }}
          build: ${{ matrix.TOOL4D_BUILD }}
```

e.g.

https://github.com/miyako/4d-topic-cicd/blob/main/.github/workflows/test.yml

## Remarks

`curl` on Windows seem to block on the default `pwsh`. Using `shell: bash` instead.

# [Build Application]() action

* runner: `[macos, self-hosted]`, `[windows, self-hosted]`
* trigger: workflow_call only
* outputs: `compiler_component_path`

Compile, build on self-hosted runner using the specified agent (4D, not `tool4d`), the [`compiler`](https://github.com/miyako/4d-class-compiler) project as a component, and the command `BUILD APPLICATION`.

You can call this action with a matrix.

e.g.

```yml
jobs:

  build_both:
    name: build self-hosted
    strategy:
      fail-fast: false
      matrix:
        platform: [macos, windows]
        include: 
          - platform: macos
            build_agent: ${{ inputs.build_agent_macos }}
          - platform: windows
            build_agent: ${{ inputs.build_agent_windows }}
    runs-on: [self-hosted, "${{ matrix.platform }}"]
    steps:
      - name: checkout 
        uses: actions/checkout@v4

      - name: build application
        uses: miyako/4D/.github/actions/build-application/@v1
        with:
          build_project_name: ${{ inputs.build_project_name }}
          build_agent: ${{ matrix.build_agent }}
          build_dir: ${{ inputs.build_dir }}
          build_project_path: ${{ inputs.build_project_path }}
          build_settings_path: ${{ inputs.build_settings_path }}
          platform: ${{ matrix.platform }}
```

## Note

The shell on Windows is `powershell`, not `pwsh` (which may not be installed).

# [Build macOS](https://github.com/miyako/4D/blob/v1/.github/actions/build-macos/action.yml) action

* runner: `[macos, self-hosted]`
* trigger: workflow_call only
* outputs: `asset_path` `asset_name` `upload_url`

Compile, build, archive, sign, notarise, staple, macOS product on self-hosted runner using the [`compiler`](https://github.com/miyako/4d-class-compiler) project.

The created asset should be uploaded to GitHub by the caller workflow with its own `secrets.GITHUB_TOKEN`.
