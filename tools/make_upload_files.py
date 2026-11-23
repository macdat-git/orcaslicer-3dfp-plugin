"""Write the Orca Cloud upload files: one copy of the plugin per target OS suffix.

    python tools/make_upload_files.py            # all targets
    python tools/make_upload_files.py win_x86_64 # just these

The plugin is pure Python (standard library only), so every copy is byte-identical; only the file name changes
(Orca Cloud wants a target-OS suffix such as my_plugin_win_x86_64.whl). The plugin's name and version come from
the PEP 723 header inside the file, not from the file name.
"""
import hashlib
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "threedfp_profiles.py")
DIST = os.path.join(ROOT, "dist")
TARGETS = ["win_x86_64", "win_arm64", "macos_arm64", "macos_x86_64", "linux_x86_64", "linux_aarch64"]


def main(targets):
    with open(SRC, encoding="utf-8") as f:
        text = f.read()
    version = re.search(r'^# version = "([^"]+)"', text, re.M).group(1)
    os.makedirs(DIST, exist_ok=True)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    print("threedfp_profiles.py version %s (sha256 %s)" % (version, digest))
    for t in targets:
        dst = os.path.join(DIST, "threedfp_profiles_%s.py" % t)
        shutil.copyfile(SRC, dst)
        print("  " + os.path.relpath(dst, ROOT))


if __name__ == "__main__":
    wanted = sys.argv[1:] or TARGETS
    unknown = [t for t in wanted if t not in TARGETS]
    if unknown:
        sys.exit("unknown target(s): %s; choose from %s" % (", ".join(unknown), ", ".join(TARGETS)))
    main(wanted)
