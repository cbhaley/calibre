#!/usr/bin/env python

__license__ = 'GPL v3'
__copyright__ = '2011, Kovid Goyal <kovid@kovidgoyal.net>'
__docformat__ = 'restructuredtext en'

import json
import mimetypes
import os
import shutil
import sys
import time
from argparse import ArgumentParser, FileType
from collections import OrderedDict
from pprint import pprint
from subprocess import check_call


class ReadFileWithProgressReporting:  # {{{

    def __init__(self, path, mode='rb'):
        self.fobj = open(path, mode)
        self.fobj.seek(0, os.SEEK_END)
        self._total = self.fobj.tell()
        self.fobj.seek(0)
        self.start_time = time.time()

    def tell(self, *a):
        return self.fobj.tell(*a)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.fobj.close()
        del self.fobj

    def __len__(self):
        return self._total

    def read(self, size):
        data = self.fobj.read(size)
        if data:
            self.report_progress(len(data))
        return data

    def report_progress(self, size):
        sys.stdout.write('\x1b[s')
        sys.stdout.write('\x1b[K')
        frac = float(self.tell()) / self._total
        mb_pos = self.tell() / float(1024**2)
        mb_tot = self._total / float(1024**2)
        kb_pos = self.tell() / 1024.0
        kb_rate = kb_pos / (time.time() - self.start_time)
        bit_rate = kb_rate * 1024
        eta = int((self._total - self.tell()) / bit_rate) + 1
        eta_m, eta_s = eta / 60, eta % 60
        sys.stdout.write(
            f'  {frac * 100:.1f}%   {mb_pos:.1f}/{mb_tot:.1f}MB {kb_rate:.1f} KB/sec    {eta_m} minutes, {eta_s} seconds left'

        )
        sys.stdout.write('\x1b[u')
        if self.tell() >= self._total:
            sys.stdout.write('\n')
            t = int(time.time() - self.start_time) + 1
            print(
                f'Upload took {t/60} minutes and {t % 60} seconds at {kb_rate:.1f} KB/sec'

            )
        sys.stdout.flush()

# }}}


class Base:  # {{{

    def __init__(self):
        self.d = os.path.dirname
        self.j = os.path.join
        self.a = os.path.abspath
        self.b = os.path.basename
        self.s = os.path.splitext
        self.e = os.path.exists

    def info(self, *args, **kwargs):
        print(*args, **kwargs)
        sys.stdout.flush()

    def warn(self, *args, **kwargs):
        print('\n' + '_' * 20, 'WARNING', '_' * 20)
        print(*args, **kwargs)
        print('_' * 50)
        sys.stdout.flush()

# }}}


class SourceForge(Base):  # {{{

    # Note that you should manually ssh once to username,project@frs.sourceforge.net
    # on the staging server so that the host key is setup

    def __init__(self, files, project, version, username, replace=False):
        self.username, self.project, self.version = username, project, version
        self.base = '/home/frs/project/c/ca/' + project
        self.rdir = self.base + '/' + version
        self.files = files

    def __call__(self):
        for x in self.files:
            start = time.time()
            self.info('Uploading', x)
            for i in range(5):
                try:
                    check_call([
                        'rsync', '-h', '-zz', '--progress', '-e', 'ssh -x', x,
                        '{},{}@frs.sourceforge.net:{}'.format(self.username, self.project, self.rdir + '/')
                    ])
                except KeyboardInterrupt:
                    raise SystemExit(1)
                except Exception:
                    print('\nUpload failed, trying again in 30 seconds')
                    time.sleep(30)
                else:
                    break
            print('Uploaded in', int(time.time() - start), 'seconds\n\n')

# }}}


