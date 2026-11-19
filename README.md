# courtdocket

A small offline data-processing service with a local HTTP fixture and SQLite storage. It is self-contained and does not require network access.

## Usage

```text
make run       # process the bundled source
make run_inc   # process using the incremental command-line mode
make schedule  # print today's 庭期结果 (use ASOF=2030-01-03T00:00:00Z to look back)
make test      # run the test suite
make mock      # start the local fixture service
```

The package exposes a Python entry point under `src/courtdocket`. The fixture service supports the source formats used by the package, and the command-line entry point writes results to the local SQLite database. Re-running commands is safe for an existing database.

## Hearing-event interpretation

Every collected notice is classified into a hearing event: `filed` (新收),
`scheduled` (开庭通知), `postponed` (延期), `rescheduled` (改期), `withdrawn`
(撤诉), `closed` (结案), `archived` (作废) or `unknown` (未知输入). Notices
with a missing case id or timestamp are invalid (无效通知); they are stored
and flagged, never silently dropped.

`python -m courtdocket.cli --schedule` prints the per-case 庭期结果 as of
now; adding `--as-of <ISO ts>` answers the same question for a historical
point in time. Both views are folded from the same stored notices with
these rules:

* notices are replayed in notice-time order, so an earlier-issued notice can
  never displace a later-issued one, no matter when it arrives (排期不倒退);
* terminal events (撤诉/结案/作废) are sticky — a later scheduling notice is
  held and flagged as `regressed` instead of silently reviving the case;
* a missing record is just missing: portal blocking or a lost page aborts
  the run without writes and is never interpreted as 撤诉;
* every case lists its full notice history (`effective` / `superseded` /
  `regressed` / `invalid`) so each result is traceable.

Full, incremental and resumed runs ingest the same notices (dedup is by
content hash over the source fields, and the watermark is only a progress
marker), so they never produce contradictory 庭期.
