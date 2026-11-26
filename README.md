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
