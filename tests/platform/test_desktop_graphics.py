import pytest
from backend.platform.desktop_graphics import configure_desktop_graphics


def test_windows_desktop_uses_both_software_compositors_and_keeps_unrelated_flags():
    env={'QTWEBENGINE_CHROMIUM_FLAGS':'--remote-debugging-port=8877 --disable-gpu=false',
         'QT_QUICK_BACKEND':'opengl'}
    assert configure_desktop_graphics(env,'nt')=='software'
    assert env['QT_QUICK_BACKEND']=='software'
    assert env['QTWEBENGINE_CHROMIUM_FLAGS']=='--remote-debugging-port=8877 --disable-gpu'
    configure_desktop_graphics(env,'nt')
    assert env['QTWEBENGINE_CHROMIUM_FLAGS'].count('--disable-gpu')==1
    assert '--no-sandbox' not in env['QTWEBENGINE_CHROMIUM_FLAGS']


def test_auto_and_non_windows_defaults_preserve_external_graphics_configuration():
    env={'AZURJUUS_DESKTOP_RENDERER':'auto','QTWEBENGINE_CHROMIUM_FLAGS':'--use-angle=d3d11'}
    before=dict(env)
    assert configure_desktop_graphics(env,'nt')=='auto' and env==before
    env={}
    assert configure_desktop_graphics(env,'posix')=='auto' and env=={}


def test_explicit_software_mode_works_outside_windows_and_rejects_unknown_modes():
    env={'AZURJUUS_DESKTOP_RENDERER':' SOFTWARE '}
    assert configure_desktop_graphics(env,'posix')=='software'
    assert env['QTWEBENGINE_CHROMIUM_FLAGS']=='--disable-gpu'
    with pytest.raises(ValueError):configure_desktop_graphics({'AZURJUUS_DESKTOP_RENDERER':'unknown'},'nt')
