# Loop watchdog evidence and opt-out verification

September 14, 2026. Branch: `investigate/loop-stalls`. Base: local
`upstream/main`, `5eb99eb2844b22ebb723711b8e6a0bbb80bb5f04`.
The two production stalls remain undetermined. No remote host was accessed.

## First-miss evidence

Added `gateway.loop_watchdog_diagnostics`, default `false`, through the dataclass,
YAML bridge, serialization, startup guard and CLI defaults. Operators enable it
in the process profile and restart the gateway. Default-off preserves the existing
healthy probe path and makes retention of thread metadata an explicit choice.
It is safe to leave enabled: healthy probes retain only 16 timing records, without
stack collection, file output, log calls or diagnostic worker wakeups.

The first missed probe captures Python stacks into scalar data before output.
The artifact at `<HERMES_HOME>/logs/gateway-loop-watchdog.json` contains monotonic
submission, deadline, observation and acknowledgement times, thread names and
native identifiers, and existing loop-default/gateway executor capacity, worker
identifiers and approximate queue sizes. Subsequent samples preserve the original
stacks while recording late acknowledgements and recovery.

The report is bounded to 64 threads, 32 frames per thread, truncated string fields
and 512 KiB. It replaces the previous incident. One writer and one pending update
bound memory even if disk output blocks. The report plus its fixed staging file
use at most 1 MiB. No source lines or frame locals are collected. Failures are
contained without logging through potentially blocked handlers.

See [operator and implementation details](website/docs/developer-guide/loop-watchdog-diagnostics.md).

## Current defect found and fixed

Upstream `gateway/shutdown_watchdog.py:127` logs synchronously before the fatal
stack dump at line 132, and the lifecycle write at line 137 also precedes
`os._exit`. A blocked logger, dump or lifecycle write can therefore prevent
supervisor recovery indefinitely. This is a reproduced current defect, independent
of the unknown causes of the production stalls.

Final reporting now runs in a daemon with a shared one-second budget for final
reporting and any pending first-miss write. The fatal dump precedes logging.
The decision still uses exactly the configured consecutive-miss threshold, with
unchanged interval/timeout defaults and exit code 75. Shutdown disarming still
wins before exit, and a disarmed dump does not stamp a watchdog exit in the ledger.
Output remains best effort if disk, stderr or the whole process cannot progress.

## Issue #76541 verdict

