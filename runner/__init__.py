"""Tau Ceti review runner."""
import os
import sys

if os.environ.get("TAUCETI_CONF") or any(arg == "--conf" or arg.startswith("--conf=") for arg in sys.argv[1:]):
    from tauceti_tools.project import bootstrap
    bootstrap()
