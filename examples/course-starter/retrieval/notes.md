# Synthetic workshop reference

This entire document describes a fictional workshop service for a retrieval exercise. These statements are not facts about Loom, its releases, operators or customers.

## Reservation window

A workshop room can be reserved up to fourteen days in advance. Reservations close two hours before the session begins. A reservation lasts ninety minutes. Cancellation does not automatically reserve another room.

## Fixture reset

The classroom fixture resets at 06:00 UTC each Monday. A reset removes the synthetic workshop reservations and restores the supplied sample records. The exercise has no real student identities or private documents.

## Retry policy

The fictional client retries an unsuccessful read twice, with a one-second delay between attempts. It does not automatically retry a write. Students record each attempt separately in their local worksheet.

## Export format

The workshop exercise exports records as UTF-8 JSON Lines. Each line contains one complete JSON object. An export includes the reservation identifier and room identifier. It does not include payment or attendance information.

## Report retention

The sample workshop service retains its synthetic activity reports for thirty days. Reports are grouped by room identifier. The exercise does not define a legal retention policy or describe a real organization's records.

## Support boundary

The fictional workshop handout provides troubleshooting steps for the local exercise. It does not promise a mentor, support response time, course credit, certification, deployment assistance or a service availability target.
