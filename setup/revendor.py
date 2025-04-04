#!/usr/bin/env python
# License: GPL v3 Copyright: 2019, Eli Schwartz <eschwartz@archlinux.org>

import os
import shutil
import tarfile
import time
from io import BytesIO

from setup import Command, download_securely, is_ci


class ReVendor(Command):

    # NAME = TAR_NAME = VERSION = DOWNLOAD_URL = ''
    CAN_USE_SYSTEM_VERSION = True

    def add_options(self, parser):
        parser.add_option(f'--path-to-{self.NAME}', help=f'Path to the extracted {self.TAR_NAME} source')
        parser.add_option(f'--{self.NAME}-url', default=self.DOWNLOAD_URL,
                help=f'URL to {self.TAR_NAME} source archive in tar.gz format')
        if self.CAN_USE_SYSTEM_VERSION:
            parser.add_option(f'--system-{self.NAME}', default=False, action='store_true',
                    help=f'Treat {self.TAR_NAME} as system copy and symlink instead of copy')

    def download_securely(self, url: str) -> bytes:
        num = 5 if is_ci else 1
        for i in range(num):
            try:
                return download_securely(url)
            except Exception as err:
                if i == num - 1:
                    raise
                self.info(f'Download failed with error "{err}" sleeping and retrying...')
                time.sleep(2)

    def download_vendor_release(self, tdir, url):
        self.info(f'Downloading {self.TAR_NAME}:', url)
        raw = self.download_securely(url)
        with tarfile.open(fileobj=BytesIO(raw)) as tf:
            tf.extractall(tdir)
            if len(os.listdir(tdir)) == 1:
                return self.j(tdir, os.listdir(tdir)[0])
            else:
                return tdir

    def add_file_pre(self, name, raw):
        pass

    def add_file(self, path, name):
        with open(path, 'rb') as f:
            raw = f.read()
        self.add_file_pre(name, raw)
        dest = self.j(self.vendored_dir, *name.split('/'))
        base = os.path.dirname(dest)
        if not os.path.exists(base):
            os.makedirs(base)
        if self.use_symlinks:
            os.symlink(path, dest)
        else:
            with open(dest, 'wb') as f:
                f.write(raw)

    def add_tree(self, base, prefix, ignore=lambda n:False):
        for dirpath, dirnames, filenames in os.walk(base):
            for fname in filenames:
                f = os.path.join(dirpath, fname)
                name = prefix + '/' + os.path.relpath(f, base).replace(os.sep, '/')
                if not ignore(name):
                    self.add_file(f, name)

    @property
    def vendored_dir(self):
        return self.j(self.RESOURCES, self.NAME)

    def clean(self):
        if os.path.exists(self.vendored_dir):
            shutil.rmtree(self.vendored_dir)
