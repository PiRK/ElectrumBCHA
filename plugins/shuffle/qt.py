#!/usr/bin/env python
#
# Cash Shuffle - CoinJoin for Bitcoin Cash
# Copyright (C) 2018 Electron Cash LLC
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from __future__ import absolute_import

import os, sys, json, copy
from functools import partial

from PyQt5.QtGui import *
from PyQt5.QtCore import *
from PyQt5.QtWidgets import *

from electroncash.plugins import BasePlugin, hook
from electroncash.i18n import _
from electroncash.util import print_error, profiler, PrintError
from electroncash_gui.qt.util import EnterButton, Buttons, CloseButton, HelpLabel, OkButton, WindowModalDialog, rate_limited
from electroncash_gui.qt.password_dialog import PasswordDialog
from electroncash_gui.qt.main_window import ElectrumWindow
from electroncash.address import Address
from electroncash.bitcoin import COINBASE_MATURITY
from electroncash.transaction import Transaction
from electroncash_plugins.shuffle.client import BackgroundShufflingThread, ERR_SERVER_CONNECT
from electroncash_plugins.shuffle.comms import query_server_for_stats

FEE = 300
SCALE_0 = sorted(BackgroundShufflingThread.scales)[0]
SCALE_N = sorted(BackgroundShufflingThread.scales)[-1]
UPPER_BOUND = SCALE_N*10 + FEE
LOWER_BOUND = SCALE_0 + FEE

def is_coin_shuffled(wallet, coin, txs_in=None):
    cache = getattr(wallet, "_is_shuffled_cache", dict())
    tx_id, n = coin['prevout_hash'], coin['prevout_n']
    name = "{}:{}".format(tx_id, n)
    answer = cache.get(name, None)
    if answer is not None:
        # check cache, if cache hit, return answer and avoid the lookup below
        return answer
    def doChk():
        if txs_in:
            txs = txs_in
        else:
            with wallet.lock:
                with wallet.transaction_lock:
                    txs = wallet.transactions
        if tx_id in txs:
            tx = txs[tx_id]
            outputs = tx.outputs()
            inputs_len = len(tx.inputs())
            outputs_groups = {}
            for out_n, output in enumerate(outputs):
                amount = output[2]
                if outputs_groups.get(amount):
                    outputs_groups[amount].append(out_n)
                else:
                    outputs_groups[amount] = [out_n]
            for amount in outputs_groups:
                group_len = len(outputs_groups[amount])
                if group_len > 2 and amount in BackgroundShufflingThread.scales:
                    if n in outputs_groups[amount] and inputs_len >= group_len:
                        return True
            return False
        else:
            return None
    # /doChk
    answer = doChk()
    if answer is not None:
        # cache the answer iff it's a definitive answer True/False only
        cache[name] = answer
    return answer

def get_shuffled_coin_totals(wallet):
    tot = 0
    n = 0
    coins = wallet.get_shuffled_coins()
    for c in coins:
        tot += c['value']
        n += 1
    return tot, n


@profiler
def get_shuffled_coins(wallet, exclude_frozen = False, mature = False, confirmed_only = False):
    if not hasattr(wallet, 'is_coin_shuffled'):
        return []
    with wallet.lock:
        with wallet.transaction_lock:
            utxos = wallet.get_utxos(exclude_frozen = exclude_frozen, mature = mature, confirmed_only = confirmed_only)
            txs = wallet.transactions
    return [utxo for utxo in utxos if wallet.is_coin_shuffled(utxo, txs)]

def my_custom_item_setup(utxo_list, utxo, name, item):
    if not hasattr(utxo_list.wallet, 'is_coin_shuffled'):
        return

    prog = utxo_list.in_progress.get(name, "")
    frozenstring = item.data(0, Qt.UserRole+1) or ""

    if utxo_list.wallet.is_coin_shuffled(utxo):  # already shuffled
        item.setText(5, _("Shuffled"))
    elif not prog and ("a" in frozenstring or "c" in frozenstring):
        item.setText(5, _("Frozen"))
    elif utxo['height'] <= 0: # not_confirmed
        item.setText(5, _("Unconfirmed"))
    elif utxo['coinbase'] and (utxo['height'] + COINBASE_MATURITY > utxo_list.wallet.get_local_height()): # maturity check
        item.setText(5, _("Not mature"))
    elif utxo['value'] >= LOWER_BOUND and utxo['value'] < UPPER_BOUND: # queued_labels
        if utxo_list.wallet.network and utxo_list.wallet.network.is_connected():
            item.setText(5, _("In queue"))
        else:
            item.setText(5, _("Offline"))
    elif utxo['value'] >= UPPER_BOUND: # too big
        item.setText(5, _("Too big"))
    elif utxo['value'] < LOWER_BOUND: # dust
        item.setText(5, _("Too small"))

    if prog == 'in progress': # in progress
        item.setText(5, _("In progress"))
    elif prog.startswith('phase '):
        item.setText(5, _("Phase {}").format(prog.split()[-1]))
    elif prog == "wait for others": # wait for others
        item.setText(5, _("Wait for others"))
    elif prog == "completed":
        item.setText(5, _("Done"))