class GitHub(Base):  # {{{

    API = 'https://api.github.com/'

    def __init__(self, files, reponame, version, username, password, replace=False):
        self.files, self.reponame, self.version, self.username, self.password, self.replace = (
            files, reponame, version, username, password, replace
        )
        self.current_tag_name = 'v' + self.version
        import requests
        self.requests = s = requests.Session()
        s.auth = (self.username, self.password)
        s.headers.update({'Accept': 'application/vnd.github.v3+json'})

    def __call__(self):
        releases = self.releases()
        self.clean_older_releases(releases)
        release = self.create_release(releases)
        upload_url = release['upload_url'].partition('{')[0]
        existing_assets = self.existing_assets(release['id'])
        for path, desc in self.files.items():
            self.info('')
            url = self.API + f'repos/{self.username}/{self.reponame}/releases/assets/{{}}'
            fname = os.path.basename(path)
            if fname in existing_assets:
                self.info(f'Deleting {fname} from GitHub with id: {existing_assets[fname]}')
                r = self.requests.delete(url.format(existing_assets[fname]))
                if r.status_code != 204:
                    self.fail(r, f'Failed to delete {fname} from GitHub')
            r = self.do_upload(upload_url, path, desc, fname)
            if r.status_code != 201:
                self.fail(r, f'Failed to upload file: {fname}')
            try:
                r = self.requests.patch(
                    url.format(r.json()['id']),
                    data=json.dumps({
                        'name': fname,
                        'label': desc
                    })
                )
            except Exception:
                time.sleep(15)
                r = self.requests.patch(
                    url.format(r.json()['id']),
                    data=json.dumps({
                        'name': fname,
                        'label': desc
                    })
                )
            if r.status_code != 200:
                self.fail(r, f'Failed to set label for {fname}')

    def clean_older_releases(self, releases):
        for release in releases:
            if release.get('assets',
                           None) and release['tag_name'] != self.current_tag_name:
                self.info(
                    '\nDeleting old released installers from: {}'.format(release['tag_name'])
                )
                for asset in release['assets']:
                    r = self.requests.delete(
                        self.API + 'repos/{}/{}/releases/assets/{}'.format(self.username, self.reponame, asset['id'])
                    )
                    if r.status_code != 204:
                        self.fail(
                            r, 'Failed to delete obsolete asset: {} for release: {}'.format(asset['name'], release['tag_name'])
                        )

    def do_upload(self, url, path, desc, fname):
        mime_type = mimetypes.guess_type(fname)[0] or 'application/octet-stream'
        self.info(f'Uploading to GitHub: {fname} ({mime_type})')
        with ReadFileWithProgressReporting(path) as f:
            return self.requests.post(
                url,
                headers={
                    'Content-Type': mime_type,
                    'Content-Length': str(f._total)
                },
                params={'name': fname},
                data=f
            )

    def fail(self, r, msg):
        print(msg, f' Status Code: {r.status_code}', file=sys.stderr)
        print('JSON from response:', file=sys.stderr)
        pprint(dict(r.json()), stream=sys.stderr)
        raise SystemExit(1)

    def already_exists(self, r):
        error_code = r.json().get('errors', [{}])[0].get('code', None)
        return error_code == 'already_exists'

    def existing_assets(self, release_id):
        url = self.API + f'repos/{self.username}/{self.reponame}/releases/{release_id}/assets'
        r = self.requests.get(url)
        if r.status_code != 200:
            self.fail('Failed to get assets for release')
        return {asset['name']: asset['id'] for asset in r.json()}

    def releases(self):
        url = self.API + f'repos/{self.username}/{self.reponame}/releases'
        r = self.requests.get(url)
        if r.status_code != 200:
            self.fail(r, 'Failed to list releases')
        return r.json()

    def create_release(self, releases):
        ' Create a release on GitHub or if it already exists, return the existing release '
        for release in releases:
            # Check for existing release
            if release['tag_name'] == self.current_tag_name:
                return release
        url = self.API + f'repos/{self.username}/{self.reponame}/releases'
        r = self.requests.post(
            url,
            data=json.dumps({
                'tag_name': self.current_tag_name,
                'target_commitish': 'master',
                'name': f'version {self.version}',
                'body': f'Release version {self.version}',
                'draft': False,
                'prerelease': False
            })
        )
        if r.status_code != 201:
            self.fail(r, f'Failed to create release for version: {self.version}')
        return r.json()

# }}}


