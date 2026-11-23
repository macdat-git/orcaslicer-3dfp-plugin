"""Write the Orca Cloud upload file(s): the plugin renamed with a target-OS suffix.

    python tools/make_upload_files.py                 # dist/threedfp_profiles_any.py  (what to upload)
    python tools/make_upload_files.py win_x86_64 ...  # specific targets instead

Orca Cloud requires the file name to end with a supported target right before .py / .whl
("Plugin filenames must end with a supported OS and arch target before .whl or .py, like my_plugin_win_x86_64.whl").
The plugin is pure Python (standard library only), so a single "_any" file covers every OS; per-OS copies are
byte-identical. The plugin's name and version come from the PEP 723 header inside the file, not the file name.
"""
import glob
import hashlib
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "threedfp_profiles.py")
DIST = os.path.join(ROOT, "dist")
# The targets Orca Cloud's upload form accepts (cloud.orcaslicer.com, plugin release form).
TARGETS = ["any", "win_x86_64", "win_arm64", "win", "macosx_arm64", "macosx_x86_64", "macosx_universal", "macosx",
           "linux_x86_64", "linux_arm64", "linux"]
DEFAULT = ["any"]


def main(targets):
    with open(SRC, encoding="utf-8") as f:
        text = f.read()
    version = re.search(r'^# version = "([^"]+)"', text, re.M).group(1)
    os.makedirs(DIST, exist_ok=True)
    for old in glob.glob(os.path.join(DIST, "threedfp_profiles_*.py")):
        os.remove(old)  # drop copies from earlier runs (some used names Orca Cloud rejects)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    print("threedfp_profiles.py version %s (sha256 %s)" % (version, digest))
    for t in targets:
        dst = os.path.join(DIST, "threedfp_profiles_%s.py" % t)
        shutil.copyfile(SRC, dst)
        print("  " + os.path.relpath(dst, ROOT))


if __name__ == "__main__":
    wanted = sys.argv[1:] or DEFAULT
    unknown = [t for t in wanted if t not in TARGETS]
    if unknown:
        sys.exit("unknown target(s): %s; Orca Cloud accepts %s" % (", ".join(unknown), ", ".join(TARGETS)))
    main(wanted)