def update_coin_status(window, coin_name, msg):
    if getattr(window.utxo_list, "in_progress", None) is None:
        return
    #print_error("[shuffle] wallet={}; Coin {} Message '{}'".format(window.wallet.basename(), coin_name, msg.strip()))
    prev_in_progress = window.utxo_list.in_progress.get(coin_name)
    new_in_progress = prev_in_progress

    if coin_name not in ("MAINLOG", "PROTOCOL"):
        if msg.startswith("Player"):
            if "get session number" in msg:
                new_in_progress = 'wait for others'
            elif "begins CoinShuffle protocol" in msg:
                new_in_progress = 'in progress'
            elif "reaches phase" in msg:
                pos = msg.find("reaches phase")
                parts = msg[pos:].split(' ', 2)
                try:
                    phase = int(parts[2])
                    new_in_progress = 'phase {}'.format(phase)
                except (IndexError, ValueError):
                    pass
            elif msg.endswith("complete protocol"):
                new_in_progress = "completed"  # NB: this means we "leak" statuses as this final status never gets cleaned up. FIXME. there is a race condition anyway between code that picks up UTXOs for shuffling and the wallet code
        elif msg.startswith("Error"):
            new_in_progress = None # flag to remove from progress list
            if msg.find(ERR_SERVER_CONNECT) != -1:
                window.cashshuffle_set_flag(1) # 1 means server connection issue
        elif msg.startswith("Blame") and "insufficient" not in msg and "wrong hash" not in msg:
            new_in_progress = None
        elif msg.startswith("shuffle_txid:"): # TXID message -- call "set_label"
            words = msg.split()
            if len(words) >= 2:
                txid = words[1]
                window.wallet.set_label(txid, _("CashShuffle"))
                window.update_wallet()

        if not msg.startswith("Error") and not msg.startswith("Exit"):
            window.cashshuffle_set_flag(0) # 0 means ok

    else:
        if msg == "stopped":
            window.utxo_list.in_progress.clear(); new_in_progress = prev_in_progress = None
        elif msg.startswith("forget "):
            words = msg.strip().split()
            prev_in_progress = 1; new_in_progress = None; coin_name = words[-1] # force the code below to pop the coin that we were asked to forget from the status dict


    if prev_in_progress != new_in_progress:
        if new_in_progress is None:
            window.utxo_list.in_progress.pop(coin_name, None)
        else:
            window.utxo_list.in_progress[coin_name] = new_in_progress
            window.utxo_list.update()

class electrum_console_logger(QObject):

    gotMessage = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super(QObject, self).__init__(parent)
        self.parent = parent

    def send(self, msg, sender):
        self.gotMessage.emit(msg, sender)


def start_background_shuffling(window, network_settings, period = 10.0, password = None, timeout = 60.0):
    logger = electrum_console_logger()
    logger.gotMessage.connect(lambda msg, sender: update_coin_status(window, sender, msg))

    window.background_process = BackgroundShufflingThread(window,
                                                          window.wallet,
                                                          network_settings,
                                                          logger = logger,
                                                          fee = FEE,
                                                          period = period,
                                                          password = password,
                                                          timeout = timeout)
    window.background_process.start()

