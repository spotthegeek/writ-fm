#!/usr/bin/env python3
"""Generate cached voice samples for WRIT-FM.

This is a one-time maintenance script that materializes short sample clips
for every selectable Kokoro, MiniMax, and Google Gemini voice. The admin UI
uses the cached files directly for voice auditioning.
"""

from __future__ import annotations

import argparse

from station.voice_samples import ensure_voice_samples


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate WRIT-FM voice samples")
    parser.add_argument(
        "--backend",
        choices=["kokoro", "minimax", "google", "all"],
        default="all",
        help="Which voice family to generate",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate samples even if they already exist",
    )
    args = parser.parse_args()

    backends = ["kokoro", "minimax", "google"] if args.backend == "all" else [args.backend]
    created = ensure_voice_samples(backends=backends, force=args.force)

    print("Generated voice samples:")
    for backend, files in created.items():
        print(f"  {backend}: {len(files)} files")
        for path in files:
            print(f"    {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
