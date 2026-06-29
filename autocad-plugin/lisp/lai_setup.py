#!/usr/bin/env python3
"""
lai_setup.py — one-time setup: builds ~/LightingAI.app

Run once from Terminal:
    python3 ~/ai-lighting/autocad-plugin/lisp/lai_setup.py

The resulting ~/LightingAI.app is a proper macOS .app bundle that
AutoCAD's (startapp ...) can launch. It opens the LightingAI control panel.
"""
import os, stat

home    = os.path.expanduser("~")
app_dir = os.path.join(home, "LightingAI.app")
gui_py  = os.path.join(home, "ai-lighting", "autocad-plugin", "lisp", "lai_gui.py")

for d in [f"{app_dir}/Contents/MacOS", f"{app_dir}/Contents/Resources"]:
    os.makedirs(d, exist_ok=True)

exec_src = f"""#!/bin/bash
cd "{home}/ai-lighting"
source venv/bin/activate 2>/dev/null || true
exec python3 "{gui_py}" > /tmp/lai_gui.log 2>&1
"""
exec_path = f"{app_dir}/Contents/MacOS/LightingAI"
with open(exec_path, "w") as f:
    f.write(exec_src)
os.chmod(exec_path, 0o755)

plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>   <string>LightingAI</string>
  <key>CFBundleIdentifier</key>   <string>com.maxfranke.lightingai.panel</string>
  <key>CFBundleName</key>         <string>LightingAI</string>
  <key>CFBundlePackageType</key>  <string>APPL</string>
  <key>CFBundleVersion</key>      <string>1.0</string>
  <key>LSBackgroundOnly</key>     <false/>
  <key>NSHighResolutionCapable</key> <true/>
</dict>
</plist>
"""
with open(f"{app_dir}/Contents/Info.plist", "w") as f:
    f.write(plist)

print(f"Done. Created: {app_dir}")
print("Now reload LightingAI.lsp in AutoCAD (APPLOAD), then type LAI.")
