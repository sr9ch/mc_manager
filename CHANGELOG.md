# Changelog

## Unreleased

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
