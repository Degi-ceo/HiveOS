"""Allow the systemd gateway unit to launch the CLI with Python ``-B -m``."""

from hive.surfaces.cli import main

raise SystemExit(main())
