# Third-party notices

The following are direct dependencies declared by this repository. Their software remains subject to their own licenses; the POOLMASTER Source Review License does not relicense them.

| Dependency | Declared version | Source | License reported by package metadata |
|---|---|---|---|
| `python-telegram-bot` | `22.8` | [Project](https://github.com/python-telegram-bot/python-telegram-bot) | LGPL-3.0-only |
| `python-dotenv` | `1.2.4` | [Project](https://github.com/theskumar/python-dotenv) | BSD-3-Clause |
| `httpx` | `0.28.1` | [Project](https://github.com/encode/httpx) | BSD-3-Clause |
| `Pillow` | `13.0.0` | [Project](https://github.com/python-pillow/Pillow) | MIT-CMU |
| `pytest` (development) | `>=8.3,<10` | [Project](https://github.com/pytest-dev/pytest) | MIT |

Indirect dependencies may carry additional licenses. Consult each package's license and metadata before redistribution.

## Widget development tools

Node.js is a development/CI runtime, not bundled application code (project: https://nodejs.org/; license: https://github.com/nodejs/node/blob/main/LICENSE). Widget builders invoke [Terser](https://github.com/terser/terser) 5.51.2 (BSD-2-Clause, reported by the pinned package metadata). These tools and their transitive dependencies retain their own licenses.

Optional Telegram widget SDK/native validation tools are supplied separately and are not redistributed in this repository. Their own terms apply. The TST outline is project-specific vector artwork, covered by the project license; it does not embed a third-party font or the private crown logo.
