# AlertMesh

AlertMesh helps people follow official emergency alerts that affect places they care about. This context defines the language for people, places, alerts, and their delivery.

## People and places

**Person**:
A verified-email account holder who manages alert preferences and receives matching alerts.
_Avoid_: User, subscriber

**Place**:
A named U.S. point and radius that a person wants monitored. A person may have multiple places, each with its own hazard and severity preferences.
_Avoid_: Location, area, geofence

## Alerts

**Source alert**:
An alert published by an official provider, such as the National Weather Service or USGS.
_Avoid_: Event (when referring to a provider's published alert)

**Alert**:
AlertMesh's record of a source alert, including its current state and prior revisions. One alert may match several places belonging to the same person.
_Avoid_: Notification, message

**Revision**:
A materially distinct published version of an alert, such as a change to its severity, affected area, instructions, or expected end time.
_Avoid_: Update (unless referring to the act of changing an alert)

**Match**:
The relationship between an alert and a place when the alert's affected area covers the place, or when an earthquake point falls within the place's radius.
_Avoid_: Subscription hit

## Delivery

**Inbox entry**:
The person's view of a matching alert in AlertMesh, retained after that alert is canceled or expires.
_Avoid_: Notification

**Email notification**:
An email sent to a person about a newly published matching alert or a material change to an alert they already received.
_Avoid_: Alert (when referring to the email itself)
