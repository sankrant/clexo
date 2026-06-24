# TODO

## Index + snapshot retention (`index_retention_days`)

**Problem.** The only retention knob today is `archive_retention_days`
(`_prune_archives`, `clexo/cli.py`), which prunes only the gzipped transcript
archives. The FTS index (`sessions` + `messages` in `index.db`) and the
snapshot files (`chain-*.md` / `refresh-*.md`) have no expiry path and grow
unbounded — the index is already the largest store (~106 MB / 712 sessions as
of 2026-06-24).

**Proposal.** Add a new `index_retention_days` config key (default `0` =
forever, mirroring `archive_retention_days`) that, during `clexo sync`:

- Deletes `sessions` + `messages` rows (and FTS entries) for sessions whose
  `last_ts` is older than the cutoff.
- Deletes the corresponding `chain-<sid>.md` / `refresh-<sid>.md` snapshots.
- **Exempts tagged sessions** — always kept, same rule as `_prune_archives`.
- Is keyed/pruned per-session so a re-run is idempotent and resumable.

**Notes / edge cases.**
- Keep it a separate knob from `archive_retention_days` — someone may want to
  keep searchable metadata long after dropping the heavy archive, or vice
  versa.
- A session with no `last_ts` should fall back to a stable timestamp (archive
  mtime, like `_prune_archives` does) rather than being treated as age-0.
- Don't prune a session that still has a live source JSONL on disk.
- Log a one-line count of what was pruned (no silent deletion).
- Docs: `docs/architecture.md` (storage/retention) + the config section.

Context: discussed 2026-06-24 — see also `archive_retention_days` (commit
`d85f37d`) which this parallels.
