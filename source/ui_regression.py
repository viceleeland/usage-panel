"""Opt-in Windows/Tk regression: python source/ui_regression.py.

Runs a disposable child process with synthetic providers and no system tray or
account access. Kept separate from the standard-library provider test suite.
"""
import argparse
import gc
import pathlib
import subprocess
import sys
import threading
import time
import weakref


def shutdown_with_pending_workers():
    import app

    release = threading.Event()
    account_started = threading.Event()
    tokens_started = threading.Event()
    account_finished = threading.Event()
    tokens_finished = threading.Event()
    failures = []

    class FakeTray:
        def __init__(self, *args):
            self.menu = args[-1]
            self.visible = True

        def run_detached(self):
            pass

        def stop(self):
            pass

    def collect():
        account_started.set()
        release.wait(15)
        account_finished.set()
        return {}

    class FakeTokens:
        def read(self):
            tokens_started.set()
            release.wait(15)
            tokens_finished.set()
            return {'date': '2099-01-01'}

    original_del = app.tk.Variable.__del__

    def variable_del(variable):
        if threading.current_thread() is not threading.main_thread():
            failures.append('Tk variable finalized by a background worker')
        original_del(variable)

    app.tk.Variable.__del__ = variable_del
    app.pystray.Icon = FakeTray
    app.DailyTokens = FakeTokens
    app.collect = collect
    app.read_json = lambda path: {}
    app.UsagePanel.save_cache = lambda self: None
    args = argparse.Namespace(smoke=False, hidden=True)
    panel = app.UsagePanel(args)
    reference = weakref.ref(panel)
    deadline = time.monotonic() + 5

    def close_when_started():
        current = reference()
        if account_started.is_set() and tokens_started.is_set():
            current.quit()
        elif time.monotonic() >= deadline:
            failures.append('Workers did not start')
            current.quit()
        else:
            current.root.after(25, close_when_started)

    panel.root.after(25, close_when_started)
    panel.root.mainloop()
    # Match main(): collect retired callbacks, then release the panel on Tk's
    # owning thread while the independent provider jobs are still blocked.
    panel.quit()
    gc.collect()
    panel = None
    gc.collect()
    if reference() is not None:
        failures.append('A pending worker or tray callback still retains the UI')
    release.set()
    account_finished.wait(5)
    tokens_finished.wait(5)
    time.sleep(0.1)
    if not account_finished.is_set() or not tokens_finished.is_set():
        failures.append('Synthetic workers did not finish')
    if failures:
        raise AssertionError('; '.join(failures))
    print('PASS: UI released before pending workers finish; no Tk worker cleanup')


def closed_dialogs_with_background_gc():
    import app

    failures = []
    finalized = []
    original_del = app.tk.Variable.__del__

    def variable_del(variable):
        finalized.append(threading.current_thread() is threading.main_thread())
        original_del(variable)

    app.tk.Variable.__del__ = variable_del
    panel = app.UsagePanel.__new__(app.UsagePanel)
    panel.root = app.tk.Tk()
    panel.root.withdraw()
    panel.tokens = {}
    panel.result = {}
    panel.trend_window = None
    rounds = [0]

    def on_error(kind, value, traceback):
        failures.append(f'{kind.__name__}: {value}')
        panel.root.destroy()

    panel.root.report_callback_exception = on_error

    def iteration():
        before = set(panel.root.tk.call('after', 'info'))
        details = panel.usage_details()
        trend = panel.show_trends()
        details.destroy()
        trend.destroy()
        if set(panel.root.tk.call('after', 'info')) != before:
            failures.append('Closed dialogs retained scheduled refresh callbacks')
        completed = threading.Event()

        def collect():
            gc.collect()
            completed.set()

        threading.Thread(target=collect, daemon=True).start()
        deadline = time.monotonic() + 5

        def wait_for_gc():
            if not completed.is_set() and time.monotonic() < deadline:
                panel.root.after(10, wait_for_gc)
                return
            if not completed.is_set():
                failures.append('Background collection did not finish')
            rounds[0] += 1
            if rounds[0] < 20 and not failures:
                panel.root.after(1, iteration)
            else:
                panel.root.destroy()

        panel.root.after(10, wait_for_gc)

    panel.root.after(1, iteration)
    panel.root.mainloop()
    gc.collect()
    if len(finalized) != 80 or not all(finalized):
        failures.append(f'Expected 80 variables released on UI thread; '
                        f'got {len(finalized)}, {finalized.count(False)} off-thread')
    if failures:
        raise AssertionError('; '.join(failures))
    print('PASS: 20 detail/trend close cycles cancel timers and finalize 80 Tk '
          'variables on the UI thread, even with background collection')


def shutdown_with_open_dialogs():
    import app

    class FakeTray:
        stopped = False

        def stop(self):
            self.stopped = True

    panel = app.UsagePanel.__new__(app.UsagePanel)
    panel.root = app.tk.Tk()
    panel.root.withdraw()
    panel.closed = False
    panel.tokens = {}
    panel.result = {}
    panel.trend_window = None
    panel.tray = FakeTray()
    details = panel.usage_details()
    trend = panel.show_trends()
    panel.root.after(60000, lambda: None)
    panel.quit()
    assert panel.tray.stopped
    assert not panel.root.tk.call('after', 'info'), 'Shutdown retained timers'
    assert not details._tclCommands and not trend._tclCommands
    print('PASS: quitting with detail/trend windows open cleans their Tcl callbacks')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child', choices=['shutdown', 'dialogs', 'open-dialogs'])
    args = parser.parse_args()
    if args.child:
        if args.child == 'shutdown':
            shutdown_with_pending_workers()
        elif args.child == 'open-dialogs':
            shutdown_with_open_dialogs()
        else:
            closed_dialogs_with_background_gc()
        return
    for scenario in ('shutdown', 'dialogs', 'open-dialogs'):
        result = subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve()),
                                 '--child', scenario], capture_output=True, text=True, timeout=30)
        print(result.stdout, end='')
        if result.returncode or result.stderr:
            print(result.stderr, file=sys.stderr, end='')
            raise SystemExit(result.returncode or 1)


if __name__ == '__main__':
    main()
