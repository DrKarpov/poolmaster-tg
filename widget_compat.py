"""Client-local compatibility for Telegram Desktop's numeric JSON parser."""
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def numeric_compat_script() -> str:
    return (Path(__file__).resolve().parent /
            'assets/widgets/numeric-compat.min.js').read_text().strip()


def with_numeric_compat(html: str) -> str:
    # Run before the widget's first state decode. Correct clients take the
    # bootstrap's early return and retain their original parser/array prototype.
    if '<script>' not in html:
        raise ValueError('widget has no script entry point')
    return html.replace('<script>', '<script>' + numeric_compat_script(), 1)
