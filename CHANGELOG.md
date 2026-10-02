# Changelog

## Unreleased

## 1.0.1 — 2026-10-02

### Fixed

- Decode Java properties escapes in Legacy Launcher game paths on Windows.
- Recognize Minecraft's year-based versions, including `26.3`.
- Prefer Legacy Launcher's selected `login.version` and inspect isolated game folders' version JSON.
- Keep CLI output working with older Windows terminal encodings and normalize saved scan paths.

## 1.0.0 — 2026-10-02

### Added

- One-time approval per newly found Minecraft build and version, with bulk approve,
  bulk skip, and individual choices stored in the local configuration.
- `sync --all-new` and `sync --skip-new` for noninteractive decisions.
- Terminal activity and transfer progress indicators for scans, Git transfer, and sync.

### Changed

- Previously approved builds synchronize without repeat questions. Git commit and
  optional push follow configuration without extra confirmation prompts.
- Repository setup asks only for the Git location and clone destination.

## 0.3.0 — 2026-10-02

### Added

- Windows and macOS configuration, data, Minecraft, and launcher search paths.
- Legacy Launcher settings detection and separate game folders under `home/`.
- Cross-platform repository lock and recoverable multi-instance synchronization.
- Separate Linux, Windows, and macOS setup instructions.

### Fixed

- Keep scan history on unreadable content or a corrupt state file.
- Save destination mappings before repository writes and stop on source scan errors.

## 0.2.3 — 2026-10-02

### Fixed

- Keep synchronization settings and destination mappings when running setup again.
- Finish `config --setup` after saving, without starting a synchronization.
- Stop safely on unreadable or symlinked source content and malformed sync manifests.
- Report invalid configuration values with a clear error.

### Improved

- Group launcher, installation, scan-change, and repository results on startup.
- Show extra discovery details with `--verbose`.

## 0.2.2 — 2026-10-02

### Improved

- Summarize large sync plans and show their full file list with `--verbose`.
- Keep package contents explicit and include contributor and release notes in
  source distributions.
- Use a deque for bounded filesystem discovery.

## 0.2.1 — 2026-10-02

### Fixed

- Exclude the configured Git repository from generic Minecraft discovery after the
  first synchronization, so subsequent scans do not treat backup files as game installs.

## 0.2.0 — 2026-10-02

### Added

- Launch with `mc_manager` to rescan local installations and compare launchers,
  instances, Minecraft/loader versions, and managed content with the previous scan.
- Allow scanning without configuring a Git repository; repository setup is explicit.
- Preserve existing discovery history when upgrading the scan state format.

## 0.1.1 — 2026-10-02

### Fixed

- Validate all planned source and destination files before copying; stage changed files
  so a late source edit does not leave an untracked partial sync.
- Reject newly appeared unmanaged files and Git URLs with embedded credentials.
- Check Git author identity before a synchronization that will create a commit.
- Keep non-Latin instance and world names readable in repository paths.

## 0.1.0 — 2026-10-01

### Added

- Launcher adapters, bounded generic discovery, and physical game-directory deduplication.
- Selective content synchronization, manifests, Git workflow, and terminal setup.
- CLI diagnostics, filesystem safety checks, and fixture-based tests.