def monkey_patches_apply(window):
    def patch_window(window):
        if getattr(window, '_shuffle_patched_', None):
            return
        window.background_process = None
        window._shuffle_patched_ = True
        window.send_tab_shuffle_extra = SendTabExtra(window)
        print_error("[shuffle] Patched window")

    def patch_utxo_list(utxo_list):
        if getattr(utxo_list, '_shuffle_patched_', None):
            return
        header = utxo_list.headerItem()
        header_labels = [header.text(i) for i in range(header.columnCount())]
        header_labels.append(_("Shuffle status"))
        utxo_list.update_headers(header_labels)
        utxo_list.in_progress = dict()
        utxo_list._shuffle_patched_ = True
        print_error("[shuffle] Patched utxo_list")

    def patch_wallet(wallet):
        if getattr(wallet, '_shuffle_patched_', None):
            return
        wallet.is_coin_shuffled = lambda coin, txs=None: is_coin_shuffled(wallet, coin, txs)
        wallet.get_shuffled_coins = lambda *args, **kwargs: get_shuffled_coins(wallet, *args, **kwargs)
        wallet._is_shuffled_cache = dict()
        wallet._addresses_cashshuffle_reserved = set()
        unfreeze_frozen_by_shuffling(wallet)
        wallet._shuffle_patched_ = True
        print_error("[shuffle] Patched wallet")

    patch_wallet(window.wallet)
    patch_utxo_list(window.utxo_list)
    patch_window(window)

def monkey_patches_remove(window):
    def restore_window(window):
        if not getattr(window, '_shuffle_patched_', None):
            return
        window.send_tab_shuffle_extra.setParent(None); window.send_tab_shuffle_extra.deleteLater(); delattr(window, 'send_tab_shuffle_extra')
        delattr(window, 'background_process')
        delattr(window, '_shuffle_patched_')
        print_error("[shuffle] Unpatched window")

    def restore_utxo_list(utxo_list):
        if not getattr(utxo_list, '_shuffle_patched_', None):
            return
        header = utxo_list.headerItem()
        header_labels = [header.text(i) for i in range(header.columnCount())]
        del header_labels[-1]
        utxo_list.update_headers(header_labels)
        utxo_list.in_progress = None
        delattr(window.utxo_list, "in_progress")
        delattr(window.utxo_list, '_shuffle_patched_')
        print_error("[shuffle] Unpatched utxo_list")

    def restore_wallet(wallet):
        if not getattr(wallet, '_shuffle_patched_', None):
            return
        delattr(wallet, '_addresses_cashshuffle_reserved')
        delattr(wallet, "is_coin_shuffled")
        delattr(wallet, "get_shuffled_coins")
        delattr(wallet, "_is_shuffled_cache")
        delattr(wallet, '_shuffle_patched_')
        unfreeze_frozen_by_shuffling(wallet)
        print_error("[shuffle] Unpatched wallet")

    restore_window(window)
    restore_utxo_list(window.utxo_list)
    restore_wallet(window.wallet)

def unfreeze_frozen_by_shuffling(wallet):
    coins_frozen_by_shuffling = wallet.storage.get("coins_frozen_by_shuffling", list())
    if coins_frozen_by_shuffling:
        wallet.set_frozen_coin_state(coins_frozen_by_shuffling, False)
    wallet.storage.put("coins_frozen_by_shuffling", None) # deletes key altogether from storage



