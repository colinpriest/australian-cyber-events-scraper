#!/usr/bin/env python3
"""
Legacy entry point for the discovery/enrichment pipeline.

This script remains for backward compatibility. The implementation now lives in
`cyber_data_collector.pipelines.discovery` and is shared with the unified pipeline.
"""

import asyncio
import sys

from cyber_data_collector.pipelines.discovery import main


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n[WARNING] Pipeline interrupted by user")
        sys.exit(130)
    except Exception as exc:
        print(f"[ERROR] Unexpected error: {exc}")
        sys.exit(1)
