# Local HTTP API

The account and Place API uses Python's standard library and SQLite. Start it with:

```sh
python -m alertmesh.api
```

The API and matching flow share one database. It defaults to `alertmesh.sqlite3`; set `ALERTMESH_DB_PATH` to choose another file. Email verification is disabled until `ALERTMESH_SMTP_HOST` is configured. SMTP configuration also accepts `ALERTMESH_SMTP_PORT`, `ALERTMESH_SMTP_SENDER`, `ALERTMESH_SMTP_USERNAME`, and `ALERTMESH_SMTP_PASSWORD`. The local server binds to `127.0.0.1:8000`; `ALERTMESH_HOST` and `ALERTMESH_PORT` override that address.

Register with `POST /api/register` using `{"email":"person@example.com","password":"at-least-12-chars"}`. The verification token is sent by email and is submitted to `POST /api/verify` as `{"token":"..."}`. Sign in at `POST /api/login`; use its bearer access token for the remaining endpoints.

- `GET /api/me` and `DELETE /api/me`
- `GET /api/places`, `POST /api/places`, `PATCH /api/places/{id}`, and `DELETE /api/places/{id}`
- `GET /api/inbox`
- `POST /api/logout`

New Places require a name and coordinates. Radius defaults to 25 km, both hazard types default to enabled, and minimum weather severity defaults to moderate. The development server is intended for local use; deploy behind HTTPS before exposing account credentials or bearer tokens over a network.
