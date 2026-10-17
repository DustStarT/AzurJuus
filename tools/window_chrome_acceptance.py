"""Check native chrome against real web dialogs in an isolated hidden Qt window."""
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
home = Path(tempfile.mkdtemp(prefix='azur-chrome-ui-'))
os.environ.update(AZURJUUS_DATABASE_URL='sqlite+pysqlite:///' + (home / 'business.db').as_posix(),
    AZURJUUS_WORKSPACE_STATE_PATH=str(home / 'state/workspace.json'),
    AZURJUUS_WORKSPACE_ROOT=str(home / 'work'), AZURJUUS_REDIS_URL='', AZURJUUS_CHROMA_URL='',
    AZURJUUS_SOCIAL_ENABLED='0', AZURJUUS_MIND_ENABLED='0', AZURJUUS_REFLECTION_ENABLED='0',
    AZURJUUS_SKILL_TRIALS_ENABLED='0')

import desktop
from PySide6.QtCore import QPoint, Qt, QTimer


def main():
    out = ROOT / 'validation/window-chrome'
    out.mkdir(parents=True, exist_ok=True)
    app = desktop.create_application()
    server, thread, url, context = desktop.start_local_server(port=0)
    window = desktop.AzurJuusWindow(url, server, thread, context)
    window.setAttribute(Qt.WA_DontShowOnScreen, True)
    window.show()
    report = {'status': 'failed', 'checks': []}
    sizes = [(820, 560), (1024, 680), (1280, 800)]
    finished = False

    def finish(error=''):
        nonlocal finished
        if finished:
            return
        finished = True
        if error:
            report['error'] = error
        else:
            report['status'] = 'passed'
        (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False), flush=True)
        window.close()
        QTimer.singleShot(2500, app.quit)

    def inspect(index, dialog, attempt=0):
        if finished:
            return
        label = '关闭任务详情' if dialog == 'work' else '关闭设置'
        script = """(()=>{const e=document.querySelector('[aria-label="LABEL"]');
            if(!e)return '';const r=e.getBoundingClientRect();return JSON.stringify({top:r.top,height:r.height});})()""".replace('LABEL', label)

        def checked(raw):
            if finished:
                return
            if not raw:
                if attempt < 30:
                    QTimer.singleShot(200, lambda: inspect(index, dialog, attempt + 1))
                else:
                    finish('Dialog was not rendered: ' + dialog)
                return
            try:
                rect = json.loads(raw)
                view_top = window._view.mapTo(window, QPoint(0, 0)).y()
                controls_bottom = window._controls.geometry().bottom()
                assert view_top == 0, 'The web surface must not reserve a blank native title strip'
                assert rect['height'] > 0
                assert not window._controls.isVisible(), 'Native controls must yield to the web overlay'
                assert not window._drag_strip.isVisible(), 'Native drag strip must not intercept dialog clicks'
                report['checks'].append({'size': sizes[index], 'dialog': dialog,
                    'nativeControlsBottom': controls_bottom, 'webViewTop': view_top,
                    'dialogCloseTop': rect['top'] + view_top, 'nativeControlsVisible': window._controls.isVisible()})
                if dialog == 'work':
                    window._page.runJavaScript("document.querySelector('[aria-label=\"关闭任务详情\"]').click();document.querySelector('[aria-label=\"打开设置\"]').click();")
                    QTimer.singleShot(200, lambda: inspect(index, 'settings'))
                elif index + 1 < len(sizes):
                    window._page.runJavaScript("document.querySelector('[aria-label=\"关闭设置\"]').click();")
                    QTimer.singleShot(100, lambda: start(index + 1))
                else:
                    window._page.runJavaScript("document.querySelector('[aria-label=\"关闭设置\"]').click();")
                    QTimer.singleShot(600, restored)
            except Exception as exc:
                finish(str(exc) or type(exc).__name__)
        window._page.runJavaScript(script, checked)

    def restored():
        if window._controls.isVisible() and window._drag_strip.isVisible():
            report['checks'].append('native controls restored after overlay closes')
            finish()
        else:
            finish('Native controls did not return after overlay closes')

    def start(index=0, attempt=0):
        if finished:
            return
        window.resize(*sizes[index])
        def opened(ready):
            if ready:
                QTimer.singleShot(200, lambda: inspect(index, 'work'))
            elif attempt < 60:
                QTimer.singleShot(250, lambda: start(index, attempt + 1))
            else:
                finish('Workspace was not rendered')
        window._page.runJavaScript("(()=>{const b=document.querySelector('[aria-label=\"打开任务中心\"]');b?.click();return !!b;})()", opened)

    QTimer.singleShot(1000, start)
    QTimer.singleShot(45000, lambda: finish('Timed out'))
    app.exec()
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
