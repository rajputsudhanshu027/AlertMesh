# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

People monitoring multiple U.S. Places for official weather and earthquake Alerts.

## Product Purpose

AlertMesh collects official National Weather Service and USGS source alerts, matches them to saved Places, and presents one Inbox entry per Person and Alert. Success means people can review relevant active Alerts, understand changes and history, and see when source data may be delayed.

## Positioning

Weather and earthquake Alerts from separate official sources are brought together and matched to each Person's Places, with duplicate Inbox entries suppressed across those Places.

## Operating Context

People add named Places, choose per-Place hazard and weather severity preferences, and review matching Alerts in an Inbox. The current web app provides a seeded local demo and account flows for registration, verification, sign-in, Place management, and Inbox review.

## Capabilities and Constraints

- The initial coverage is the contiguous United States.
- Weather Alerts use official affected areas when available; earthquake events match Places by point and radius.
- Canceled and expired Alerts remain in Inbox history. Source delay status and last-checked time are visible.
- AlertMesh is a portfolio demonstration, not a guaranteed emergency warning service. Email delivery is not guaranteed.
- Preserve existing demo and account behavior while redesigning the UI.

## Evidence on Hand

The repository contains the domain glossary, MVP specification, application code, seeded demo, and deterministic test suite. Do not invent customer endorsements, operational guarantees, or live-feed status for the local demo.

## Product Principles

- Make relevant Alerts easy to scan and follow.
- Show source freshness clearly and retain last-known Alerts during outages.
- Keep one Inbox entry for an Alert even when it matches several Places.
- Explain uncertainty and delivery status without implying guaranteed delivery.