def generate_index():  # {{{
    os.chdir('/srv/download')
    releases = set()
    for x in os.listdir('.'):
        if os.path.isdir(x) and '.' in x:
            releases.add(tuple(int(y) for y in x.split('.')))
    rmap = OrderedDict()
    for rnum in sorted(releases, reverse=True):
        series = rnum[:2] if rnum[0] == 0 else rnum[:1]
        if series not in rmap:
            rmap[series] = []
        rmap[series].append(rnum)

    template = '''<!DOCTYPE html>\n<html lang="en"> <head> <meta charset="utf-8"> <title>{title}</title><link rel="icon" type="image/png" href="//calibre-ebook.com/favicon.png" /> <style type="text/css"> {style} </style> </head> <body> <h1>{title}</h1> <p>{msg}</p> {body} </body> </html> '''  # noqa: E501
    style = '''
    body { font-family: sans-serif; background-color: #eee; }
    a { text-decoration: none; }
    a:visited { color: blue }
    a:hover { color: red }
    ul { list-style-type: none }
    li { padding-bottom: 1ex }
    dd li { text-indent: 0; margin: 0 }
    dd ul { padding: 0; margin: 0 }
    dt { font-weight: bold }
    dd { margin-bottom: 2ex }
    '''
    body = []
    for series in rmap:
        body.append(
            '<li><a href="{0}.html" title="Releases in the {0}.x series">{0}.x</a>\xa0\xa0\xa0<span style="font-size:smaller">[{1} releases]</span></li>'  # noqa: E501
            .format('.'.join(map(str, series)), len(rmap[series]))
        )
    body = '<ul>{}</ul>'.format(' '.join(body))
    index = template.format(
        title='Previous calibre releases',
        style=style,
        msg='Choose a series of calibre releases',
        body=body
    )
    with open('index.html', 'wb') as f:
        f.write(index.encode('utf-8'))

    for series, releases in rmap.items():
        sname = '.'.join(map(str, series))
        body = [
            '<li><a href="{0}/" title="Release {0}">{0}</a></li>'.format(
                '.'.join(map(str, r))
            ) for r in releases
        ]
        body = '<ul class="release-list">{}</ul>'.format(' '.join(body))
        index = template.format(
            title=f'Previous calibre releases ({sname}.x)',
            style=style,
            msg='Choose a calibre release',
            body=body
        )
        with open(f'{sname}.html', 'wb') as f:
            f.write(index.encode('utf-8'))

        for r in releases:
            rname = '.'.join(map(str, r))
            os.chdir(rname)
            try:
                body = []
                files = os.listdir('.')
                windows = [x for x in files if x.endswith('.msi')]
                if windows:
                    def wdesc(x):
                        return 'Windows ' + ('64-bit' if '-64bit-' in x else '32-bit') + ' Installer'
                    windows = [
                        '<li><a href="{0}" title="{1}">{1}</a></li>'.format(x, wdesc(x)) for x in windows
                    ]
                    body.append(
                        '<dt>Windows</dt><dd><ul>{}</ul></dd>'.format(
                            ' '.join(windows)
                        )
                    )
                portable = [x for x in files if '-portable-' in x]
                if portable:
                    body.append(
                        '<dt>Calibre Portable</dt><dd><a href="{0}" title="{1}">{1}</a></dd>'
                        .format(portable[0], 'Calibre Portable Installer')
                    )
                osx = [x for x in files if x.endswith('.dmg')]
                if osx:
                    body.append(
                        '<dt>Apple Mac</dt><dd><a href="{0}" title="{1}">{1}</a></dd>'
                        .format(osx[0], 'OS X Disk Image (.dmg)')
                    )
                linux = [
                    x for x in files if x.endswith(('.txz', 'tar.bz2'))
                ]
                if linux:
                    def ldesc(x):
                        if 'i686' in x:
                            return 'Linux Intel 32-bit binary'
                        if 'arm64' in x:
                            return 'Linux ARM 64-bit binary'
                        return 'Linux Intel 64-bit binary'
                    linux = [
                        '<li><a href="{0}" title="{1}">{1}</a></li>'.format(x, ldesc(x)) for x in linux
                    ]
                    body.append(
                        '<dt>Linux</dt><dd><ul>{}</ul></dd>'.format(
                            ' '.join(linux)
                        )
                    )
                source = [x for x in files if x.endswith(('.xz', '.gz'))]
                if source:
                    body.append(
                        '<dt>Source Code</dt><dd><a href="{0}" title="{1}">{1}</a></dd>'
                        .format(source[0], 'Source code (all platforms)')
                    )

                body = '<dl>{}</dl>'.format(''.join(body))
                index = template.format(
                    title=f'calibre release ({rname})',
                    style=style,
                    msg='',
                    body=body
                )
                with open('index.html', 'wb') as f:
                    f.write(index.encode('utf-8'))
            finally:
                os.chdir('..')
