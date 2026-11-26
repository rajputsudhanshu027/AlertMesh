# Source ingestion

AlertMesh fetches current NWS alerts and recent actual NWS alert messages, plus the USGS all earthquakes GeoJSON feed for the past seven days. The recent NWS messages let the flow process updates and cancellations while the active endpoint continues to include alerts that have been active longer than a week. Each normalized alert carries source status, original publication time, and revision time, then is sent through the same SQLite backed `AlertMesh` application flow used by the API. NWS source ID references are persisted so revisions retain one Alert identity. Events omitted from a successful complete feed snapshot are marked expired; their Inbox entries remain for 90 days.

Run one ingestion pass:

```sh
python -m alertmesh.ingest --once
```

Run continuously with the default 60 second interval:

```sh
python -m alertmesh.ingest
```

The process uses `ALERTMESH_DB_PATH` for the shared database and requires `ALERTMESH_SMTP_HOST` to send alert emails. Configure `ALERTMESH_SMTP_PORT`, `ALERTMESH_SMTP_SENDER`, `ALERTMESH_SMTP_USERNAME`, and `ALERTMESH_SMTP_PASSWORD` as needed. Set `ALERTMESH_USER_AGENT` to identify the client to the NWS API. Retry behavior can be tuned with `ALERTMESH_SOURCE_MAX_ATTEMPTS`, `ALERTMESH_SOURCE_RETRY_BASE_SECONDS`, and `ALERTMESH_SOURCE_RETRY_MAX_SECONDS`.

The one pass command prints per source success, attempt and record counts, and last checked time. It does not print person identifiers or email addresses. Temporary source failures use bounded exponential backoff, mark that source stale, and leave stored alerts and inbox entries in place. A later successful fetch clears the stale flag.

Source references: [NWS API documentation](https://www.weather.gov/documentation/services-web-api) and [USGS GeoJSON feed documentation](https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php).
