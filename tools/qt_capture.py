"""Capture only an unobscured fixture window from the composed desktop."""
import os


def capture_window(window, target):
    unobscured = True
    if os.name == 'nt':
        import win32gui
        handle = int(window.winId())
        left, top, right, bottom = win32gui.GetWindowRect(handle)
        unobscured = all(win32gui.GetAncestor(win32gui.WindowFromPoint(
            (int(left + (right-left)*x), int(top + (bottom-top)*y))), 2) == handle
            for x in (.15, .5, .85) for y in (.15, .5, .85))
    if not unobscured:
        return {'captureUnobscured': False, 'captureSaved': False}
    screen = window.screen()
    area = window.frameGeometry()
    origin = screen.geometry().topLeft()
    # A HWND capture may return a stale WebEngine texture. Capture the window's
    # desktop rectangle after checking ownership; no unrelated area is saved.
    pixels = screen.grabWindow(0, area.x()-origin.x(), area.y()-origin.y(),
        area.width(), area.height())
    return {'captureUnobscured': True, 'captureSaved': pixels.save(str(target))}
