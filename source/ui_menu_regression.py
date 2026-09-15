"""Opt-in Windows desktop regression for the native score menu.

Run ``python source/ui_menu_regression.py --count 30`` from the project.
This opens its own test window and uses synthetic data. It neither reads account
credentials nor writes application settings, and sends no global input.
It is separate from unittest discovery because it requires an interactive desktop.
"""
import argparse
import ctypes
from ctypes import wintypes
import faulthandler
import json
import sys
import threading
import time
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count', type=int, default=30)
    args = parser.parse_args()
    if sys.platform != 'win32':
        print('SKIP: native Windows menu regression requires Windows.')
        return 0
    if args.count < 3:
        parser.error('--count must be at least 3')
    faulthandler.enable()

    import app

    class FakeTray:
        def __init__(self, *args, **kwargs):
            pass

        def run_detached(self):
            pass

        def stop(self):
            pass

    app.pystray.Icon = FakeTray
    app.read_json = lambda _: {}
    app.DailyTokens = lambda: None
    app.psutil.process_iter = lambda *_: []
    app.psutil.virtual_memory = lambda: SimpleNamespace(
        used=8 * 2**30, total=16 * 2**30, percent=50)
    app.UsagePanel.refresh = lambda *_: None
    app.UsagePanel.refresh_tokens = lambda *_: None
    app.UsagePanel.save_settings = lambda *_: None
    app.UsagePanel.save_cache = lambda *_: None
    app.UsagePanel.process_alerts = lambda *_: None

    panel = app.UsagePanel(SimpleNamespace(smoke=False, hidden=False))
    modes = ['综合智能', '软件工程', '视觉空间']
    tables = {mode: {name: [100 * (index + 1) + col for col in range(6)]
                     for name in app.DEFAULT_SCORES}
              for index, mode in enumerate(modes)}
    panel.result = {'radar': {'ok': True, 'tables': tables,
                              'note': 'Synthetic menu regression data'}}
    panel.draw()
    root = panel.root
    root.title('UsagePanel native menu regression (synthetic data)')
    root.geometry('+20+20')
    root.update_idletasks()
    selector = panel.radar_selector
    labels = dict(panel.score_labels)
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    owner = user32.GetParent(root.winfo_id())
    completed = 0
    native_commands = 0
    choices = set()
    errors = []
    original_change_mode = panel.change_mode

    def change_mode():
        nonlocal native_commands
        original_change_mode()
        native_commands += 1
        choices.add(panel.radar_mode.get())

    panel.change_mode = change_mode

    def fail(exc_type, value, tb):
        errors.append(f'{exc_type.__name__}: {value}')
        panel.quit()

    root.report_callback_exception = fail

    def action():
        nonlocal completed
        if completed >= args.count:
            assert choices == set(modes), f'Menu choices missing: {choices}'
            panel.quit()
            return
        menu = selector['menu']
        closed = threading.Event()
        root.focus_force()

        def choose_native_item():
            # Target only this process's test window; never send global keys.
            time.sleep(.03)
            for key in [0x28] * (completed % 3 + 1) + [0x0D]:
                user32.PostMessageW(owner, 0x100, key, 0)  # WM_KEYDOWN
                user32.PostMessageW(owner, 0x101, key, 0)  # WM_KEYUP
                time.sleep(.005)
            if not closed.wait(1):
                user32.PostMessageW(owner, 0x1F, 0, 0)  # WM_CANCELMODE

        threading.Thread(target=choose_native_item, daemon=True).start()
        try:
            menu.post(selector.winfo_rootx(),
                      selector.winfo_rooty() + selector.winfo_height())
        finally:
            closed.set()
        # Windows dispatches the menu command after TrackPopupMenu returns.
        root.after(10, verify_choice)

    def verify_choice():
        nonlocal completed
        expected = panel.radar_mode.get()
        for key, label in labels.items():
            name, column = key
            assert str(label.cget('text')) == str(tables[expected][name][column]), 'Score label did not update'
        # Refreshing account data must retain the native menu and score labels.
        panel.draw()
        assert panel.radar_selector is selector and selector.winfo_exists()
        assert all(panel.score_labels[key] is label and label.winfo_exists()
                   for key, label in labels.items())
        completed += 1
        if completed % 10 == 0:
            print(f'Native menu rounds and refreshes passed: {completed}', flush=True)
        root.after(5, action)

    root.after(200, action)
    root.mainloop()
    report = {'success': completed == args.count and not errors,
              'menu_rounds': completed, 'native_commands': native_commands,
              'account_redraws': completed,
              'modes_verified': len(choices), 'errors': errors}
    print(json.dumps(report), flush=True)
    return 0 if report['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
