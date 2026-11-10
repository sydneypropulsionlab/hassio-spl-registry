"""PDF induction certificates, rendered with Typst from certificate.typ."""
import json
from pathlib import Path

import typst

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / 'certificate.typ'
FONTS = HERE / 'fonts'


def render(data):
    """Return the certificate PDF (bytes) for a dict of display strings."""
    return typst.compile(str(TEMPLATE), root=str(HERE), font_paths=[str(FONTS)],
                         sys_inputs={'data': json.dumps(data)})


def months_text(n):
    if not n:
        return 'No expiry'
    if n % 12 == 0:
        y = n // 12
        return f'{y} year' + ('s' if y != 1 else '')
    return f'{n} month' + ('s' if n != 1 else '')
