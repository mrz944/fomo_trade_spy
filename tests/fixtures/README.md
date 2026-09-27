# Fixture provenance

`fomo-captured.json` contains six unmodified swap rows and minimal public trader
identity from the authenticated cache of the paper service, captured 2026-09-27.
Profile/media URLs and unrelated personal profile fields were omitted. No API key,
authorization header, secret file, or funded signing data is included.

`base-captured.json` contains a historical Base block, canonical receipt and
callTracer response read on 2026-09-27. It is a schema/decoder fixture, not evidence
that the discovered FOMO trader executed this transaction.

`ChainFixture` in the integration tests constructs explicitly synthetic transaction
amounts, identities and timestamps using the captured receipt format. Its eligibility
and simulated fills exist only in pytest temporary databases. They must never be
imported into deployed state or described as observed real trading.
