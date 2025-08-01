#!/usr/bin/env python


__license__   = 'GPL v3'
__copyright__ = '2010, Kovid Goyal <kovid@kovidgoyal.net>'
__docformat__ = 'restructuredtext en'

import os
from collections import defaultdict
from contextlib import closing
from functools import partial
from threading import Thread

from qt.core import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QIcon,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QSize,
    Qt,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from calibre import as_unicode
from calibre.constants import ismacos
from calibre.db.copy_to_library import copy_one_book
from calibre.gui2 import Dispatcher, choose_dir, error_dialog, gprefs, info_dialog, warning_dialog
from calibre.gui2.actions import InterfaceAction
from calibre.gui2.actions.choose_library import library_qicon
from calibre.gui2.dialogs.progress import ProgressDialog
from calibre.gui2.widgets2 import Dialog
from calibre.startup import connect_lambda
from calibre.utils.config import prefs
from calibre.utils.icu import numeric_sort_key, sort_key
from calibre.utils.localization import ngettext
from polyglot.builtins import iteritems, itervalues


def ask_about_cc_mismatch(gui, db, newdb, missing_cols, incompatible_cols):  # {{{
    source_metadata = db.field_metadata.custom_field_metadata(include_composites=True)
    dest_library_path = newdb.library_path
    ndbname = os.path.basename(dest_library_path)

    d = QDialog(gui)
    d.setWindowTitle(_('Different custom columns'))
    l = QFormLayout()
    tl = QVBoxLayout()
    d.setLayout(tl)
    d.s = QScrollArea(d)
    tl.addWidget(d.s)
    d.w = QWidget(d)
    d.s.setWidget(d.w)
    d.s.setWidgetResizable(True)
    d.w.setLayout(l)
    d.setMinimumWidth(600)
    d.setMinimumHeight(500)
    d.bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)

    msg = _('The custom columns in the <i>{0}</i> library are different from the '
        'custom columns in the <i>{1}</i> library. As a result, some metadata might not be copied.').format(
        os.path.basename(db.library_path), ndbname)
    d.la = la = QLabel(msg)
    la.setWordWrap(True)
    la.setStyleSheet('QLabel { margin-bottom: 1.5ex }')
    l.addRow(la)
    if incompatible_cols:
        la = d.la2 = QLabel(_('The following columns are incompatible - they have the same name'
                ' but different data types. They will be ignored: ') +
                    ', '.join(sorted(incompatible_cols, key=sort_key)))
        la.setWordWrap(True)
        la.setStyleSheet('QLabel { margin-bottom: 1.5ex }')
        l.addRow(la)

    missing_widgets = []
    if missing_cols:
        la = d.la3 = QLabel(_('The following columns are missing in the <i>{0}</i> library.'
                                ' You can choose to add them automatically below.').format(
                                    ndbname))
        la.setWordWrap(True)
        l.addRow(la)
        for k in missing_cols:
            widgets = (k, QCheckBox(_('Add to the %s library') % ndbname))
            l.addRow(QLabel(k), widgets[1])
            missing_widgets.append(widgets)
    d.la4 = la = QLabel(_('This warning is only shown once per library, per session'))
    la.setWordWrap(True)
    tl.addWidget(la)

    tl.addWidget(d.bb)
    d.bb.accepted.connect(d.accept)
    d.bb.rejected.connect(d.reject)
    d.resize(d.sizeHint())
    if d.exec() == QDialog.DialogCode.Accepted:
        changes_made = False
        for k, cb in missing_widgets:
            if cb.isChecked():
                col_meta = source_metadata[k]
                newdb.create_custom_column(
                            col_meta['label'], col_meta['name'], col_meta['datatype'],
                            len(col_meta['is_multiple']) > 0,
                            col_meta['is_editable'], col_meta['display'])
                changes_made = True
        if changes_made:
            # Unload the db so that the changes are available
            # when it is next accessed
            from calibre.gui2.ui import get_gui
            library_broker = get_gui().library_broker
            library_broker.unload_library(dest_library_path)
        return True
    return False
# }}}


