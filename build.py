"""Build a Windows portable executable from this checkout."""
import os
from pathlib import Path
import subprocess
import sys

def main():
    if os.name != 'nt':
        raise SystemExit('Build this desktop application on Windows.')
    root=Path(__file__).resolve().parent
    sys.path.insert(0,str(root/'source'))
    from app import tray_image
    build=root/'build'
    build.mkdir(exist_ok=True)
    icon=build/'usage-panel.ico'
    tray_image().save(icon,sizes=[(16,16),(32,32),(48,48),(64,64)])
    subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--onefile','--windowed',
        '--name','UsagePanel','--icon',str(icon),'--distpath',str(root/'dist'),
        '--workpath',str(build/'pyinstaller'),'--specpath',str(build),str(root/'source/app.py')],
        cwd=root,check=True)
    print(root/'dist/UsagePanel.exe')

if __name__=='__main__': main()
