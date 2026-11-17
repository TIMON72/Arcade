# TVON Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make TV power-on, Raspberry HDMI selection, picture/audio recovery, and service shutdown reliable on the production Batocera cabinet.

**Architecture:** Keep the existing staged supervisor and CEC keeper. Replace process-liveness success checks with CEC evidence, keep the CEC session alive while the service runs, and make all exit paths deterministic. Preserve soft-before-hard audio recovery and hotplug suppression.

**Tech Stack:** Python 3 standard library, Bash service wrapper, HDMI-CEC `cec-client`, Batocera/RPi5 DRM and PipeWire.

## Global Constraints

- No new Python dependencies and no broad TVON rewrite.
- Preserve `on <device>`, `as`, `tx 1f:82:<pa>`, and never send CEC `q`.
- Existing config keys remain compatible; no automatic deployment to the second cabinet.
- Do not commit or push without a separate user request.
- Tests are inline Python checks because this repository has no test framework.

---

### Task 1: Honest CEC and DRM verification

**Files:**
- Modify: `scripts/tvon/tvon.py:313-321,494-499,630-707,1381-1408`

**Interfaces:**
- `active_source_ok(active, physical_address)` accepts only evidence for the requested physical address.
- `query_active_source()` remains the independent CEC scan used for verification.
- `_drm_output_has_mode()` returns `False` when both DRM metadata files are absent.

- [x] Write failing inline checks proving an unrelated recorder does not pass, a keeper cannot be verified without scan evidence, and missing DRM metadata fails.
- [x] Run the checks and confirm failure against current behavior.
- [x] Require the requested PA in `active_source_ok`; verify keeper state using `query_active_source()` after its claim and during later checks.
- [x] Track the last successful PA in the keeper and restart it if its process dies.
- [x] Require at least one existing DRM metadata file before accepting picture readiness.
- [x] Re-run the checks and `python -m py_compile scripts/tvon/tvon.py`.

### Task 2: Persistent keeper and cleanup

**Files:**
- Modify: `scripts/tvon/tvon.py:1681-1776`
- Modify: `services/tvon:44-61,89-99`

**Interfaces:**
- `supervise()` keeps the CEC keeper alive for the whole service lifetime and cleans it on every exit.
- SIGTERM/SIGINT stops the supervisor loop and its CEC child.
- The service wrapper returns the actual command status.

- [x] Add a failing static check showing bounded `active_source_keep_sec` shutdown and missing top-level `finally` cleanup.
- [x] Replace the bounded hold with a supervised loop that rechecks the keeper and picture/source periodically.
- [x] Add signal handlers and `try/finally` cleanup; make startup failures clean up too.
- [x] Change the service wrapper to `exit "$?"` after `case` and explicitly terminate leftover `cec-client` on stop.
- [x] Verify with inline checks, `bash -n services/tvon`, and Python compilation.

### Task 3: Real deadlines and outcome status

**Files:**
- Modify: `scripts/tvon/tvon.py:214-228,440-453,1643-1761`

**Interfaces:**
- `wait_tv_power_on()` uses a monotonic wall-clock deadline.
- The final service status includes current picture, Active Source, and audio when enabled.

- [x] Add a failing check using mocked monotonic time/commands to prove the old sleep counter exceeds its deadline.
- [x] Bound the whole wake loop by `time.monotonic() + max_wait_sec`, preserving per-command timeouts without extending the outer deadline.
- [x] Refresh audio verification after post-check and include it in the final return status.
- [x] Return nonzero when enabled audio recovery failed, while preserving logs and recovery order.
- [x] Run inline checks and compilation.

### Task 4: Documentation and focused regression review

**Files:**
- Modify: `README.md:309-314`
- Modify: `scripts/tvon/config.toml:25-32`

- [x] Document that the CEC keeper is supervised permanently and service stop closes it.
- [x] Remove the obsolete two-minute hold description from config comments.
- [x] Review the final diff for regressions in PA fallback, signal cleanup, and existing Raspberry-only behavior.

### Task 5: Deploy and exercise the real cabinet

**Files:**
- No new files.

- [x] Deploy with `deploy/deploy.ps1 192.168.3.156 -Auth linux -Update -NoLogs`.
- [x] Confirm `timer`, `server`, and `tvon` start; verify the USB Serial timer remains unaffected.
- [x] Inspect TVON logs for power status, active-source scan evidence, picture/audio verification, and absence of stale CEC processes.
- [x] Restart TVON once and confirm the old keeper is cleaned and a new supervised keeper starts.
- [x] Leave the cabinet online with the production keeper running; report any TV-specific limitation instead of claiming an unverified result.
