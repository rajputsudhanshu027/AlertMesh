# Deterministic alert replay

Run this command to reset a fresh in-memory SQLite database and replay seeded NWS and USGS alerts against sample People and Places:

```sh
python -m alertmesh.demo
```

The replay uses an in-memory email adapter and makes no network requests. It prints inbox and delivery outcomes for matching, duplicate replay, multi-Place deduplication, catch-up, revisions, expiry, retry, source staleness, and deletion.
