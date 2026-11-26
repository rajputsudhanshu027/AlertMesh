# Deterministic alert replay

Run this command to reset a fresh in-memory SQLite database and replay seeded NWS and USGS alerts against sample People and Places:

```sh
python -m alertmesh.demo
```

The replay uses an in-memory email adapter and makes no network requests. It prints inbox and delivery outcomes for matching, duplicate replay, multi-Place deduplication, catch-up, revisions, expiry, retry, source staleness, and deletion.

## Browser preview

Start the application with:

```sh
python -m alertmesh.api
```

Open <http://127.0.0.1:8000/> to view the browser dashboard. The **Reset demo** button reloads the seeded snapshot. The dashboard uses only its local fixture; it does not call NWS, USGS, or an email provider.
