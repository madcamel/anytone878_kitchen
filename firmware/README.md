# firmware/

Put your own copy of the stock AnyTone firmware here. It is **not** distributed with this repo.

    python3 scripts/get_firmware.py <the vendor .zip or an extracted folder>

copies and hash-checks the files the harness is built against:

| Model | Files | Package |
|---|---|---|
| AT-D878UV | `D878UV_V4.00_20250725.CDD/.CDI/.spi` | D878UV V4.00 (2025-07-25) |
| AT-D878UVII | `D878UVII_V4.00_20250725.CDD/.CDI/.spi` | D878UVII V4.00 (2025-07-25) |

The packages come from the AnyTone distributors' download pages (e.g. BridgeCom's AnyTone 878
support pages). Every address in `annotations/` and every patch targets these exact builds; other
versions are rejected by hash.
