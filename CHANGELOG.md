# Changelog

## Unreleased

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
