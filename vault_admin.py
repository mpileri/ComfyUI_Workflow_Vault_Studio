"""Admin tools for a shared (studio) Workflow Vault. See `python vault_admin.py --help`."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from workflow_vault.admin import main  # noqa: E402

if __name__ == "__main__":
    main()
