"""
MailJaeger version — single source of truth.

All version references across the codebase (API, UI, tests) MUST import
from this module so that a single edit here propagates everywhere.

Versioning follows Semantic Versioning (https://semver.org):
  MAJOR.MINOR.PATCH
  - MAJOR: incompatible API changes
  - MINOR: new functionality (backward-compatible)
  - PATCH: backward-compatible bug fixes

Bump policy:
  Every significant feature or fix commit should bump VERSION here.
  No automatic tooling — human-controlled, explicit bumps only.
"""

VERSION = "1.2.0"
