# Provenance

- Project: POOLMASTER
- Private canonical repository: `DrKarpov/POOLMASTER`
- Public repository: `DrKarpov/poolmaster-tg`
- Public release: `v1.0.0`
- Private source release: `v1.0.0`
- Private source commit: `b73674be5e8b7edbff97a5a92c8f011cbb01a5f1`

The public source is derived from the private POOLMASTER v1.0.0 production release. The public snapshot contains a minimal compatibility change allowing the chart renderer to operate without private branding assets. Private branding assets and deployment-specific files are intentionally omitted.

Private project branding assets are intentionally omitted from the public source snapshot.

The public repository begins with a clean Git history; private branches, pull requests, and operational history are not copied. The public release tag identifies the public snapshot and does not claim byte-for-byte identity with the private production tree.

## Explicit public differences

- Public README, user documentation, legal notices, dependency notices, and provenance.
- Public GitHub configuration and root-commit-safe CI whitespace check.
- Removal of internal operational/audit documentation and the internal release-gate changelog.
- `deploy/**` is intentionally excluded.
- `assets/jetton_logo.svg` and `assets/jetton_logo.png` are intentionally excluded.
- `chart.py` makes the private logo optional; the corresponding renderer tests are updated. The internal preview-script test is excluded with its internal audit tool.
- The supplied portfolio images are included at `docs/assets/price-chart.png` and `docs/assets/farming-chart.png`; only ancillary text metadata was stripped, with decoded pixels preserved.

No other application source differences are intended. The final source-difference audit compares the public snapshot against the private v1.0.0 source tree.
