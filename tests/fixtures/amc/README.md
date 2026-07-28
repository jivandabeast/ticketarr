# AMC email fixtures

Drop **sanitized real AMC emails** here as `.eml` files. Tests in
`tests/test_amc_parser.py` are parametrized over every file that matches one
of the filename prefixes below. If this directory is empty the tests simply
skip — the suite always runs green.

## How to export a `.eml`

- **Gmail (web):** open the message → three-dot menu → **Download message**.
- **Apple Mail:** select the message → **File → Save As… → Raw Message Source**.
- **Thunderbird:** right-click → **Save As… → EML File**.
- **Outlook:** open the message → **File → Save As → .eml**.

## Filename conventions

The parser test file inspects the filename prefix to know what to assert:

| Prefix               | What it should contain                                                       |
| -------------------- | ---------------------------------------------------------------------------- |
| `reservation_*.eml`  | Real AMC A-List / order confirmation with a real ticket                      |
| `cancellation_*.eml` | Reservation cancellation / refund email                                      |
| `thankyou_*.eml`     | Post-visit "Thank You for Visiting AMC …" email                              |
| `skip_*.eml`         | AMC email that must be ignored (concessions, membership, Popcorn Pass, etc.) |

Anything not matching a prefix above is ignored.

## Optional sidecar assertions

For any `foo.eml` you can add `foo.expected.json` with a subset of the fields
below. The test will assert exact matches on whatever keys you provide:

```json
{
  "order_number": "123456789",
  "title": "Dune: Part Two",
  "theater_name": "AMC Foo 12",
  "showtime": "2024-03-14T22:30:00+00:00",
  "skip_reason": "concession order"
}
```

`showtime` uses ISO-8601 with a `+00:00` offset (the parser normalizes to UTC).

## Sanitizing

Before committing anything to source control:

- Redact your real name, seat numbers, and credit-card last-4.
- The `order_number` and `ticket_confirmation` are fine to keep — they're
  otherwise meaningless outside AMC's systems — but scrub them if you'd
  rather not have them public.
- Keep the AMC subject line, `From:` header, and body markers intact — the
  parser relies on them.

## Privacy

Both `tests/fixtures/**/*.eml` **and** `tests/fixtures/**/*.expected.json`
are gitignored. Raw emails contain PII (name, gmail address, order numbers,
seat assignments), and the sidecar files carry the derived versions of those
same fields — so neither is safe to commit. Everything under this directory
stays local to your machine.

That means the test suite is intentionally **local-only**: it runs against
whatever you've dropped in, and gracefully collects zero items on a fresh
clone. If a contributor wants coverage, they need to plug in their own
sanitized emails.