class Plugin(BasePlugin):

    def fullname(self):
        return 'CashShuffle'

    def description(self):
        return _("CashShuffle Protocol")

    def is_available(self):
        return True

    def __init__(self, parent, config, name):
        BasePlugin.__init__(self, parent, config, name)
        self.windows = []
        self.initted = False

    @hook
    def init_qt(self, gui):
        if self.initted:
            return
        self.print_error("Initializing...")
        ct = 0
        for window in gui.windows:
            self.on_new_window(window)
            ct += 1
        self.initted = True
        self.print_error("Initialized (had {} extant windows).".format(ct))

    def window_has_cashshuffle(self, window):
        return window in self.windows

    def window_wants_cashshuffle(self, window):
        return window.wallet.storage.get("cashshuffle_enabled", False)

    def window_set_wants_cashshuffle(self, window, b):
        window.wallet.storage.put("cashshuffle_enabled", bool(b))

    def window_set_cashshuffle(self, window, b):
        if not b and self.window_has_cashshuffle(window):
            self.on_close_window(window)
        elif b and not self.window_has_cashshuffle(window):
            self._enable_for_window(window)
        self.window_set_wants_cashshuffle(window, b)

    @hook
    def on_new_window(self, window):
        if window.wallet and not self.window_has_cashshuffle(window) and self.window_wants_cashshuffle(window):
            self._enable_for_window(window)

    def _enable_for_window(self, window):
        if not window.is_wallet_cashshuffle_compatible():
            # wallet is watching-only, multisig, or hardware so.. mark it permanently for no cashshuffle
            self.window_set_cashshuffle(window, False)
            return
        title = window.windowTitle() if window and window.windowTitle() else "UNKNOWN WINDOW"
        self.print_error("Window '{}' registered, performing window-specific startup code".format(title))
        password = None
        name = window.wallet.basename()
        while window.wallet.has_password():
            msg = _("CashShuffle requires access to '{}'.").format(name) + "\n" +  _('Please enter your password')
            dlgParent = None if sys.platform == 'darwin' else window
            password = PasswordDialog(parent=dlgParent, msg=msg).run()
            if password is None:
                # User cancelled password input
                self.window_set_cashshuffle(window, False)
                window.show_error(_("Can't get password, disabling for this wallet."), parent=window)
                return
            try:
                window.wallet.check_password(password)
                break
            except Exception as e:
                window.show_error(str(e), parent=window)
                continue
        network_settings = copy.deepcopy(window.config.get("cashshuffle_server_v1", None))
        if not network_settings:
            network_settings = self.settings_dialog(window, msg=_("Please choose a CashShuffle server"), restart_ask = False)
        if not network_settings:
            self.window_set_cashshuffle(window, False)
            window.show_error(_("Can't get network, disabling CashShuffle."), parent=window)
            return
        network_settings = copy.deepcopy(network_settings)
        network_settings['host'] = network_settings.pop('server')
        network_settings["network"] = window.network
        monkey_patches_apply(window)
        self.windows.append(window)
        window.update_status()
        window.utxo_list.update()
        start_background_shuffling(window, network_settings, password=password)

    @hook
    def utxo_list_item_setup(self, utxo_list, x, name, item):
        return my_custom_item_setup(utxo_list, x, name, item)


    def on_close(self):
        for window in self.windows.copy():
            self.on_close_window(window)
            window.update_status()
        self.initted = False
        self.print_error("Plugin closed")

    @hook
    def on_close_window(self, window):
        if window not in self.windows:
            return
        title = window.windowTitle() if window and window.windowTitle() else "UNKNOWN WINDOW"
        if window.background_process:
            self.print_error("Joining background_process...")
            window.background_process.join()
            window.background_process = None
            self.print_error("Window '{}' closed, ended shuffling for its wallet".format(title))
        self.windows.remove(window)
        monkey_patches_remove(window)
        window.utxo_list.update()
        window.update_status()
        self.print_error("Window '{}' removed".format(title))

    @hook
    def on_new_password(self, window, old, new):
        if getattr(window, 'background_process', None):
            self.print_error("Got new password for wallet {} informing background process...".format(window.wallet.basename() if window.wallet else 'UNKNOWN'))
            window.background_process.set_password(new)

    @hook
    def spendable_coin_filter(self, window, coins):
        if not coins or window not in self.windows:
            return
        # in Cash-Shuffle mode we can ONLY spend shuffled coins!
        for coin in coins.copy():
            if not is_coin_shuffled(window.wallet, coin):
                coins.remove(coin)

    @hook
    def balance_label_extra(self, window):
        if window not in self.windows:
            return
        tot, n = get_shuffled_coin_totals(window.wallet)
        window.send_tab_shuffle_extra.refresh(tot, n)
        if n:
            return _('Shuffled: {} {} in {} Coins').format(window.format_amount(tot).strip(), window.base_unit(), n)
        return None

    @hook
    def not_enough_funds_extra(self, window):
        if window not in self.windows:
            return
        tot, n = get_shuffled_coin_totals(window.wallet)
        window.send_tab_shuffle_extra.refresh(tot, n)
        if tot:
            c, u, x = window.wallet.get_balance()
            diff = (c+u+x) - tot
            if diff > 0:
                return _("{} {} are unshuffled").format(window.format_amount(diff).strip(), window.base_unit())
        return None

    def settings_dialog(self, window, msg=None, restart_ask = True):
        assert window and (isinstance(window, ElectrumWindow) or isinstance(window.parent(), ElectrumWindow))
        if not isinstance(window, ElectrumWindow):
            window = window.parent()

        d = SettingsDialog(None, _("CashShuffle Settings"), msg, window)

        server_ok = False
        ns = None
        while not server_ok:
            if not d.exec_():
                return
            else:
                ns = d.get_form()
                if not self.check_server_connectivity(ns.get("server"), ns.get("info"), ns.get("ssl")):
                    server_ok = bool(QMessageBox.critical(None, _("Error"), _("Unable to connect to the specified server."), QMessageBox.Retry|QMessageBox.Ignore, QMessageBox.Retry) == QMessageBox.Ignore)
                else:
                    server_ok = True
        if ns:
            self.save_network_settings(window, ns)
            if restart_ask: #and ns != selected:
                window.restart_cashshuffle(msg = _("CashShuffle must be restarted for the server change to take effect."))
        d.deleteLater()
        return ns

    def check_server_connectivity(self, host, stat_port, ssl):
        try:
            import socket
            try:
                prog = QProgressDialog(_("Checking server..."), None, 0, 3, None)
                prog.setWindowModality(Qt.ApplicationModal); prog.setMinimumDuration(0); prog.setValue(1)
                QApplication.instance().processEvents(QEventLoop.ExcludeUserInputEvents|QEventLoop.ExcludeSocketNotifiers, 1) # this forces the window to be shown
                port, poolSize, connections, pools = query_server_for_stats(host, stat_port, ssl)
                prog.setValue(2)
                self.print_error("{}:{}{} got response: shufflePort = {} poolSize = {} connections = {} pools = {}".format(host, stat_port, 's' if ssl else '', port, poolSize, connections, pools))
                socket.create_connection((host, port), 3.0).close() # test connectivity to port
                prog.setValue(3)
            finally:
                prog.close(); prog.deleteLater()
            return True
        except:
            self.print_error("Connectivity test got exception: {}".format(str(sys.exc_info()[1])))
        return False

    def save_network_settings(self, window, network_settings):
        ns = copy.deepcopy(network_settings)
        self.print_error("Saving network settings: {}".format(ns))
        window.config.set_key("cashshuffle_server_v1", ns)


    def settings_widget(self, window):
        return EnterButton(_('Settings'), partial(self.settings_dialog, window))

    def requires_settings(self):
        return True