**The normal config.yaml opt-out was broken in v0.19.1. It works in this checkout.**
The [reporter's final comment](https://github.com/NousResearch/hermes-agent/issues/76541#issuecomment-5155175422)
identifies release commit `cc4cab2f592e60a197e796506de9168f74baf3ea`.
The closure cited the guard and dataclass parser but missed the real loader.

| Evidence | What it establishes |
| --- | --- |
| [9cd72968498976118337ed9d7af3b4af558a984a](https://github.com/NousResearch/hermes-agent/commit/9cd72968498976118337ed9d7af3b4af558a984a), July 24 | Added the boolean and guard. At this commit, `gateway/run.py:7772` returns when disabled. |
| Release `cc4cab2f`, July 30, `gateway/config.py:938`, `:1146`, `:1211` | Field exists, flat/nested dataclass parsing exists, and the result is assigned. |
| Release `cc4cab2f`, `gateway/run.py:10158` | The disabled guard exists. It was not a Windows-specific missing check. |
| Release `cc4cab2f`, `gateway/config.py:1260`, `:1298`, `:1300`, `:1399`, `:1742` | The real loader builds a flat `gw_data`, bridges selected keys, omits `loop_watchdog`, and passes only `gw_data` to the dataclass. Both nested and root-level config.yaml opt-outs are lost. |
| [8ee0103ea2f85f277b465a4558d6fa85e7739b6b](https://github.com/NousResearch/hermes-agent/commit/8ee0103ea2f85f277b465a4558d6fa85e7739b6b), August 22, `gateway/config.py:1537` | Added the missing bridge, explicitly including the pre-existing boolean. This fix is an ancestor of the inspected `upstream/main`. |
| `5820d0b0d5`, then `fb0f7806ee` | Extracted the loader into `gateway/config_loader.py`, then made the bridge table-driven. Current lines 39-42 explain the original failure; lines 80-83 bridge the knobs. |
| Current `gateway/config.py:720`, `gateway/run_startup.py:605` | The loaded false value reaches the guard, which returns before either timer or watchdog is armed. |

`config=None` does arm the guard if the method is invoked on an incomplete runner.
That is covered by an existing test, but it is not the normal startup path:
`GatewayRunner.__init__` at current `gateway/run.py:3354` and release
`gateway/run.py:5519` loads configuration whenever the constructor receives None.
`load_gateway_config_for_runner` returns a configuration object; a failed load
raises or falls back to a configuration, not a None result. There is only one
production caller of `start_loop_liveness_watchdog`, inside the guarded method.

Other configuration distinctions are explicit rather than inferred causes:

- Current YAML accepts a root-level `loop_watchdog` or nested `gateway` mapping.
  Root-level presence wins. A literal dotted YAML key is not interpreted as a path.
- An unreadable or malformed config can fall back to defaults with a warning;
  managed configuration can override the user layer. Profiles read their own home.
  The lifetime watchdog belongs to the process, including in multiplex mode.
- The standalone `gateway/run.py --config` route passes YAML directly to
  `GatewayConfig.from_dict`, so it bypasses the historically broken ordinary loader.
  Legacy `gateway.json` was also passed through to the dataclass.
- The lifetime-loop opt-out does not disable the separate startup and shutdown-drain
  watchdogs. The reporter's quoted consecutive-probe log is specifically the
  lifetime watchdog, so that distinction does not dismiss this report.

The release defect is sufficient to explain a correctly nested config.yaml being
ignored. No assumption about the reporter's exact YAML formatting, profile or
network root cause is needed. GitHub was read only; no issue or PR was changed.

## Tests and mutation proof

Added `tests/gateway/test_loop_watchdog_diagnostics.py` and
`tests/gateway/test_loop_watchdog_config_startup.py`, with 13 parameterized cases:
real asyncio loop stalls and executor saturation, late acknowledgements,
bounded output across repeated stalls, disabled diagnostics, failing initialization/collection,
failed/blocked writes, blocked fatal reporting and real YAML-to-startup wiring.
Extended the existing shutdown-disarm contract to check the lifecycle ledger too.

All mutations were applied temporarily in this worktree and restored in `finally`:

| Mutation | Observed failure |
| --- | --- |
| Omit both `recorder.observe` calls | First-miss test failed because no snapshot appeared during the held loop stall. |
| Restore the entire upstream watchdog file | All three blocked-final-report cases failed because exit 75 was never reached while log, dump or ledger output was held. |
| Remove `loop_watchdog` from the YAML bridge | Disabled-startup case failed with `True` instead of `False`, recreating the historical loss. |
| Remove `loop_watchdog_diagnostics` from the YAML bridge | Enabled-diagnostics case failed with `False` instead of `True`. |
| Bypass the disabled guard | Disabled-startup case failed because the arm boundary was called. |

Each mutation run used the required interpreter and `scripts/run_tests.sh` with
`--file-retries 0`; every run returned exit 1. The fatal-report defect was tested
against the actual local `upstream/main` file, not an approximation.

Validation: 12 relevant suite files, **202 passed, 0 failed, 2 skipped** on macOS,
with retries disabled. Coverage includes gateway config, startup failures, loop/
shutdown/systemd/startup watchdogs, CLI config coercion, Telegram network and
initialization handling, and both previous real-network diagnostic controls.
The skips are platform-specific witness tests. Ruff and `git diff --check` pass.
An initial test cleanup race was corrected; subsequent focused and broader runs
passed on their first attempt. No remaining test failure is classified as a
regression.

Command prefix for every suite and mutation run:

```sh
HERMES_PYTHON=/Users/alex007/hermes-pr/hermes-agent/.venv/bin/python \
  ./scripts/run_tests.sh <paths> --file-retries 0
```
