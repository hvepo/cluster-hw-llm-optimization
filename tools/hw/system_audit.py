#!/usr/bin/env python3
"""Backwards-compatible alias for tools/hw/hardware_probe.py."""
from hardware_probe import analyze_hwinfo, collect_node_hardware_profile, main  # noqa: F401

if __name__ == "__main__":
    main()
