"""Offline tests: no OrcaSlicer and no API key needed.

    python tests/test_offline.py

Runs the pure-Python helpers, then loads the plugin against a stub `orca` module to check
registration and the permissions it asks for. Exit code 0 = all passed."""
import importlib.util
import json
import os
import re
import sys
import tempfile
import types

PLUGIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "threedfp_profiles.py")
fails = 0


def load(name, with_orca=None):
    if with_orca is not None:
        sys.modules["orca"] = with_orca
    else:
        sys.modules.pop("orca", None)
    spec = importlib.util.spec_from_file_location(name, PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(("PASS " if ok else "FAIL ") + label + ("" if ok else f"  got={got!r} want={want!r}"))


m = load("plugin_plain")

# --- printer / nozzle detection
for name, want in [("Bambu Lab H2C", "H2C"), ("Bambu Lab P2S 0.4 nozzle", "P2S"), ("Bambu Lab X1 Carbon", "X1C"),
                   ("Bambu Lab P1S", "X1C"), ("Bambu Lab A1 mini", "A1M"), ("Bambu Lab A1", "A1"),
                   ("Bambu Lab H2D Pro", "H2DP"), ("Bambu Lab H2D", "H2D"), ("Voron 2.4 350", "ANY")]:
    check("printer " + name, m.printer_code(name), want)
check("nozzle list", m.nozzle_code(["0.4", "0.4"]), "0.4")
check("nozzle csv", m.nozzle_code("0.6,0.6"), "0.6")
check("nozzle in name", m.nozzle_code("Bambu Lab H2C 0.2 nozzle"), "0.2")
check("nozzle default", m.nozzle_code(None), "0.4")

# --- setting serialization for PresetTransaction.set()
check("floats", m.setting_string(["220", "225"]), "220,225")
check("replicate per variant", m.setting_string(["220"], 3), "220,220,220")
check("percent", m.setting_string(["95%"]), "95%")
check("float scalar", m.setting_string(0.98), "0.98")
check("int-valued float", m.setting_string(21.0), "21")
check("bool", m.setting_string(True), "1")
check("plain string vector", m.setting_string(["PLA"]), "PLA")
check("quoted string vector", m.setting_string(["Bambu Lab", "x"]), '"Bambu Lab";x')
check("newline escaped", m.setting_string(["; start\nM104"]), '"; start\\nM104"')
check("colour", m.setting_string(["#FFAA00"]), "#FFAA00")

# --- a profile shaped like the API's slicer=orca response (values made up)
preset = {"type": "filament", "name": "Example PLA Matte Black (3DFP) @H2C",
          "inherits": "Generic PLA High Speed @BBL H2C 0.4 nozzle", "from": "User", "version": "1.8.0.0",
          "filament_settings_id": ["Example PLA Matte Black (3DFP) @H2C"],
          "filament_notes": ["Generated from community data.\nReview all settings before printing."],
          "compatible_printers": ["Bambu Lab H2C 0.4 nozzle"], "nozzle_temperature": ["200"],
          "filament_flow_ratio": ["1.02"], "filament_density": ["1.36"], "filament_type": ["PLA"],
          "filament_vendor": ["Example"], "default_filament_colour": ["#000000"]}
vals = {k: m.setting_string(v, 2) for k, v in m.slot_values(preset).items()}
check("meta keys not staged", any(k in vals for k in ("name", "inherits", "compatible_printers", "filament_settings_id", "type")), False)
check("temp per variant", vals["nozzle_temperature"], "200,200")
check("notes quoted", vals["filament_notes"].startswith('"Generated') and "\\n" in vals["filament_notes"], True)

with tempfile.TemporaryDirectory() as d:
    path, replaced = m.write_user_preset(d, preset, "2.5.2.0", os.path.join(d, "bk"))
    data = json.load(open(path, encoding="utf-8"))
    check("file name", os.path.basename(path), "Example PLA Matte Black (3DFP) @H2C.json")
    check("from User", data["from"], "User")
    check("type removed", "type" in data, False)
    check("keeps given version", data["version"], "1.8.0.0")
    check("first write", replaced, False)
    _, replaced2 = m.write_user_preset(d, preset, "2.5.2.0", os.path.join(d, "bk"))
    check("rewrite replaces", replaced2, True)
    check("backup kept", len(os.listdir(os.path.join(d, "bk"))), 1)
check("blocked word in file name", m.preset_file_name("Elegoo PLA Confetti @BBL P2S"), "Elegoo PLA C_onfetti @BBL P2S.json")
check("bad chars in file name", m.preset_file_name('A/B: "C"?'), "A_B_ _C__.json")

# --- choosing the slot's base preset
cat = {"Bambu PLA Basic @BBL H2C": {"type": "PLA", "system": True},
       "Generic PLA @BBL H2C": {"type": "PLA", "system": True},
       "Generic PLA High Speed @BBL H2C": {"type": "PLA", "system": True},
       "Generic PLA Silk @BBL H2C": {"type": "PLA", "system": True},
       "Generic PETG @BBL H2C": {"type": "PETG", "system": True},
       "My PLA": {"type": "PLA", "system": False}}
check("base exact", m.pick_base("Bambu PLA Basic @BBL H2C", "PLA", cat), "Bambu PLA Basic @BBL H2C")
check("base strips nozzle suffix", m.pick_base("Generic PLA High Speed @BBL H2C 0.4 nozzle", "PLA", cat), "Generic PLA High Speed @BBL H2C")
check("base adds nozzle suffix", m.pick_base("Bambu ABS @BBL H2C", "ABS", {"Bambu ABS @BBL H2C 0.4 nozzle": {"type": "ABS", "system": True}}), "Bambu ABS @BBL H2C 0.4 nozzle")
check("base closest variant", m.pick_base("Bambu PLA Silk+ @BBL H2C", "PLA", cat), "Generic PLA Silk @BBL H2C")
check("base generic fallback", m.pick_base("Bambu PLA Matte @BBL H2C", "PLA", cat), "Generic PLA @BBL H2C")
check("base by type", m.pick_base(None, "petg", cat), "Generic PETG @BBL H2C")
check("base none", m.pick_base(None, "PC", cat), None)
check("first hex of multicolour", m.first_hex("#88bfff,#DA63C4"), "#88BFFF")
check("first hex none", m.first_hex(None), None)

# --- G-code stats and the deduction plan
with tempfile.TemporaryDirectory() as d:
    g = os.path.join(d, "plate_1.gcode")
    with open(g, "w") as f:
        f.write("; total filament used [g] = 16.40\n" + "G1 X1\n" * 50000 +
                "; filament used [mm] = 4000.10, 1500.00\n; filament used [g] = 12.30, 4.10\n")
    check("grams per filament", m.gcode_grams(g), [12.3, 4.1])
check("plan", m.deduct_plan([12.3, 4.1, 0.0], {"1": "ABC123", "3": "XYZ"}), {"ABC123": 12.3})
check("plan sums one spool", m.deduct_plan([1.0, 2.5], {"1": "A", "2": "A"}), {"A": 3.5})

# --- the panel's script is extracted so it can be syntax-checked: node --check <file>
js = re.search(r"<script>(.*)</script>", m.PANEL_HTML, re.S).group(1)
js_path = os.path.join(tempfile.gettempdir(), "threedfp_panel.js")
with open(js_path, "w", encoding="utf-8") as f:
    f.write(js)

# --- load against a stub orca module
calls = {}
orca = types.ModuleType("orca")
orca.base = type("Base", (), {})
orca.script = types.SimpleNamespace(ScriptPluginCapabilityBase=type("ScriptBase", (), {"__init__": lambda self: None}))
orca.plugin = lambda cls: calls.setdefault("plugin", cls) or cls
orca.register_capability = lambda cls: calls.setdefault("caps", []).append(cls)
orca.request_permissions = lambda **kw: calls.setdefault("perms", kw)
store = os.path.realpath(tempfile.mkdtemp())
storage = os.path.join(store, "orca_plugins", "plugin_data", "k")
os.makedirs(storage)
orca.host = types.SimpleNamespace(plugin=types.SimpleNamespace(storage=lambda: storage))
mo = load("plugin_orca", orca)
calls["plugin"]().register_capabilities()
check("one capability", [c.__name__ for c in calls["caps"]], ["ThreeDFPPanel"])
check("network grant is host-wide", calls["perms"]["network_http"], ["https://3dfilamentprofiles.com"])
check("threads requested", calls["perms"]["threading"], True)
cap = mo.ThreeDFPPanel()
check("capability name", cap.get_name(), "3D Filament Profiles")
check("data_dir from storage path", mo.data_dir(), store)
cap.set_setting("printer", "P2S")
check("settings persist", cap.settings()["printer"], "P2S")
check("defaults kept", cap.settings()["save_preset"], True)

print("\npanel script written to", js_path)
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