class SendTabExtra(QFrame):
    ''' Implements a Widget that appears in the main_window 'send tab' to inform the user of shuffled coin status & totals '''

    def __init__(self, window):
        self.send_tab = window.send_tab
        self.send_grid = window.send_grid
        self.wallet = window.wallet
        self.window = window
        super().__init__(window.send_tab)
        self.send_grid.addWidget(self, 0, 0, 1, self.send_grid.columnCount()) # just our luck. row 0 is free!
        self.setup()

    def setup(self):
        self.setFrameStyle(QFrame.Panel|QFrame.Sunken)
        l = QGridLayout(self)
        l.setVerticalSpacing(12)
        l.setHorizontalSpacing(30)
        l.setContentsMargins(6, 12, 6, 12)
        msg = "{}\n\n{}\n\n{}".format(_("In order to protect your privacy, when CashShuffle is enabled, only shuffled coins can be sent."),
                                      _("If insufficient shuffled funds are available, you can wait a few minutes as coins are shuffled in the background."),
                                      _("To toggle CashShuffle off, use the CashSuffle icon in the status bar."))
        titleLabel = HelpLabel("<big><b>{}</b></big> <i>{}</i>"
                              .format(_("CashShuffle Enabled"),
                                      _("Only shuffled funds may be sent")), msg)
        l.addWidget(titleLabel, 0, 1, 1, 3)
        l.addWidget(HelpLabel("Shuffled funds available:", msg), 1, 1)
        self.amountLabel = QLabel("")
        l.addWidget(self.amountLabel, 1, 2)
        self.numCoinsLabel = QLabel("")
        l.addWidget(self.numCoinsLabel, 1, 3)
        l.setAlignment(titleLabel, Qt.AlignLeft)
        l.setAlignment(self.numCoinsLabel, Qt.AlignLeft)
        l.addItem(QSpacerItem(1, 1, QSizePolicy.MinimumExpanding, QSizePolicy.Fixed), 1, 4)


        icon = QLabel()
        icon.setPixmap(QPixmap(":/icons/cash_shuffle5.png").scaledToWidth(100,Qt.SmoothTransformation))
        l.addWidget(icon, 0, 0, l.rowCount(), 1)

        l.setSizeConstraint(QLayout.SetNoConstraint)

        self.window.history_updated_signal.connect(self.refresh)

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()

    def refresh(self, amount = None, n = None):
        if amount is None or n is None:
            amount, n = get_shuffled_coin_totals(self.wallet)
        self.amountLabel.setText("<b>{}</b> {}".format(self.window.format_amount(amount).strip(), self.window.base_unit()))
        self.numCoinsLabel.setText(_("<b>{}</b> Coins <small>(UTXOs)</small>").format(n))


