# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License in the project root for
# license information.
# --------------------------------------------------------------------------
"""Compatibility entry point for the local performance exporter."""

from perf_tools.export import main

if __name__ == "__main__":
    raise SystemExit(main())