class Worker(Thread):  # {{{

    def __init__(self, ids, db, loc, progress, done, delete_after, add_duplicates):
        Thread.__init__(self)
        self.was_canceled = False
        self.ids = ids
        self.processed = set()
        self.db = db
        self.loc = loc
        self.error = None
        self.progress = progress
        self.done = done
        self.left_after_cancel = 0
        self.delete_after = delete_after
        self.auto_merged_ids = {}
        self.add_duplicates = add_duplicates
        self.duplicate_ids = {}
        self.check_for_duplicates = not add_duplicates and (prefs['add_formats_to_existing'] or prefs['check_for_dupes_on_ctl'])
        self.failed_books = {}

    def cancel_processing(self):
        self.was_canceled = True

    def run(self):
        try:
            self.doit()
        except Exception as err:
            import traceback
            try:
                err = str(err)
            except Exception:
                err = repr(err)
            self.error = (err, traceback.format_exc())

        self.done()

    def doit(self):
        from calibre.gui2.ui import get_gui
        library_broker = get_gui().library_broker
        newdb = library_broker.get_library(self.loc)
        self.find_identical_books_data = None
        try:
            if self.check_for_duplicates:
                self.find_identical_books_data = newdb.new_api.data_for_find_identical_books()
            self._doit(newdb)
        finally:
            library_broker.prune_loaded_dbs()

    def _doit(self, newdb):
        for i, x in enumerate(self.ids):
            if self.was_canceled:
                self.left_after_cancel = len(self.ids) - i
                break
            try:
                self.do_one(i, x, newdb)
            except Exception as err:
                import traceback
                err = as_unicode(err)
                self.failed_books[x] = (err, as_unicode(traceback.format_exc()))

    def do_one(self, num, book_id, newdb):
        duplicate_action = 'add'
        if self.check_for_duplicates:
            duplicate_action = 'add_formats_to_existing' if prefs['add_formats_to_existing'] else 'ignore'
        rdata = copy_one_book(
                book_id, self.db, newdb,
                preserve_date=gprefs['preserve_date_on_ctl'],
                duplicate_action=duplicate_action, automerge_action=gprefs['automerge'],
                identical_books_data=self.find_identical_books_data,
                preserve_uuid=self.delete_after
        )
        self.progress(num, rdata['title'])
        if rdata['action'] == 'automerge':
            self.auto_merged_ids[book_id] = _('%(title)s by %(author)s') % dict(title=rdata['title'], author=rdata['author'])
        elif rdata['action'] == 'duplicate':
            self.duplicate_ids[book_id] = (rdata['title'], rdata['authors'])
        self.processed.add(book_id)
# }}}


class ChooseLibrary(Dialog):  # {{{

    def __init__(self, parent, locations):
        self.locations = locations
        Dialog.__init__(self, _('Choose library'), 'copy_to_choose_library_dialog', parent)
        self.resort()
        self.current_changed()

    def resort(self):
        if self.sort_alphabetically.isChecked():
            sorted_locations = sorted(self.locations, key=lambda name_loc: numeric_sort_key(name_loc[0]))
        else:
            sorted_locations = self.locations
        self.items.clear()
        for name, loc in sorted_locations:
            i = QListWidgetItem(name, self.items)
            i.setData(Qt.ItemDataRole.UserRole, loc)
        self.items.setCurrentRow(0)

    def setup_ui(self):
        self.l = l = QGridLayout(self)
        self.items = i = QListWidget(self)
        i.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        i.currentItemChanged.connect(self.current_changed)
        l.addWidget(i)
        self.v = v = QVBoxLayout()
        l.addLayout(v, 0, 1)
        self.sort_alphabetically = sa = QCheckBox(_('&Sort libraries alphabetically'))
        v.addWidget(sa)
        sa.setChecked(bool(gprefs.get('copy_to_library_choose_library_sort_alphabetically', True)))
        sa.stateChanged.connect(self.resort)

        connect_lambda(sa.stateChanged, self, lambda self:
                gprefs.set('copy_to_library_choose_library_sort_alphabetically',
                bool(self.sort_alphabetically.isChecked())))
        la = self.la = QLabel(_('Library &path:'))
        v.addWidget(la)
        le = self.le = QLineEdit(self)
        la.setBuddy(le)
        b = self.b = QToolButton(self)
        b.setIcon(QIcon.ic('document_open.png'))
        b.setToolTip(_('Browse for library'))
        b.clicked.connect(self.browse)
        h = QHBoxLayout()
        h.addWidget(le), h.addWidget(b)
        v.addLayout(h)
        v.addStretch(10)
        bb = self.bb
        bb.setStandardButtons(QDialogButtonBox.StandardButton.Cancel)
        self.delete_after_copy = False
        b = bb.addButton(_('&Copy'), QDialogButtonBox.ButtonRole.AcceptRole)
        b.setIcon(QIcon.ic('edit-copy.png'))
        b.setToolTip(_('Copy to the specified library'))
        b2 = bb.addButton(_('&Move'), QDialogButtonBox.ButtonRole.AcceptRole)
        connect_lambda(b2.clicked, self, lambda self: setattr(self, 'delete_after_copy', True))
        b2.setIcon(QIcon.ic('edit-cut.png'))
        b2.setToolTip(_('Copy to the specified library and delete from the current library'))
        b.setDefault(True)
        l.addWidget(bb, 1, 0, 1, 2)
        self.items.setFocus(Qt.FocusReason.OtherFocusReason)

    def sizeHint(self):
        return QSize(800, 550)

    def current_changed(self):
        i = self.items.currentItem() or self.items.item(0)
        if i is not None:
            loc = i.data(Qt.ItemDataRole.UserRole)
            self.le.setText(loc)

    def browse(self):
        d = choose_dir(self, 'choose_library_for_copy',
                       _('Choose library'))
        if d:
            self.le.setText(d)

    @property
    def args(self):
        return (str(self.le.text()), self.delete_after_copy)