# }}}


SERVER_BASE = '/srv/download/'


def upload_to_servers(files, version):  # {{{
    base = SERVER_BASE
    dest = os.path.join(base, version)
    if not os.path.exists(dest):
        os.mkdir(dest)
    for src in files:
        shutil.copyfile(src, os.path.join(dest, os.path.basename(src)))
    cwd = os.getcwd()
    try:
        generate_index()
    finally:
        os.chdir(cwd)

    # for server, rdir in {'files':'/srv/download/'}.items():
    #     print('Uploading to server:', server)
    #     server = f'{server}.calibre-ebook.com'
    #     # Copy the generated index files
    #     print('Copying generated index')
    #     check_call(['rsync', '-hza', '-e', 'ssh -x', '--include', '*.html',
    #                 '--filter', '-! */', base, f'root@{server}:{rdir}'])
    #     # Copy the release files
    #     rdir = f'{rdir}{version}/'
    #     for x in files:
    #         start = time.time()
    #         print('Uploading', x)
    #         for i in range(5):
    #             try:
    #                 check_call(['rsync', '-h', '-z', '--progress', '-e', 'ssh -x', x,
    #                 f'root@{server}:{rdir}'])
    #             except KeyboardInterrupt:
    #                 raise SystemExit(1)
    #             except Exception:
    #                 print('\nUpload failed, trying again in 30 seconds')
    #                 time.sleep(30)
    #             else:
    #                 break
    #         print('Uploaded in', int(time.time() - start), 'seconds\n\n')
# }}}


# CLI {{{
def cli_parser():
    epilog = 'Copyright Kovid Goyal 2012'

    p = ArgumentParser(
        description='Upload project files to a hosting service automatically',
        epilog=epilog
    )
    a = p.add_argument
    a(
        'appname',
        help='The name of the application, all files to'
        ' upload should begin with this name'
    )
    a(
        'version',
        help='The version of the application, all files to'
        ' upload should contain this version'
    )
    a(
        'file_map',
        type=FileType('r'),
        help='A file containing a mapping of files to be uploaded to '
        'descriptions of the files. The descriptions will be visible '
        'to users trying to get the file from the hosting service. '
        'The format of the file is filename: description, with one per '
        'line. filename can be a path to the file relative to the current '
        'directory.'
    )
    a(
        '--replace',
        action='store_true',
        default=False,
        help='If specified, existing files are replaced, otherwise '
        'they are skipped.'
    )

    subparsers = p.add_subparsers(
        help='Where to upload to',
        dest='service',
        title='Service',
        description='Hosting service to upload to'
    )
    sf = subparsers.add_parser(
        'sourceforge', help='Upload to sourceforge', epilog=epilog
    )
    gh = subparsers.add_parser('github', help='Upload to GitHub', epilog=epilog)
    subparsers.add_parser('calibre', help='Upload to calibre file servers')

    a = sf.add_argument
    a('project', help='The name of the project on sourceforge we are uploading to')
    a('username', help='Sourceforge username')

    a = gh.add_argument
    a('project', help='The name of the repository on GitHub we are uploading to')
    a('username', help='Username to log into your GitHub account')
    a('password', help='Password to log into your GitHub account')

    return p


def main(args=None):
    cli = cli_parser()
    args = cli.parse_args(args)
    files = {}
    with args.file_map as f:
        for line in f:
            fname, _, desc = line.partition(':')
            fname, desc = fname.strip(), desc.strip()
            if fname and desc:
                files[fname] = desc

    ofiles = OrderedDict()
    for x in sorted(files, key=lambda x: os.stat(x).st_size, reverse=True):
        ofiles[x] = files[x]

    if args.service == 'sourceforge':
        sf = SourceForge(
            ofiles, args.project, args.version, args.username, replace=args.replace
        )
        sf()
    elif args.service == 'github':
        gh = GitHub(
            ofiles,
            args.project,
            args.version,
            args.username,
            args.password,
            replace=args.replace
        )
        gh()
    elif args.service == 'calibre':
        upload_to_servers(ofiles, args.version)


if __name__ == '__main__':
    main()
# }}}
