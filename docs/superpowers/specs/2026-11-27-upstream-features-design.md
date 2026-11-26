# Upstream Feature Parity — Design

Date: 2026-11-27

## Goal

Bring four user-visible features from the upstream openhop line into the native
Mac control center: clean-endpoint latency testing, "Send continue again",
support-bundle export, a working multi-proxy pool, and a Tor route with fallback.

## Decisions

- Reuse the Python modules already vendored in `src/`; do not copy upstream UI.
- All work stays local. The support bundle is exported to Downloads, never uploaded.
- No new Swift dependencies; keep the plain `swiftc` build.
- Tor is opt-in and only available when a Tor SOCKS service is reachable — the app
  never installs Homebrew packages by itself.
- Out of scope: WarpGate relay, Whole-PC tunnel, Desktop asar patches, leaderboard,
  Sparkle/OTA (separate spec).

## Feature 1 — Clean endpoint test

- `DashboardModel.testCleanEndpoints()` runs, via the venv Python, a small `-c`
  script importing `clean_endpoint.test_endpoint_latency` for every entry in
  `KNOWN_CLEAN_ENDPOINTS`, printing JSON `{endpoint: milliseconds|null}`.
- Publish `@Published var endpointLatencies: [String: Double]` and
  `@Published var endpointTesting = false`.
- Settings → Connection route: **Test endpoints** button. While testing show a
  spinner; then the endpoint picker displays `endpoint — N ms` (untestable entries
  marked `—`). Selecting an entry calls the existing `applyCleanEndpoint(_:)`.
- Errors and "all unreachable" show inline in the card; no modal.

## Feature 2 — Send continue again

- `DashboardModel.resendContinuation()` runs
  `auto_resume.resend_failed()` via the venv Python and announces the returned
  `(sent, note)` as success/error.
- Add a **Send continue again** button in Diagnostics, and a matching action on the
  Activity event when its detail reports that the continue did not reach the chat.
- Disable while `isBusy`. Never auto-repeat without a user press.

## Feature 3 — Support bundle

- Wire the existing `exportSupportBundle()` to a **Export support bundle** button in
  Settings → About.
- The exporter writes a timestamped zip into `~/Downloads`, containing the local
  command audit log, diagnostics output and app version — never chat history.
- After writing, reveal the file with `NSWorkspace.activateFileViewerSelectingURLs`.
- Show success/error inline. No network call, no upload service.

## Feature 4 — Multi-proxy pool

- Settings' proxy `TextField` becomes a `TextEditor` so a list can be pasted
  (one `host:port` per line, old single-value input still valid).
- Store the list as newline-separated text in the existing `custom_proxy` field of
  `lane/egress_mode.json`; `proxy_chain.py` already owns the index and rotation.
- `scripts/zen-lane-macos.sh` stops building the proxy URL from the raw string. It
  calls `get_current_custom_proxy()` from `proxy_chain.py` and uses the returned
  host/port/type to build `HTTPS_PROXY`, matching the upstream rotation behavior.
- Preserve the existing WARP / 41987 fallback order.

## Feature 5 — Tor route + fallback

- Add **Tor** to the route picker (`mode = "tor"`).
- The setup gate gains a **fifth, optional** row: **Tor (optional)**, status
  `OK — 127.0.0.1:9050` or `Not installed`, with an **Install Tor** button that
  runs `brew install tor`. It uses an amber icon when missing and is excluded from
  `setupComplete`, so it never blocks Continue. The four required checks stay
  unchanged.
- Availability check: a SOCKS5 probe on `127.0.0.1:9050` (or `:9150` for Tor
  Browser) using `tor_manager.get_active_tor_target()`. If absent, show
  "Tor is not running — install with `brew install tor`" and keep the option disabled.
- Lane maps Tor to `socks5://127.0.0.1:<active port>` via the same config path as
  custom proxies.
- Auto-fallback: when a guard rotation fails to get a fresh WARP address and the
  user has enabled Tor fallback in Settings, write a flag file that the guard reads
  to use the Tor target for the next hop. The flag is cleared on the next successful
  WARP hop. This reuses the Tor branches already present in `zen_guard.py`.
- Settings gets a **Tor fallback** toggle, disabled until Tor is reachable.

## Install progress in the setup gate

All install buttons (OpenCode, WARP, App, Runtime and the optional Tor row) show a
percentage while they run. Shell installers (`curl | bash`, `brew`, the bundled
script) do not stream a reliable byte percentage, so the value is an elapsed-time
estimate against an expected duration per step, capped at 95% until the command
finishes, when it becomes 100%. The UI labels it as an estimate (`~`) so it is not
mistaken for a measured value. State lives in
`@Published var setupProgress: [String: Double]`; a main-queue timer updates it
while `setupInstalling` is set, and clears the value when the step completes.

## Error handling and safety

- Every new action runs on a background queue and publishes back on the main queue.
- No action touches a running Start/Stop/Switch operation (`guard operationState`).
- Failures keep the previous configuration intact and show a readable message.
- No chat data, credentials or sessions leave the Mac.

## Verification

- `bash build-app.sh` and `bash build-dmg.sh` must succeed.
- Clean endpoint test: run in Settings, confirm ms values appear and applying one
  updates `lane/egress_mode.json`.
- Send again: trigger with a deliberate failed continue in a test session; confirm
  the announce result matches `resend_failed()`.
- Support bundle: confirm the zip appears in Downloads and contains logs, not chats.
- Proxy pool: paste two proxies, apply, confirm the lane uses the rotated entry and
  that a hop changes it.
- Tor: with Tor running on 9050, select Tor and confirm the lane's proxy target;
  with Tor absent, confirm the option is disabled and the hint is shown.
- Failure cases for each: no network, missing Python module, bad proxy string,
  unreachable Tor — app stays usable and the error is inline.
