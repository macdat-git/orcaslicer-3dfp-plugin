"""Embed assets/icon_64.png into threedfp_profiles.py as LOGO_PNG_B64 (the panel header logo).

    python tools/embed_logo.py

Replaces everything between the "# LOGO-BEGIN" and "# LOGO-END" markers in the plugin file.
"""
import base64
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "threedfp_profiles.py")
ICON = os.path.join(ROOT, "assets", "icon_64.png")

with open(ICON, "rb") as f:
    b64 = base64.b64encode(f.read()).decode("ascii")
lines = [b64[i:i + 100] for i in range(0, len(b64), 100)]
block = "# LOGO-BEGIN\nLOGO_PNG_B64 = (\n" + "".join('    "%s"\n' % ln for ln in lines) + ")\n# LOGO-END"

with open(PLUGIN, encoding="utf-8") as f:
    text = f.read()
new, n = re.subn(r"# LOGO-BEGIN\n.*?# LOGO-END", lambda m: block, text, count=1, flags=re.S)
if n != 1:
    raise SystemExit("LOGO-BEGIN/LOGO-END markers not found in " + PLUGIN)
with open(PLUGIN, "w", encoding="utf-8", newline="\n") as f:
    f.write(new)
print("embedded %s (%d bytes, %d base64 chars) into %s" % (os.path.basename(ICON), os.path.getsize(ICON), len(b64),
                                                            os.path.basename(PLUGIN)))
