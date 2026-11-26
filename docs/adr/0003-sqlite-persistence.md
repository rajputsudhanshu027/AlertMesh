# SQLite persistence for application state

AlertMesh stores people, places, normalized alerts, inbox entries, delivery status, emailed material revisions, and source freshness in SQLite. The application flow continues to accept an email adapter and selects its database path with `ALERTMESH_DB_PATH` (default: `alertmesh.sqlite3`); tests and embedded callers can pass `db_path=":memory:"`.

Opening the application initializes the schema with SQLite `CREATE TABLE IF NOT EXISTS` migrations. The database enforces unique person/alert inbox identity and foreign key cleanup for linked place and delivery records. Each application state update is saved in one SQLite transaction. Inbox terminal records are retained for 90 days and are physically removed by `prune_expired()`; inbox reads also run this cleanup.

Alert ingestion reloads state and serializes matching, deduplication, delivery status, and persistence in one SQLite write transaction. Other mutations currently save an application snapshot and are intended for the portfolio demo's single application instance; multi-worker use should move those operations into repository-level transactions too.
