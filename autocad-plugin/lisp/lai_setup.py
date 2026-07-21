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
# The .app bundle cannot host a tkinter window directly — macOS won't give it
# foreground focus.  Instead, write a .command file and open it in Terminal.
# Terminal opens a real window, activates as a foreground app, and tkinter
# can create its window normally.  This also gives the user a visible console.
cat > /tmp/lightingai_run.command << 'CMDEOF'
#!/bin/bash
printf "\\033]0;LightingAI Panel\\007"
cd "{home}/ai-lighting"
source venv/bin/activate 2>/dev/null || true
PYTHON=$(command -v python3.13 || command -v python3 || echo python3)
exec "$PYTHON" "{gui_py}"
CMDEOF
chmod +x /tmp/lightingai_run.command
open /tmp/lightingai_run.command
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

# ── LightingAI_Bridge.app — opens Terminal and runs /tmp/lai_bridge.sh ────────
bridge_app_dir = os.path.join(home, "LightingAI_Bridge.app")
for d in [f"{bridge_app_dir}/Contents/MacOS", f"{bridge_app_dir}/Contents/Resources"]:
    os.makedirs(d, exist_ok=True)

bridge_exec_src = """#!/bin/bash
cat > /tmp/lai_bridge_run.command << 'CMDEOF'
#!/bin/bash
printf "\\033]0;LightingAI Bridge\\007"
bash /tmp/lai_bridge.sh
CMDEOF
chmod +x /tmp/lai_bridge_run.command
open /tmp/lai_bridge_run.command
"""
bridge_exec_path = f"{bridge_app_dir}/Contents/MacOS/LightingAI_Bridge"
with open(bridge_exec_path, "w") as f:
    f.write(bridge_exec_src)
os.chmod(bridge_exec_path, 0o755)

bridge_plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>   <string>LightingAI_Bridge</string>
  <key>CFBundleIdentifier</key>   <string>com.maxfranke.lightingai.bridge</string>
  <key>CFBundleName</key>         <string>LightingAI_Bridge</string>
  <key>CFBundlePackageType</key>  <string>APPL</string>
  <key>CFBundleVersion</key>      <string>1.0</string>
  <key>LSBackgroundOnly</key>     <false/>
</dict>
</plist>
"""
with open(f"{bridge_app_dir}/Contents/Info.plist", "w") as f:
    f.write(bridge_plist)

print(f"Done. Created: {app_dir}")
print(f"Done. Created: {bridge_app_dir}")
print("Now reload LightingAI.lsp in AutoCAD (APPLOAD), then type LAI.")
