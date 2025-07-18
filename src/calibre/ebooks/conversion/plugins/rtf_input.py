__license__   = 'GPL v3'
__copyright__ = '2008, Kovid Goyal <kovid at kovidgoyal.net>'

import glob
import os
import re
import textwrap

from calibre.customize.conversion import InputFormatPlugin, OptionRecommendation
from calibre.utils.resources import get_path as P
from polyglot.builtins import as_bytes, iteritems

border_style_map = {
        'single': 'solid',
        'double-thickness-border': 'double',
        'shadowed-border': 'outset',
        'double-border': 'double',
        'dotted-border': 'dotted',
        'dashed': 'dashed',
        'hairline': 'solid',
        'inset': 'inset',
        'dash-small': 'dashed',
        'dot-dash': 'dotted',
        'dot-dot-dash': 'dotted',
        'outset': 'outset',
        'tripple': 'double',
        'triple': 'double',
        'thick-thin-small': 'solid',
        'thin-thick-small': 'solid',
        'thin-thick-thin-small': 'solid',
        'thick-thin-medium': 'solid',
        'thin-thick-medium': 'solid',
        'thin-thick-thin-medium': 'solid',
        'thick-thin-large': 'solid',
        'thin-thick-thin-large': 'solid',
        'wavy': 'ridge',
        'double-wavy': 'ridge',
        'striped': 'ridge',
        'emboss': 'inset',
        'engrave': 'inset',
        'frame': 'ridge',
}