# }}}


class DuplicatesQuestion(QDialog):  # {{{

    def __init__(self, parent, duplicates, loc):
        QDialog.__init__(self, parent)
        l = QVBoxLayout()
        self.setLayout(l)
        self.la = la = QLabel(_('Books with the same, title, author and language as the following already exist in the library %s.'
                                ' Select which books you want copied anyway.') %
                              os.path.basename(loc))
        la.setWordWrap(True)
        l.addWidget(la)
        self.setWindowTitle(_('Duplicate books'))
        self.books = QListWidget(self)
        self.items = []
        for book_id, (title, authors) in iteritems(duplicates):
            i = QListWidgetItem(_('{0} by {1}').format(title, ' & '.join(authors[:3])), self.books)
            i.setData(Qt.ItemDataRole.UserRole, book_id)
            i.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            i.setCheckState(Qt.CheckState.Checked)
            self.items.append(i)
        l.addWidget(self.books)
        self.bb = bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        self.a = b = bb.addButton(_('Select &all'), QDialogButtonBox.ButtonRole.ActionRole)
        b.clicked.connect(self.select_all), b.setIcon(QIcon.ic('plus.png'))
        self.n = b = bb.addButton(_('Select &none'), QDialogButtonBox.ButtonRole.ActionRole)
        b.clicked.connect(self.select_none), b.setIcon(QIcon.ic('minus.png'))
        self.ctc = b = bb.addButton(_('&Copy to clipboard'), QDialogButtonBox.ButtonRole.ActionRole)
        b.clicked.connect(self.copy_to_clipboard), b.setIcon(QIcon.ic('edit-copy.png'))
        l.addWidget(bb)
        self.resize(600, 400)

    def copy_to_clipboard(self):
        items = [('✓' if item.checkState() == Qt.CheckState.Checked else '✗') + ' ' + str(item.text())
                 for item in self.items]
        QApplication.clipboard().setText('\n'.join(items))

    def select_all(self):
        for i in self.items:
            i.setCheckState(Qt.CheckState.Checked)

    def select_none(self):
        for i in self.items:
            i.setCheckState(Qt.CheckState.Unchecked)

    @property
    def ids(self):
        return {int(i.data(Qt.ItemDataRole.UserRole)) for i in self.items if i.checkState() == Qt.CheckState.Checked}
# }}}


# Static session-long set of pairs of libraries that have had their custom columns
# checked for compatibility
libraries_with_checked_columns = defaultdict(set)