class SettingsDialog(WindowModalDialog, PrintError):
    def __init__(self, parent, title,
                 message, window):
        super().__init__(parent, title)
        self.setWindowModality(Qt.ApplicationModal)
        self.setMinimumSize(500, 200)
        self.setup(message, window)
    def showEvent(self, e):
        super().showEvent(e)
    def hideEvent(self, e):
        super().hideEvent(e)
    def closeEvent(self, e):
        super().closeEvent(e)
    def from_combobox(self):
        d = self.cb.currentData()
        if isinstance(d, dict):
            host, info, ssl = d.get('server'), d.get('info'), d.get('ssl')
            self.le.setText(host)
            self.sb.setValue(info)
            self.chk.setChecked(ssl)
        en = self.cb.currentIndex() == self.cb.count()-1
        self.le.setEnabled(en); self.sb.setEnabled(en); self.chk.setEnabled(en)
    def get_form(self):
        return {
            'server': self.le.text(),
            'info'  : self.sb.value(),
            'ssl'   : self.chk.isChecked()
        }
    def setup_combo_box(self, selected = {}):
        def load_servers(fname):
            r = {}
            try:
                zips = __file__.find(".zip")
                if zips == -1:
                    with open(os.path.join(os.path.dirname(__file__), fname), 'r') as f:
                        r = json.loads(f.read())
                else:
                    from zipfile import ZipFile
                    zip_file = ZipFile(__file__[: zips + 4])
                    with zip_file.open("shuffle/" + fname) as f:
                        r = json.loads(f.read().decode())
            except:
                self.print_error("Error loading server list from {}: {}", fname, str(sys.exc_info()[1]))
            return r
        # /
        servers = load_servers("servers.json")
        selIdx = -1
        for host, d0 in sorted(servers.items()):
            d = d0.copy()
            d['server'] = host
            item = host + (' [ssl]' if d['ssl'] else '')
            self.cb.addItem(item, d)
            if selected and selected == d:
                selIdx = self.cb.count()-1

        self.cb.addItem(_("(Custom)"))
        if selIdx > -1:
            self.cb.setCurrentIndex(selIdx)
        elif selected and len(selected) == 3:
            custIdx = self.cb.count()-1
            self.cb.setItemData(custIdx, selected.copy())
            self.cb.setCurrentIndex(custIdx)
            return True
        return False
    def setup(self, msg, window):
        vbox = QVBoxLayout(self)
        if not msg:
            msg = _("Choose a CashShuffle server from the list.\nChanges will require the CashShuffle plugin to restart.")
        vbox.addWidget(QLabel(msg))
        grid = QGridLayout()
        vbox.addLayout(grid)

        self.cb = QComboBox(self)
        selected = dict()
        try:
            # try and pre-populate from config
            current = window.config.get("cashshuffle_server_v1", dict())
            dummy = (current["server"], current["info"], current["ssl"]); del dummy;
            selected = current
        except KeyError:
            pass

        self.setup_combo_box(selected = selected)

        grid.addWidget(QLabel(_('Servers'), self), 0, 0)
        grid.addWidget(self.cb, 0, 1)

        grid.addWidget(QLabel(_("Host"), self), 1, 0)

        hbox = QHBoxLayout(); grid.addLayout(hbox, 1, 1, 1, 2); grid.setColumnStretch(2, 1)
        self.le = QLineEdit(self); hbox.addWidget(self.le)
        hbox.addWidget(QLabel(_("P:")))
        self.sb = QSpinBox(self); self.sb.setRange(1, 65535); hbox.addWidget(self.sb)
        self.chk = QCheckBox(_("SSL"), self); hbox.addWidget(self.chk)

        self.cb.currentIndexChanged.connect(lambda x='ignored': self.from_combobox())
        self.from_combobox()

        vbox.addStretch()
        vbox.addLayout(Buttons(CloseButton(self), OkButton(self)))
    # /
# /SettingsDialog

