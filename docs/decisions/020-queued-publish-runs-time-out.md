# 020: a publish or unpublish run nobody claims fails itself

- **Status:** accepted
- **Date:** 2026-09-19

## Context

Since the publish gate (issues 41 and 42), `Store.active_publish_run` holds a
draft's text and image set still while its run is `queued` or `building`, and
`approve` refuses a second run while one is in flight. That is only safe if
every run leaves those two states. A `building` run is recovered by
`publisher.recover_stuck_runs` at start. A `queued` run leaves only when the
publisher claims it, and `publisher.tick` does not claim anything until a
GitHub App or a test-token repo is configured: it logs "leaving queued" and
returns. With nothing configured, an approved draft is frozen for edits with
no exit (issue 44). The same shape applies to an `unpublish` run once the gate
covers that kind (issue 43).

The visible fix was an admin cancel or requeue control. That adds a route and a
permission surface for a state that is a symptom of a missing configuration,
and Scott asked for the smaller change: a timeout on the queued task.

## Decision

A run of either kind that is still `queued` longer than
`CHRONICLE_PUBLISH_QUEUE_TIMEOUT_SECONDS` (default 900, 15 minutes) fails
itself. `publisher.expire_unclaimed_runs` does it through the ordinary
`Store.finish_run` failure path, so the transition table stays the only
decider of a draft's status: a publish run returns the draft from `approved`
to `in_review` (already re-approvable), and an unpublish run leaves it
`published`. The run's `result` carries `error_class: publish_queue_timeout`
and a plain-language `message`; the same text lands as a `chronicle`-authored
feedback entry on the draft, which is the surface the editor already shows.
A failed unpublish now writes that entry too (it did not before), since its
draft has no status change to signal the failure.

The sweep is called by `publisher.run_loop` after every tick, not from inside
`tick`. `tick` returns early when no target is configured, which is exactly
when a run goes unclaimed, so a sweep inside it would be skipped in the one
case it exists for. The age is measured from the run's `created_at`.

Only `queued` runs are touched. A `building` run that stalls is the existing
recovery's job (`recover_stuck_runs` for publish and unpublish, the lease for
the builder), and this does not duplicate it.

Why 900 seconds: a healthy run is claimed within one poll (5 seconds by
default) and finishes in seconds, so 15 minutes is far beyond any normal
claim latency, while an operator who is partway through GitHub App setup does
not lose an approval to a short timer. It matches the watcher's maximum
backoff (ADR 013). It is read through `_float_env` like every other interval
there, so a shorter value is one environment variable away.

## Consequences

- No new route, no new permission, no admin action. Recovery is the same one
  an author already has after any failed publish: read the feedback entry,
  fix the cause (configure the target), and approve again.
- With no target configured, every approval now fails after the timeout
  rather than waiting. That is the intended change: the queue no longer
  hides a missing configuration behind a frozen draft.
- A run requeued by `recover_stuck_runs` keeps its original `created_at`, so
  one that was already older than the timeout when the api restarted is
  failed by the first sweep if the tick did not take it. The tick runs first
  in each iteration, so a configured target claims it before the sweep sees
  it.
- Assumes a single api replica, as ADR 013 does: the sweep runs on the
  publisher thread and shares its serialisation with the claim.
