# AlertMesh

AlertMesh monitors official U.S. weather and earthquake alerts for places people care about. It matches alerts to saved places, keeps an alert history, and notifies people when a new or materially changed alert affects them.

## MVP

The first release is designed for individuals tracking multiple named places in the contiguous United States.

- Ingest official National Weather Service weather alerts and USGS earthquake events.
- Match weather alerts using their official affected areas; match earthquake points against each place's chosen radius.
- Let people manage hazard and weather-severity preferences for each place.
- Keep one inbox entry per person and alert, listing every matching place.
- Email once for a newly matching alert; send follow-up email only for material changes.
- Show active alerts immediately when a person adds a place, without sending a backdated email.
- Retain canceled and expired alerts in the inbox history.
- Retry temporary source and delivery failures, deduplicate user-visible alerts, and show when source data may be delayed.
- Require verified email accounts. Deleting a place stops future matching; deleting an account removes linked places and notification history.

## Product boundaries

AlertMesh is a portfolio project focused on dependable ingestion, matching, deduplication, and observable delivery. The MVP is a demonstration, not a production-operated emergency warning service. It does not promise email arrival, and it does not include webhooks or international coverage.

## Project decisions

- [Domain glossary](./CONTEXT.md)
- [Place matching decision](./docs/adr/0001-place-matching-uses-source-geography.md)
- [Inbox and email deduplication decision](./docs/adr/0002-one-inbox-entry-per-person-and-alert.md)

## Data sources

- [National Weather Service API](https://www.weather.gov/documentation/services-web-api)
- [USGS earthquake feeds](https://earthquake.usgs.gov/earthquakes/feed/v1.0/)

## Local API

The account and Place API uses Python's standard library and SQLite. Start it with:

```sh
python -m alertmesh.api
```

Set `ALERTMESH_DB_PATH` to choose the SQLite file. Email verification is disabled until `ALERTMESH_SMTP_HOST` is configured. SMTP configuration also accepts `ALERTMESH_SMTP_PORT`, `ALERTMESH_SMTP_SENDER`, `ALERTMESH_SMTP_USERNAME`, and `ALERTMESH_SMTP_PASSWORD`. The local server binds to `127.0.0.1:8000`; `ALERTMESH_HOST` and `ALERTMESH_PORT` override that address.

Open <http://127.0.0.1:8000/> to explore the read-only seeded browser demo. Use **Reset demo** to reload its NWS and USGS sample Alerts; the demo makes no live source requests or real email calls. See [the deterministic demo guide](./docs/deterministic-demo.md).

Register with `POST /api/register` using `{"email":"person@example.com","password":"at-least-12-chars"}`. The verification token is sent by email and is submitted to `POST /api/verify` as `{"token":"..."}`. Sign in at `POST /api/login`; use its bearer access token for the remaining endpoints.

- `GET /api/me` and `DELETE /api/me`
- `GET /api/places`, `POST /api/places`, `PATCH /api/places/{id}`, and `DELETE /api/places/{id}`
- `GET /api/inbox`
- `POST /api/logout`

New Places require a name and coordinates. Radius defaults to 25 km, both hazard types default to enabled, and minimum weather severity defaults to moderate. The development server is intended for local use; deploy behind HTTPS before exposing account credentials or bearer tokens over a network.

