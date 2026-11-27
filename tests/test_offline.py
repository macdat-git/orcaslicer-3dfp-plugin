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
    check("no .info written for a new preset", os.path.exists(path[:-5] + ".info"), False)

# --- the .info OrcaSlicer keeps for Orca Cloud sync
def info_file(d, name, body):
    p = os.path.join(d, "filament", name + ".info")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    return p


def touch_json(d, name):
    with open(os.path.join(d, "filament", name + ".json"), "w", encoding="utf-8") as f:
        f.write("{}")


with tempfile.TemporaryDirectory() as d:
    # updating a synced preset: keep its cloud ID, mark it for upload, newer time
    p = info_file(d, preset["name"], "sync_info = \nuser_id = u1\nsetting_id = 4d54290d-b529\nbase_id = GFSL95_19\nupdated_time = 1000\n")
    touch_json(d, preset["name"])
    path, _ = m.write_user_preset(d, dict(preset, setting_id="GFSL95_19", base_id="x", updated_time="1"), "2.5.2.0")
    info = m.read_info(p)
    check("update keeps cloud ID", info["setting_id"], "4d54290d-b529")
    check("update marked for upload", info["sync_info"], "update")
    check("update bumps time", int(info["updated_time"]) > 1000, True)
    check("update keeps user and base", (info["user_id"], info["base_id"]), ("u1", "GFSL95_19"))
    data = json.load(open(path, encoding="utf-8"))
    check("sync fields kept out of the json", [k for k in m.INFO_KEYS if k in data], [])

with tempfile.TemporaryDirectory() as d:
    # a .info left from a removed preset: its old ID would get the new file deleted on the next sync
    p = info_file(d, preset["name"], "sync_info = \nuser_id = u1\nsetting_id = old-id\nbase_id = GFSL95_19\nupdated_time = 1000\n")
    m.write_user_preset(d, preset, "2.5.2.0")
    info = m.read_info(p)
    check("leftover info loses its ID", info["setting_id"], "")
    check("leftover info not marked", info["sync_info"], "")

with tempfile.TemporaryDirectory() as d:
    # a preset waiting for its first upload stays that way
    p = info_file(d, preset["name"], "sync_info = create\nuser_id = u1\nsetting_id = \nbase_id = GFSL95_19\nupdated_time = 1000\n")
    touch_json(d, preset["name"])
    m.write_user_preset(d, preset, "2.5.2.0")
    info = m.read_info(p)
    check("pending upload kept", (info["sync_info"], info["setting_id"]), ("create", ""))
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

# --- auto-link: used slots -> the spools 3DFP places in this printer's AMS (shaped like /my/spools, values made up)
def sp(code, mat, mtype, rgb, loc, grams=500):
    return {"short_code": code, "material": mat, "material_type": mtype, "rgb": rgb, "location": loc, "remaining_grams": grams}


spools = [sp("Y1", "PETG", "Basic", "#FCE300", "Printer: H2C, AMS: A-1"),
          sp("K2", "PETG", "Basic", "#000000", "Printer: H2C, AMS: A-2"),
          sp("K3", "PETG", "Basic", "#000000", "Printer: H2C, AMS: A-3"),
          sp("G4", "PETG", "High Speed Matte", "#63666A", "Printer: H2C, AMS: A-4"),
          sp("B5", "PLA", "Silk+", "#008BDA", "Printer: H2C, AMS: B-4"),
          sp("A6", "ASA", "Basic", "#000000", "Printer: H2C, AMS: HT-A-1"),
          sp("E7", "PLA", "Basic", "#FFFFFF", "Printer: H2C, External Spool 1", 0),
          sp("P8", "PLA", "Basic", "#000000", "Printer: P2S, AMS: A-1"),
          sp("S9", "PLA", "Basic", "#000000", "Shelf")]
check("position AMS", m.spool_position("Printer: H2C, AMS: B-4"), ("H2C", "B-4", (0, "B", 4)))
check("position HT", m.spool_position("Printer: H2C, AMS: HT-A-1"), ("H2C", "HT-A-1", (1, "A", 1)))
check("position external", m.spool_position("Printer: H2C, External Spool 2")[2], (2, "", 2))
check("position shelf", m.spool_position("Shelf: Closet Shelf 3"), None)
check("printer from preset name", m.printer_spools(spools, "Bambu Lab H2C 0.4 nozzle")[0], "H2C")
check("printer P2S", [t[2]["short_code"] for t in m.printer_spools(spools, "Bambu Lab P2S 0.4 nozzle")[1]], ["P8"])
check("printer unknown", m.printer_spools(spools, "Voron 2.4"), ("", []))
check("type same", m.type_score("PLA", spools[4]), 2)
check("type with variant", m.type_score("PLA-CF", sp("x", "PLA", "CF", "", "")), 2)
check("type base only", m.type_score("PLA-CF", spools[4]), 1)
check("type different", m.type_score("ABS", spools[4]), 0)
check("colour same", round(m.colour_distance("#000000", "#000000FF")), 0)
check("colour unknown is far", m.colour_distance("", "#000000") > 700, True)

