---
title: "Loop watchdog diagnostics"
description: "Capture the first missed loop probe without delaying supervised recovery"
---

# Loop watchdog diagnostics

Enable first-miss evidence in the running gateway's profile, then restart the gateway:

```yaml
gateway:
  loop_watchdog: true
  loop_watchdog_diagnostics: true
```

`loop_watchdog_diagnostics` defaults to `false`. This keeps the existing healthy
probe path unchanged and lets operators choose when to retain thread metadata.
When enabled, healthy probes only keep a ring of 16 timing records in memory.
There are no stack walks, filesystem operations, logging calls or diagnostic
worker wakeups until a probe misses its deadline.

The first miss captures Python stacks before logging or file output. The latest
incident is saved as `<HERMES_HOME>/logs/gateway-loop-watchdog.json`, using the
process profile's home. It includes:

- Monotonic `scheduled_at` immediately before submission, `deadline` after
  submission, `observed_at` when the result is sampled, and `acknowledged_at`
  from the loop callback. A null acknowledgement means none had been observed
  when this snapshot was assembled. These are process monotonic times, not UTC.
- The first-miss `captured_at`, thread names, Python identifiers, native thread
  identifiers, and innermost-first stacks. The monitored loop is listed first.
- Best-effort metadata for the loop's existing default executor and the gateway's
  existing work executor: capacity, approximate queue size, worker identifiers,
  worker count and name prefix. Missing or unsupported pools are marked
  unavailable. Collecting evidence never creates or submits work to an executor.

Further missed probes and the first successful probe update the timing history
and recovery flag while preserving the original first-miss stacks. Late
acknowledgements remain associated with the probe that scheduled them. History
is limited to the latest 16 probes; a subsequent stall replaces the incident.

Output is bounded to 64 threads, 32 frames per thread, truncated filename/function/
thread-name fields, and 512 KiB per file. Truncation is flagged. No source lines,
frame locals or request payloads are collected. One daemon writer can hold one
pending update, replacing older pending updates if storage stalls. Writes replace
the file atomically using one staging file, so disk use is at most 1 MiB across
the report and staging file. The writer retires when drained. Collection or write
errors do not stop probing and are not sent through a potentially blocked logger.

The restart decision still uses `loop_watchdog_probe_interval_s`,
`loop_watchdog_probe_timeout_s` and `loop_watchdog_max_strikes`, with defaults
30 seconds, 10 seconds and three misses. The exit code remains 75. Final stderr,
logging and lifecycle reporting, plus any pending snapshot write, share a maximum
one-second reporting budget before exit; a blocked output cannot suppress recovery.
Shutdown disarming still wins before exit. Evidence is best effort: a blocked disk
may leave an older complete report or a partial staging file, and process-wide
loss of execution time can delay both watchdog and snapshot collection.

`gateway.loop_watchdog: false` disables the lifetime loop watchdog and selector
floor even when diagnostics are enabled. It does not disable the separate startup
or shutdown-drain watchdogs. Use nested YAML as shown above, not a literal dotted
YAML key. A root-level `loop_watchdog` key also works and takes precedence over
the nested value when both are present.
