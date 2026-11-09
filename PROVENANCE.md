# Provenance

- Project: POOLMASTER
- Private canonical repository: `DrKarpov/POOLMASTER`
- Public repository: `DrKarpov/poolmaster-tg`
- Public snapshot version: `v1.1.0`
- Stable private source release: `v1.1.0`
- Private source commit: `9c7420d20eaad532c663d594c8836707a9f83deb`

This source-review snapshot is derived from the stable private release. Public Git history is independent; private branches, PRs, operational reports, runtime state, credentials and deployment material are not copied. The original public v1.0.0 history/tag is retained.

## Explicit application-source differences

| File | Public-only compatibility |
|---|---|
| `chart.py` | Retains the existing optional-logo loader/draw behavior, allowing legacy PNG rendering without private branding files. |
| `assets/widgets/price/logo.svg` | Original crown replaced by the owner-approved TST outline. It is used by the farming header; the price header remains text-only. |
| `assets/widgets/coin/widget.html` | Embedded crown path replaced by the same TST outline; placement/scale and bevel offset adapted to its viewBox. Accessible mark labels refer to TST. Randomness, timing, haptics and animation behavior remain unchanged. |
| `coin_widget.py` | Matching Russian/English accessible-label substitutions refer to TST. |

`assets/jetton_logo.svg` and `assets/jetton_logo.png` remain excluded. The original crown is not embedded elsewhere in the public widget assets. Bot names and actual market/pool identifiers remain as source data; blurring the portfolio ticker does not alter market validation.

No other application-source differences are intended. All remaining root Python modules, widget assets, builder/backup/import scripts, requirements and the historical seed are compared with the exact private source tree. This is not a claim of byte-for-byte identity with production.

## Public tests and tooling

- Existing missing-logo adaptations in `tests/test_chart_rendering.py` and `tests/test_farming_history_chart.py` are retained.
- `tests/test_coin_widget.py` checks public localized labels/budgets without importing the private concept document; runtime animation/haptic tests remain.
- `tests/test_public_branding.py` checks TST usage and loading without private logo files.
- `tests/native_widget_probe.py` accepts `POOLMASTER_WIDGET_SDK_DIR` for separately supplied optional SDK bundles; the private development checkout is not required.
- The internal preview-tool test is excluded along with its private audit tool, as in v1.0.0. Other application tests are retained.
- Public CI keeps the root-commit-safe whitespace check and adds explicit Node 22/template verification. Public CODEOWNERS and Dependabot settings are retained.

## Documentation, images and license

README, public guides, changelog, legal/dependency notices and security-version information are maintained independently of operational records. The proprietary POOLMASTER Source Review License v1.0 is preserved; TST does not introduce a third-party font or a general reuse license.

The four portfolio PNGs at `docs/assets/widgets/` are owner-approved browser renders using saved market observations, with original logos and the related ticker selectively blurred. Non-target pixels match the original renders. They illustrate the original interface; the public application's TST compatibility is intentionally distinct. Source snapshots for the images were taken on 2026-11-09; displayed values are not live quotes. The old `docs/assets/price-chart.png` and `docs/assets/farming-chart.png` previews are replaced in the current tree.

Private image originals, generation inputs, data fixtures, source manifests, audit reports, internal concepts/plans and deployment documents are excluded. No complete private documentation tree is exported.