H2C = "Bambu Lab H2C 0.4 nozzle"
slots = [{"preset": "Bambu PETG Basic @BBL H2C", "color": "#000000", "type": "PETG"},
         {"preset": "Bambu PETG Basic @BBL H2C", "color": "#FCE300", "type": "PETG"},
         {"preset": "Bambu PLA Silk+ @BBL H2C", "color": "#0A8CDC", "type": "PLA"},
         {"preset": "Bambu PETG Basic @BBL H2C", "color": "#000000", "type": "PETG"},
         {"preset": "Generic ABS @BBL H2C", "color": "#FF0000", "type": "ABS"}]
links, unmatched = m.link_slots(slots, [10.0, 2.5, 1.2, 3.0, 0.0], spools, H2C)
check("auto exact colour", (links["1"]["code"], links["1"]["where"]), ("K2", "H2C A-2"))
check("auto tie lists the other spool", links["1"]["alts"], ["A-3"])
check("auto yellow", links["2"]["code"], "Y1")
check("auto nearest colour", links["3"]["code"], "B5")
check("second black slot gets the other black spool", links["4"]["code"], "K3")
check("unused slot not linked", "5" in links, False)
check("all used slots linked", unmatched, [])
check("plan", m.links_plan([10.0, 2.5, 1.2, 3.0], links), {"K2": 10.0, "Y1": 2.5, "B5": 1.2, "K3": 3.0})
links, unmatched = m.link_slots(slots, [1.0, 0, 0, 0, 4.0], spools, H2C, manual={"1": "MANUAL"})
check("manual link wins", (links["1"]["code"], links["1"]["source"]), ("MANUAL", "manual"))
check("no ABS in the AMS", [u["slot"] for u in unmatched], ["5"])
check("empty spool skipped", m.link_slots([{"type": "PLA", "color": "#FFFFFF"}], [1.0], [spools[6]], H2C)[0], {})
check("printer not in 3DFP", m.link_slots(slots, [1.0], spools, "Voron 2.4")[1][0]["slot"], "1")

# --- embedded logo: a 64x64 PNG, and the panel HTML has the slot it goes into
import base64, struct
png = base64.b64decode(m.LOGO_PNG_B64)
check("logo is a PNG", png[:8], b"\x89PNG\r\n\x1a\n")
check("logo is 64x64", struct.unpack(">II", png[16:24]), (64, 64))
check("panel has logo slot", m.PANEL_HTML.count("__LOGO__"), 1)

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
check("auto-link on by default", cap.settings()["auto_link"], True)
check("auto-deduct on by default", m.DEFAULTS["auto_deduct"], True)
check("AMS add-spools on by default", cap.settings()["ams_add_spools"], True)
check("AMS sync setting hidden without the service", cap.ams_sync(), False)
mo._write_json("ams_sync.json", {"service": "3DFP AMS Sync", "mode": "live"})
check("AMS sync setting shown when the service left its marker", cap.ams_sync(), True)
cap.set_setting("auto_deduct", False)  # the offer-to-deduct path below

# the print-start path with a stub UI and API: auto-links, then offers to deduct
notes = []
orca.host.ui = types.SimpleNamespace(NotificationLevel=types.SimpleNamespace(RegularNotificationLevel=0, WarningNotificationLevel=1),
                                     push_notification=lambda lvl, text, **kw: notes.append((lvl, text)))
cap.set_setting("api_key", "3dfp_test")
cap.spools = spools
cap.job_spools = lambda: None  # no network
cap.last_slice = {"grams": [10.0, 2.5], "slots": slots[:2], "printer_name": H2C, "printer_code": "H2C", "deducted": False}
cap._print_started_job()
check("offer names the spools", notes and "10.0 g from K2" in notes[-1][1] and "2.5 g from Y1" in notes[-1][1], True)
check("plan kept for the click", cap.last_slice["plan"], {"K2": 10.0, "Y1": 2.5})
log = open(os.path.join(storage, "events.log"), encoding="utf-8").read()
check("events.log records the links", '"1": ["K2", "auto", "H2C A-2", "or A-3"]' in log, True)
cap.last_slice = {"grams": [4.0], "slots": [slots[4]], "printer_name": H2C, "printer_code": "H2C", "deducted": False}
notes.clear()
cap._print_started_job()
check("unmatched slot warns", notes and notes[0][0] == 1 and "slot 1" in notes[0][1], True)

print("\npanel script written to", js_path)
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
