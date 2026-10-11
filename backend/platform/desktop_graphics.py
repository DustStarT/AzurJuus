"""Choose the desktop graphics path before Qt/WebEngine initialization."""
import os
import re


def configure_desktop_graphics(environ=None, platform=None):
    env=os.environ if environ is None else environ
    platform=os.name if platform is None else platform
    mode=env.get('AZURJUUS_DESKTOP_RENDERER','software' if platform=='nt' else 'auto').strip().lower()
    if mode not in {'software','auto'}:
        raise ValueError('AZURJUUS_DESKTOP_RENDERER 必须为 software 或 auto。')
    if mode=='software':
        # Windows can show corrupted shared textures instead of reporting a GPU
        # failure. Switch both compositors; keep glass styling and other flags.
        flags=env.get('QTWEBENGINE_CHROMIUM_FLAGS','')
        flags=re.sub(r'(?<!\S)--disable-gpu(?:=[^\s]+)?(?=\s|$)','',flags).strip()
        env['QTWEBENGINE_CHROMIUM_FLAGS']=(flags+' --disable-gpu').strip()
        env['QT_QUICK_BACKEND']='software'
    return mode
