"""Compare two versions of an induction (e.g. the one a record was signed against and the current one).

Produces plain data for the template: changed settings, changed source documents, and a git-style
checklist diff per section with word-level highlighting of reworded items and SWP excerpts.
"""
import difflib
import re

from markupsafe import Markup, escape

KIND_LABELS = {'check': 'Assessor covers', 'demo': 'Inductee demonstrates', 'ack': 'Inductee acknowledges',
               'info': 'Information'}
FIELDS = [('title', 'Title'), ('location', 'Location'), ('type', 'Type'), ('refresh_months', 'Default refresh (months)'),
          ('grants_access', 'Grants access'), ('description', 'Description')]


def words(a, b):
    """Word-level diff of two strings as (old_html, new_html) with <del>/<ins> marks."""
    ta, tb = re.findall(r'\S+|\s+', a or ''), re.findall(r'\S+|\s+', b or '')
    old, new = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        x, y = escape(''.join(ta[i1:i2])), escape(''.join(tb[j1:j2]))
        if op == 'equal':
            old.append(x)
            new.append(y)
        else:
            if x:
                old.append(Markup('<del>') + x + Markup('</del>'))
            if y:
                new.append(Markup('<ins>') + y + Markup('</ins>'))
    return Markup('').join(old), Markup('').join(new)


def _show(v):
    if isinstance(v, list):
        return ', '.join(map(str, v)) or 'none'
    return 'none' if v in (None, '') else str(v)


def _refs_text(item):
    return '\n\n'.join(f'{r.get("label", "")}: {r.get("text", "")}' for r in item.get('refs', []))


def _item_key(item):
    return f'{item["kind"]}|{item["text"]}'


def _section_rows(old_items, new_items):
    """Rows of ('same'|'added'|'removed'|'changed', old_item, new_item, extras)."""
    rows = []
    a, b = [_item_key(i) for i in old_items], [_item_key(i) for i in new_items]
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == 'equal':
            for o, n in zip(old_items[i1:i2], new_items[j1:j2]):
                refs_changed = _refs_text(o) != _refs_text(n)
                rows.append({'op': 'refs' if refs_changed else 'same', 'old': o, 'new': n,
                             'refs': words(_refs_text(o), _refs_text(n)) if refs_changed else None})
        elif op == 'delete':
            rows += [{'op': 'removed', 'old': o, 'new': None} for o in old_items[i1:i2]]
        elif op == 'insert':
            rows += [{'op': 'added', 'old': None, 'new': n} for n in new_items[j1:j2]]
        else:  # replace: pair the most similar items as rewordings (in order); the rest are added/removed
            olds, news = old_items[i1:i2], new_items[j1:j2]
            pairs, last = {}, -1
            for nj, n in enumerate(news):
                best, best_ratio = None, 0.45
                for oi in range(last + 1, len(olds)):
                    ratio = difflib.SequenceMatcher(None, olds[oi]['text'], n['text']).ratio()
                    if ratio >= best_ratio:
                        best, best_ratio = oi, ratio
                if best is not None:
                    pairs[best] = nj
                    last = best
            oi = nj = 0
            while oi < len(olds) or nj < len(news):
                if oi < len(olds) and oi not in pairs:
                    rows.append({'op': 'removed', 'old': olds[oi], 'new': None})
                    oi += 1
                elif oi < len(olds) and pairs[oi] > nj:
                    rows.append({'op': 'added', 'old': None, 'new': news[nj]})
                    nj += 1
                elif oi < len(olds):
                    o, n = olds[oi], news[nj]
                    ow, nw = words(o['text'], n['text'])
                    rows.append({'op': 'changed', 'old': o, 'new': n, 'old_html': ow, 'new_html': nw,
                                 'kind_changed': o['kind'] != n['kind'],
                                 'refs': words(_refs_text(o), _refs_text(n))
                                 if _refs_text(o) != _refs_text(n) else None})
                    oi += 1
                    nj += 1
                else:
                    rows.append({'op': 'added', 'old': None, 'new': news[nj]})
                    nj += 1
    return rows


def compare(old, new):
    """Compare two induction JSON dicts."""
    fields = []
    for key, label in FIELDS:
        ov, nv = old.get(key), new.get(key)
        if key == 'grants_access':  # older bundles used room_access
            ov, nv = ov or old.get('room_access'), nv or new.get('room_access')
        if (ov or None) != (nv or None):
            if key == 'description':
                ow, nw = words(_show(ov), _show(nv))
                fields.append({'label': label, 'old_html': ow, 'new_html': nw})
            else:
                fields.append({'label': label, 'old': _show(ov), 'new': _show(nv)})

    old_src = {s['ref']: s for s in old.get('sources', [])}
    new_src = {s['ref']: s for s in new.get('sources', [])}
    sources = []
    for ref in list(old_src) + [r for r in new_src if r not in old_src]:
        o, n = old_src.get(ref), new_src.get(ref)
        if not n:
            sources.append({'op': 'removed', 'ref': ref, 'old': o, 'new': None})
        elif not o:
            sources.append({'op': 'added', 'ref': ref, 'old': None, 'new': n})
        elif o.get('hash') != n.get('hash') or o.get('version') != n.get('version'):
            sources.append({'op': 'changed', 'ref': ref, 'old': o, 'new': n,
                            'version_changed': o.get('version') != n.get('version')})

    old_secs = {s['title']: s['items'] for s in old.get('sections', [])}
    new_secs = {s['title']: s['items'] for s in new.get('sections', [])}
    order = [s['title'] for s in new.get('sections', [])]
    order += [t for t in old_secs if t not in new_secs]
    sections = []
    counts = {'added': 0, 'removed': 0, 'changed': 0, 'refs': 0}
    for title in order:
        status = 'added' if title not in old_secs else 'removed' if title not in new_secs else 'same'
        rows = _section_rows(old_secs.get(title, []), new_secs.get(title, []))
        for r in rows:
            if r['op'] in counts:
                counts[r['op']] += 1
        sections.append({'title': title, 'status': status, 'rows': rows,
                         'changed': status != 'same' or any(r['op'] != 'same' for r in rows)})

    identical = old.get('version') == new.get('version')
    return {'fields': fields, 'sources': sources, 'sections': sections, 'counts': counts,
            'identical': identical, 'any': bool(fields or sources or any(s['changed'] for s in sections))}
