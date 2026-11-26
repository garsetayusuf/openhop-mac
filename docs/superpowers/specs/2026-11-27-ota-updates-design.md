# OTA Updates for OpenHop Mac — Design

Date: 2026-11-27

## Goal

The version chip in Settings → About becomes a working update check. When a newer
release exists, the user can auto-install it: the app downloads the DMG, swaps the
installed app and relaunches. Publishing a release is one script.

## Decisions

- **Source of truth:** GitHub Releases API
  `https://api.github.com/repos/garsetayusuf/openhop-mac/releases/latest`.
  `tag_name` is the version; the asset named `OpenHop.dmg` supplies the download URL.
  No `update.json` / appcast to maintain.
- **Engine:** lightweight, in-repo Swift code. No Sparkle dependency (the project
  builds with plain `swiftc`; adding Sparkle would need SwiftPM + framework linking).
- **Check timing:** at launch and every 24 hours while the app runs, plus manual
  button. No cron / launchd job; no checks while the app is fully quit.
- **Install:** automatic swap of `/Applications/OpenHop.app` then relaunch.
- **Versioning:** keep the visible `3.8.x` line. `Info.plist`
  `CFBundleShortVersionString` becomes the single source of truth, set to `3.8.1`
  for this build; next tag is `v3.8.2`.
- **Out of scope:** EdDSA signature verification (app is ad-hoc signed), delta
  updates, Sparkle, background checking while quit.

## Architecture

New `macos/UpdateManager.swift`:
- `struct Release: Decodable` — `tag_name`, `assets[].name`, `browser_download_url`,
  `body` (release notes).
- `func check()` — `URLSession` GET, decode, compare normalized version strings
  (`v3.8.2` vs `3.8.1`) numerically, publish `latestVersion`, `dmgURL`, `notes`,
  `state` (`.idle`, `.checking`, `.upToDate`, `.available`, `.downloading`, `.failed`).
- `func download()` — `URLSession.downloadTask` to
  `~/Library/Caches/openhop/updates/OpenHop.dmg`; reports `progress`.
- `func install()` — mount the DMG with `hdiutil`, copy `OpenHop.app` to a staging
  directory, atomically replace `/Applications/OpenHop.app`, launch the new app
  with `open`, then terminate the old process. On any failure, leave the existing
  app untouched and publish an error.
- `func cancel()` — cancel the download and delete the partial file.
- `static let appVersion` — read `CFBundleShortVersionString` from `Bundle.main`.

`DashboardModel` owns an `UpdateManager` and mirrors its published state so
SwiftUI views can bind to it.

## UI

- Settings → About: the `v3.8.1` chip becomes a button. Labels:
  `Check for updates` → `Checking…` → `Up to date` / `vX.Y.Z available` →
  `Update to vX.Y.Z`.
- While downloading: progress row with a Cancel button.
- When the download finishes: an alert/sheet with **Install & Restart** and
  **Cancel**. Cancel discards the DMG.
- Badge on the chip + `UNUserNotificationCenter` notification when a background
  check finds an update (mirror Sparkle's "gentle reminder" pattern for menu-bar
  apps); clicking the notification opens Settings → About.
- Menu bar: add `Check for Updates…` alongside `Open OpenHop` in the full menu
  (after Continue only).
- All errors shown inline in the About card; never a modal that can be missed.

## Publishing

`release.sh` at repo root:
1. Take version argument (`3.8.2`).
2. Bump `CFBundleShortVersionString` and `CFBundleVersion` in `macos/Info.plist`.
3. Run `bash build-dmg.sh`.
4. `git add`, `git commit -m "Release vX.Y.Z"`, `git tag vX.Y.Z`.
5. `git push && git push --tags`.
6. `gh release create vX.Y.Z dist/OpenHop.dmg --title "vX.Y.Z" --notes "..."`.

User only runs `bash release.sh 3.8.2` after writing notes. The app then sees the
new release on its next daily check or manual button press.

## Verification

- Build must succeed with `bash build-app.sh`.
- Manual: launch app with current version, press version button, confirm
  "Up to date" (or point `owner/repo` at a test repo with a release to exercise
  the "available" path).
- Failure cases: no network, no release, missing DMG asset, cancelled download —
  each must leave the app running and show a readable message.
- Publishing dry run: `gh release create` with `--draft` to confirm the asset URL
  matches what the app expects.