class CopyToLibraryAction(InterfaceAction):

    name = 'Copy To Library'
    action_spec = (_('Copy to library'), 'copy-to-library.png',
            _('Copy selected books to the specified library'), None)
    popup_type = QToolButton.ToolButtonPopupMode.InstantPopup
    dont_add_to = frozenset(['context-menu-device'])
    action_type = 'current'
    action_add_menu = True

    def genesis(self):
        self.menu = self.qaction.menu()

    @property
    def stats(self):
        return self.gui.iactions['Choose Library'].stats

    def library_changed(self, db):
        self.build_menus()

    def initialization_complete(self):
        self.library_changed(self.gui.library_view.model().db)

    def location_selected(self, loc):
        enabled = loc == 'library'
        self.qaction.setEnabled(enabled)
        self.menuless_qaction.setEnabled(enabled)

    def build_menus(self):
        self.menu.clear()
        if os.environ.get('CALIBRE_OVERRIDE_DATABASE_PATH', None):
            self.menu.addAction('disabled', self.cannot_do_dialog)
            return
        db = self.gui.library_view.model().db
        locations = list(self.stats.locations(db))
        if len(locations) > 5:
            self.menu.addAction(_('Choose library...'), self.choose_library)
            self.menu.addSeparator()
        for name, loc in locations:
            ic = library_qicon(name)
            name = name.replace('&', '&&')
            self.menu.addAction(ic, name, partial(self.copy_to_library,
                loc))
            self.menu.addAction(ic, name + ' ' + _('(delete after copy)'),
                    partial(self.copy_to_library, loc, delete_after=True))
            self.menu.addSeparator()
        if len(locations) <= 5:
            self.menu.addAction(_('Choose library...'), self.choose_library)

        self.qaction.setVisible(bool(locations))
        if ismacos:
            # The cloned action has to have its menu updated
            self.qaction.changed.emit()

    def choose_library(self):
        db = self.gui.library_view.model().db
        locations = list(self.stats.locations(db))
        d = ChooseLibrary(self.gui, locations)
        if d.exec() == QDialog.DialogCode.Accepted:
            path, delete_after = d.args
            if not path:
                return
            db = self.gui.library_view.model().db
            current = os.path.normcase(os.path.abspath(db.library_path))
            if current == os.path.normcase(os.path.abspath(path)):
                return error_dialog(self.gui, _('Cannot copy'),
                    _('Cannot copy to current library.'), show=True)
            self.copy_to_library(path, delete_after)

    def _column_is_compatible(self, source_metadata, dest_metadata):
        return (source_metadata['datatype'] == dest_metadata['datatype'] and
                    (source_metadata['datatype'] != 'text' or
                     source_metadata['is_multiple'] == dest_metadata['is_multiple']))

    def copy_to_library(self, loc, delete_after=False):
        rows = self.gui.library_view.selectionModel().selectedRows()
        if not rows or len(rows) == 0:
            return error_dialog(self.gui, _('Cannot copy'),
                    _('No books selected'), show=True)
        ids = list(map(self.gui.library_view.model().id, rows))
        db = self.gui.library_view.model().db
        if not db.exists_at(loc):
            return error_dialog(self.gui, _('No library'),
                    _('No library found at %s')%loc, show=True)

        # Open the new db so we can check the custom columns. We use only the
        # backend since we only need the custom column definitions, not the
        # rest of the data in the db. We also do not want the user defined
        # formatter functions because loading them can poison the template cache
        global libraries_with_checked_columns

        from calibre.db.legacy import create_backend
        newdb = create_backend(loc, load_user_formatter_functions=False)

        continue_processing = True
        with closing(newdb):
            if newdb.library_id not in libraries_with_checked_columns[db.library_id]:

                newdb_meta = newdb.field_metadata.custom_field_metadata()
                incompatible_columns = []
                missing_columns = []
                for k, m in db.field_metadata.custom_iteritems():
                    if k not in newdb_meta:
                        missing_columns.append(k)
                    elif not self._column_is_compatible(m, newdb_meta[k]):
                        # Note that composite columns are always assumed to be
                        # compatible. No attempt is made to copy the template
                        # from the source to the destination.
                        incompatible_columns.append(k)

                if missing_columns or incompatible_columns:
                    continue_processing = ask_about_cc_mismatch(self.gui, db, newdb,
                                            missing_columns, incompatible_columns)
                if continue_processing:
                    libraries_with_checked_columns[db.library_id].add(newdb.library_id)

        newdb.close()
        del newdb
        if not continue_processing:
            return
        duplicate_ids = self.do_copy(ids, db, loc, delete_after, False)
        if duplicate_ids:
            d = DuplicatesQuestion(self.gui, duplicate_ids, loc)
            if d.exec() == QDialog.DialogCode.Accepted:
                ids = d.ids
                if ids:
                    self.do_copy(list(ids), db, loc, delete_after, add_duplicates=True)

    def do_copy(self, ids, db, loc, delete_after, add_duplicates=False):
        aname = _('Moving to') if delete_after else _('Copying to')
        dtitle = f'{aname} {os.path.basename(loc)}'
        self.pd = ProgressDialog(dtitle, min=0, max=len(ids)-1,
                parent=self.gui, cancelable=True, icon='lt.png', cancel_confirm_msg=_(
                    'Aborting this operation means that only some books will be copied'
                    ' and resuming a partial copy is not supported. Are you sure you want to abort?'))

        def progress(idx, title):
            self.pd.set_msg(title)
            self.pd.set_value(idx)

        self.worker = Worker(ids, db, loc, Dispatcher(progress),
                             Dispatcher(self.pd.accept), delete_after, add_duplicates)
        self.worker.start()
        self.pd.canceled_signal.connect(self.worker.cancel_processing)

        self.pd.exec()
        self.pd.canceled_signal.disconnect()

        if self.worker.left_after_cancel:
            msg = _('The copying process was interrupted. {} books were copied.').format(len(self.worker.processed))
            if delete_after:
                msg += ' ' + _('No books were deleted from this library.')
            msg += ' ' + _('The best way to resume this operation is to re-copy all the books with the option to'
                     ' "Check for duplicates when copying to library" in Preferences->Import/export->Adding books turned on.')
            warning_dialog(self.gui, _('Canceled'), msg, show=True)
            return

        if self.worker.error is not None:
            e, tb = self.worker.error
            error_dialog(self.gui, _('Failed'), _('Could not copy books: ') + e,
                    det_msg=tb, show=True)
            return

        if delete_after:
            donemsg = _('Moved the book to {loc}') if len(self.worker.processed) == 1 else _(
                'Moved {num} books to {loc}')
        else:
            donemsg = _('Copied the book to {loc}') if len(self.worker.processed) == 1 else _(
                'Copied {num} books to {loc}')

        self.gui.status_bar.show_message(donemsg.format(num=len(self.worker.processed), loc=loc), 2000)
        if self.worker.auto_merged_ids:
            books = '\n'.join(itervalues(self.worker.auto_merged_ids))
            info_dialog(self.gui, _('Auto merged'),
                    _('Some books were automatically merged into existing '
                        'records in the target library. Click "Show '
                        'details" to see which ones. This behavior is '
                        'controlled by the Auto-merge option in '
                        'Preferences->Import/export->Adding books->Adding actions.'), det_msg=books,
                    show=True)
        done_ids = frozenset(self.worker.processed) - frozenset(self.worker.duplicate_ids)
        if delete_after and done_ids:
            v = self.gui.library_view
            ci = v.currentIndex()
            row = None
            if ci.isValid():
                row = ci.row()

            v.model().delete_books_by_id(done_ids, permanent=True)
            self.gui.iactions['Remove Books'].library_ids_deleted(done_ids, row)

        if self.worker.failed_books:
            def fmt_err(book_id):
                err, tb = self.worker.failed_books[book_id]
                title = db.title(book_id, index_is_id=True)
                return _('Copying: {0} failed, with error:\n{1}').format(title, tb)
            title, msg = _('Failed to copy some books'), _('Could not copy some books, click "Show details" for more information.')
            tb = '\n\n'.join(map(fmt_err, self.worker.failed_books))
            tb = ngettext('Failed to copy a book, see below for details',
                          'Failed to copy {} books, see below for details', len(self.worker.failed_books)).format(
                len(self.worker.failed_books)) + '\n\n' + tb
            if len(ids) == len(self.worker.failed_books):
                title, msg = _('Failed to copy books'), _('Could not copy any books, click "Show details" for more information.')
            error_dialog(self.gui, title, msg, det_msg=tb, show=True)
        return self.worker.duplicate_ids

    def cannot_do_dialog(self):
        warning_dialog(self.gui, _('Not allowed'),
                    _('You cannot use other libraries while using the environment'
                      ' variable CALIBRE_OVERRIDE_DATABASE_PATH.'), show=True)