class RTFInput(InputFormatPlugin):

    name        = 'RTF Input'
    author      = 'Kovid Goyal'
    description = _('Convert RTF files to HTML')
    file_types  = {'rtf'}
    commit_name = 'rtf_input'

    options = {
        OptionRecommendation(name='ignore_wmf', recommended_value=False,
            help=_('Ignore WMF images instead of replacing them with a placeholder image.')),
    }

    def generate_xml(self, stream):
        from calibre.ebooks.rtf2xml.ParseRtf import ParseRtf
        ofile = 'dataxml.xml'
        run_lev, debug_dir, indent_out = 1, None, 0
        if getattr(self.opts, 'debug_pipeline', None) is not None:
            try:
                os.mkdir('rtfdebug')
                debug_dir = 'rtfdebug'
                run_lev = 4
                indent_out = 1
                self.log('Running RTFParser in debug mode')
            except Exception:
                self.log.warn('Impossible to run RTFParser in debug mode')
        parser = ParseRtf(
            in_file=stream,
            out_file=ofile,
            # Convert symbol fonts to unicode equivalents. Default
            # is 1
            convert_symbol=1,

            # Convert Zapf fonts to unicode equivalents. Default
            # is 1.
            convert_zapf=1,

            # Convert Wingding fonts to unicode equivalents.
            # Default is 1.
            convert_wingdings=1,

            # Convert RTF caps to real caps.
            # Default is 1.
            convert_caps=1,

            # Indent resulting XML.
            # Default is 0 (no indent).
            indent=indent_out,

            # Form lists from RTF. Default is 1.
            form_lists=1,

            # Convert headings to sections. Default is 0.
            headings_to_sections=1,

            # Group paragraphs with the same style name. Default is 1.
            group_styles=1,

            # Group borders. Default is 1.
            group_borders=1,

            # Write or do not write paragraphs. Default is 0.
            empty_paragraphs=1,

            # Debug
            deb_dir=debug_dir,

            # Default encoding
            default_encoding=getattr(self.opts, 'input_encoding', 'cp1252') or 'cp1252',

            # Run level
            run_level=run_lev,
        )
        parser.parse_rtf()
        with open(ofile, 'rb') as f:
            return f.read()

    def extract_images(self, picts):
        from binascii import unhexlify

        from calibre.utils.imghdr import what
        self.log('Extracting images...')

        with open(picts, 'rb') as f:
            raw = f.read()
        picts = filter(len, re.findall(br'\{\\pict([^}]+)\}', raw))
        hex_pat = re.compile(br'[^a-fA-F0-9]')
        encs = [hex_pat.sub(b'', pict) for pict in picts]

        count = 0
        imap = {}
        for enc in encs:
            if len(enc) % 2 == 1:
                enc = enc[:-1]
            data = unhexlify(enc)
            fmt = what(None, data)
            if fmt is None:
                fmt = 'wmf'
            count += 1
            name = f'{count:04}.{fmt}'
            with open(name, 'wb') as f:
                f.write(data)
            imap[count] = name
            # with open(name+'.hex', 'wb') as f:
            #     f.write(enc)
        return self.convert_images(imap)

    def convert_images(self, imap):
        self.default_img = None
        for count, val in iteritems(imap):
            try:
                imap[count] = self.convert_image(val)
            except Exception:
                self.log.exception('Failed to convert', val)
        return imap

    def convert_image(self, name):
        if not name.endswith('.wmf'):
            return name
        try:
            return self.rasterize_wmf(name)
        except Exception:
            self.log.exception(f'Failed to convert WMF image {name!r}')
        return self.replace_wmf(name)

    def replace_wmf(self, name):
        if self.opts.ignore_wmf:
            os.remove(name)
            return '__REMOVE_ME__'
        from calibre.ebooks.covers import message_image
        if self.default_img is None:
            self.default_img = message_image('Conversion of WMF images is not supported.'
            ' Use Microsoft Word or LibreOffice to save this RTF file'
            ' as HTML and convert that in calibre.')
        name = name.replace('.wmf', '.jpg')
        with open(name, 'wb') as f:
            f.write(self.default_img)
        return name

    def rasterize_wmf(self, name):
        from calibre.utils.wmf.parse import wmf_unwrap
        with open(name, 'rb') as f:
            data = f.read()
        data = wmf_unwrap(data)
        name = name.replace('.wmf', '.png')
        with open(name, 'wb') as f:
            f.write(data)
        return name

    def write_inline_css(self, ic, border_styles):
        font_size_classes = [f'span.fs{i} {{ font-size: {x}pt }}' for i, x in enumerate(ic.font_sizes)]
        color_classes = [f'span.col{i} {{ color: {x} }}' for i, x in enumerate(ic.colors) if x != 'false']
        css = textwrap.dedent('''
        span.none {
            text-decoration: none; font-weight: normal;
            font-style: normal; font-variant: normal
        }

        span.italics { font-style: italic }

        span.bold { font-weight: bold }

        span.small-caps { font-variant: small-caps }

        span.underlined { text-decoration: underline }

        span.strike-through { text-decoration: line-through }

        ''')
        css += '\n'+'\n'.join(font_size_classes)
        css += '\n' +'\n'.join(color_classes)

        for cls, val in iteritems(border_styles):
            css += f'\n\n.{cls} {{\n{val}\n}}'

        with open('styles.css', 'ab') as f:
            f.write(css.encode('utf-8'))

    def convert_borders(self, doc):
        border_styles = []
        style_map = {}
        for elem in doc.xpath(r'//*[local-name()="cell"]'):
            style = ['border-style: hidden', 'border-width: 1px',
                    'border-color: black']
            for x in ('bottom', 'top', 'left', 'right'):
                bs = elem.get(f'border-cell-{x}-style', None)
                if bs:
                    cbs = border_style_map.get(bs, 'solid')
                    style.append(f'border-{x}-style: {cbs}')
                bw = elem.get(f'border-cell-{x}-line-width', None)
                if bw:
                    style.append(f'border-{x}-width: {bw}pt')
                bc = elem.get(f'border-cell-{x}-color', None)
                if bc:
                    style.append(f'border-{x}-color: {bc}')
            style = ';\n'.join(style)
            if style not in border_styles:
                border_styles.append(style)
            idx = border_styles.index(style)
            cls = f'border_style{idx}'
            style_map[cls] = style
            elem.set('class', cls)
        return style_map

    def convert(self, stream, options, file_ext, log,
                accelerators):
        from lxml import etree

        from calibre.ebooks.metadata.meta import get_metadata
        from calibre.ebooks.metadata.opf2 import OPFCreator
        from calibre.ebooks.rtf.input import InlineClass
        from calibre.ebooks.rtf2xml.ParseRtf import RtfInvalidCodeException
        from calibre.utils.xml_parse import safe_xml_fromstring
        self.opts = options
        self.log = log
        self.log('Converting RTF to XML...')
        try:
            xml = self.generate_xml(stream.name)
        except RtfInvalidCodeException as e:
            self.log.exception('Unable to parse RTF')
            raise ValueError(_('This RTF file has a feature calibre does not '
            'support. Convert it to HTML first and then try it.\n%s')%e)

        d = glob.glob(os.path.join('*_rtf_pict_dir', 'picts.rtf'))
        if d:
            imap = {}
            try:
                imap = self.extract_images(d[0])
            except Exception:
                self.log.exception('Failed to extract images...')

        self.log('Parsing XML...')
        doc = safe_xml_fromstring(xml)
        border_styles = self.convert_borders(doc)
        for pict in doc.xpath('//rtf:pict[@num]',
                namespaces={'rtf':'http://rtf2xml.sourceforge.net/'}):
            num = int(pict.get('num'))
            name = imap.get(num, None)
            if name is not None:
                pict.set('num', name)

        self.log('Converting XML to HTML...')
        inline_class = InlineClass(self.log)
        styledoc = safe_xml_fromstring(P('templates/rtf.xsl', data=True), recover=False)
        extensions = {('calibre', 'inline-class'): inline_class}
        transform = etree.XSLT(styledoc, extensions=extensions)
        result = transform(doc)
        html = 'index.xhtml'
        with open(html, 'wb') as f:
            res = as_bytes(transform.tostring(result))
            # res = res[:100].replace('xmlns:html', 'xmlns') + res[100:]
            # clean multiple \n
            res = re.sub(br'\n+', b'\n', res)
            # Replace newlines inserted by the 'empty_paragraphs' option in rtf2xml with html blank lines
            # res = re.sub(br'\s*<body>', '<body>', res)
            # res = re.sub(br'(?<=\n)\n{2}',
            # '<p>\u00a0</p>\n'.encode('utf-8'), res)
            f.write(res)
        self.write_inline_css(inline_class, border_styles)
        stream.seek(0)
        mi = get_metadata(stream, 'rtf')
        if not mi.title:
            mi.title = _('Unknown')
        if not mi.authors:
            mi.authors = [_('Unknown')]
        opf = OPFCreator(os.getcwd(), mi)
        opf.create_manifest([('index.xhtml', None)])
        opf.create_spine(['index.xhtml'])
        opf.render(open('metadata.opf', 'wb'))
        return os.path.abspath('metadata.opf')

    def postprocess_book(self, oeb, opts, log):
        for item in oeb.spine:
            for img in item.data.xpath('//*[local-name()="img" and @src="__REMOVE_ME__"]'):
                p = img.getparent()
                idx = p.index(img)
                p.remove(img)
                if img.tail:
                    if idx == 0:
                        p.text = (p.text or '') + img.tail
                    else:
                        p[idx-1].tail = (p[idx-1].tail or '') + img.tail
