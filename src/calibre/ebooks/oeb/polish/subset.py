#!/usr/bin/env python


__license__   = 'GPL v3'
__copyright__ = '2013, Kovid Goyal <kovid at kovidgoyal.net>'
__docformat__ = 'restructuredtext en'

import os
import sys
from io import BytesIO

from calibre import as_unicode, prints
from calibre.ebooks.oeb.base import OEB_DOCS, OEB_STYLES, XPath, css_text
from calibre.ebooks.oeb.polish.utils import OEB_FONTS
from calibre.utils.fonts.subset import subset
from calibre.utils.fonts.utils import get_font_names
from polyglot.builtins import iteritems


def remove_font_face_rules(container, sheet, remove_names, base):
    changed = False
    for rule in tuple(sheet.cssRules):
        if rule.type != rule.FONT_FACE_RULE:
            continue
        try:
            uri = rule.style.getProperty('src').propertyValue[0].uri
        except (IndexError, KeyError, AttributeError, TypeError, ValueError):
            continue
        name = container.href_to_name(uri, base)
        if name in remove_names:
            sheet.deleteRule(rule)
            changed = True
    return changed


def iter_subsettable_fonts(container):
    for name, mt in iteritems(container.mime_map):
        if (mt in OEB_FONTS or name.rpartition('.')[-1].lower() in {'otf', 'ttf'}):
            yield name, mt


def subset_all_fonts(container, font_stats, report):
    remove = set()
    total_old = total_new = 0
    changed = False
    for name, mt in iter_subsettable_fonts(container):
        chars = font_stats.get(name, set())
        with container.open(name, 'rb') as f:
            f.seek(0, os.SEEK_END)
            font_size = f.tell()
        if not chars:
            remove.add(name)
            report(_('Removed unused font: %s')%name)
            continue
        with container.open(name, 'r+b') as f:
            raw = f.read()
            try:
                font_name = get_font_names(raw)[-1]
            except Exception as e:
                report(
                    f'Corrupted font: {name}, ignoring.  Error: {as_unicode(e)}')
                continue
            warnings = []
            report('Subsetting font: %s'%(font_name or name))
            font_type = os.path.splitext(name)[1][1:].lower()
            output = BytesIO()
            try:
                warnings = subset(BytesIO(raw), output, font_type, chars)
            except Exception as e:
                report(
                    f'Unsupported font: {name}, ignoring. Error: {as_unicode(e)}')
                continue
            nraw = output.getvalue()
            total_old += font_size

            for w in warnings:
                report(w)
            olen = len(raw)
            nlen = len(nraw)
            total_new += len(nraw)
            if nlen == olen:
                report(_('The font %s was already subset')%font_name)
            else:
                report(_('Decreased the font {0} to {1} of its original size').format(
                    font_name, (f'{nlen/olen*100:.1f}%')))
                changed = True
            f.seek(0), f.truncate(), f.write(nraw)

    for name in remove:
        container.remove_item(name)
        changed = True

    if remove:
        for name, mt in iteritems(container.mime_map):
            if mt in OEB_STYLES:
                sheet = container.parsed(name)
                if remove_font_face_rules(container, sheet, remove, name):
                    container.dirty(name)
            elif mt in OEB_DOCS:
                for style in XPath('//h:style')(container.parsed(name)):
                    if style.get('type', 'text/css') == 'text/css' and style.text:
                        sheet = container.parse_css(style.text, name)
                        if remove_font_face_rules(container, sheet, remove, name):
                            style.text = css_text(sheet)
                            container.dirty(name)
    if total_old > 0:
        report(_('Reduced total font size to %.1f%% of original')%(
            total_new/total_old*100))
    else:
        report(_('No embedded fonts found'))
    return changed


if __name__ == '__main__':
    from calibre.ebooks.oeb.polish.container import get_container
    from calibre.ebooks.oeb.polish.stats import StatsCollector
    from calibre.utils.logging import default_log
    default_log.filter_level = default_log.DEBUG
    inbook = sys.argv[-1]
    ebook = get_container(inbook, default_log)
    report = []
    stats = StatsCollector(ebook).font_stats
    subset_all_fonts(ebook, stats, report.append)
    outbook, ext = inbook.rpartition('.')[0::2]
    outbook += '_subset.'+ext
    ebook.commit(outbook)
    prints('\nReport:')
    for msg in report:
        prints(msg)
    print()
    prints('Output written to:', outbook)
