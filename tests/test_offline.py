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
import time
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
# a hand link to a spool 3DFP has on a shelf (swapped out) gives way to the matching AMS spool
links, unmatched = m.link_slots(slots, [0, 7.0], spools, H2C, manual={"2": "S9"})
check("stale hand link replaced by the AMS spool", (links["2"]["code"], links["2"]["source"], links["2"].get("replaced")),
      ("Y1", "auto", "S9"))
links, unmatched = m.link_slots(slots, [0, 0, 0, 0, 4.0], spools, H2C, manual={"5": "S9"})
check("stale hand link kept when no AMS spool fits", (links["5"]["code"], links["5"]["source"], unmatched), ("S9", "manual", []))
check("hand link in the printer still wins", m.link_slots(slots, [0, 7.0], spools, H2C, manual={"2": "K2"})[0]["2"]["code"], "K2")
check("hand link to a spool on another printer replaced",
      m.link_slots(slots, [0, 0, 1.0], spools, H2C, manual={"3": "P8"})[0]["3"].get("replaced"), "P8")
check("hand link without a spool list kept", m.link_slots(slots, [0, 7.0], [], H2C, manual={"2": "S9"})[0]["2"]["code"], "S9")
ck = m.slice_checks([0, 7.0], slots, m.link_slots(slots, [0, 7.0], spools, H2C, manual={"2": "S9"})[0], spools, H2C, "H2C", 0)
check("replaced link is reported", [c["kind"] for c in ck], ["relinked"])

# --- slice checks: short spool, type / colour mismatch, stale manual link
low = [sp("K2", "PETG", "Basic", "#000000", "Printer: H2C, AMS: A-2", 12),
       sp("Y1", "PETG", "Basic", "#FCE300", "Printer: H2C, AMS: A-1"),
       sp("S9", "PLA", "Basic", "#FFFFFF", "Shelf: Closet Shelf 3"),
       sp("M1", "PLA", "Silk", "#FF0000,#0000FF", "Printer: H2C, AMS: B-1")]
links = {"1": {"code": "K2", "source": "auto", "where": "H2C A-2", "alts": []},
         "2": {"code": "S9", "source": "manual", "where": "", "alts": []},
         "3": {"code": "M1", "source": "manual", "where": "", "alts": []},
         "4": {"code": "GONE", "source": "manual", "where": "", "alts": []}}
cslots = [{"type": "PETG", "color": "#000000"}, {"type": "PETG", "color": "#000000"},
          {"type": "PLA", "color": "#00FF00"}, {"type": "PLA", "color": "#000000"}]
ck = m.slice_checks([10.0, 5.0, 1.0, 1.0], cslots, links, low, H2C, "H2C", margin=10)
kinds = sorted((c["slot"], c["kind"]) for c in ck)
check("checks found", kinds, [("1", "low"), ("2", "colour"), ("2", "stale"), ("2", "type"), ("4", "missing")])
check("low names grams", [c["text"] for c in ck if c["kind"] == "low"], ["Slot 1 (AMS: A-2) needs 10 g, spool K2 has 12 g"])
check("multicolour spool: no colour warning", ("3", "colour") in kinds, False)
check("manual link inside this printer isn't stale", ("3", "stale") in kinds, False)
check("enough filament: no warning", m.slice_checks([1.0], cslots, {"1": links["1"]}, low, H2C, "H2C", 0), [])
two = {"1": {"code": "Y1", "source": "auto"}, "2": {"code": "Y1", "source": "auto"}}
check("one spool used by two slots is summed", [c["text"] for c in m.slice_checks(
    [300.0, 250.0], [{"type": "PETG", "color": "#FCE300"}] * 2, two, low, H2C, "H2C", 10)], ["Slot 1 (AMS: A-1) needs 550 g, spool Y1 has 500 g"])
check("printer not tracked: no stale warning", [c["kind"] for c in m.slice_checks(
    [1.0], [{"type": "PLA", "color": "#FFFFFF"}], {"1": links["2"]}, low, "Voron 2.4", "ANY", 0)], [])
check("stale warning has no position", [c["text"] for c in ck if c["kind"] == "stale"][0].startswith("Slot 2 is linked"), True)

# --- slot names: where each slot's spool is, named as in 3DFP
check("location name AMS", m.location_name("Printer: H2C, AMS: A-1"), "AMS: A-1")
check("location name HT", m.location_name("Printer: H2C, AMS: HT-A-1"), "AMS: HT-A-1")
check("location name external", m.location_name("Printer: P2S, External Spool"), "External Spool")
check("location name shelf", m.location_name("Shelf: Closet Shelf 3"), "")
ps = m.slot_positions(slots, spools, H2C, "H2C", {"5": "S9"})
check("positions from auto-link", {k: v["name"] for k, v in ps.items() if not v.get("away")},
      {"1": "AMS: A-2", "2": "AMS: A-1", "3": "AMS: B-4", "4": "AMS: A-3"})
check("tie shows the other position", ps["1"]["alts"], ["AMS: A-3"])
check("manual link on a shelf shows where the spool is", (ps["5"]["name"], ps["5"]["away"]), ("Shelf", True))
check("manual link to an unknown spool", m.slot_positions(slots[:1], spools, H2C, "H2C", {"1": "GONE"})["1"]["name"],
      "not in your 3DFP spools")
check("manual link in the printer", m.slot_positions(slots[:1], spools, H2C, "H2C", {"1": "A6"})["1"]["name"], "AMS: HT-A-1")
check("auto-link off: only manual links", list(m.slot_positions(slots, spools, H2C, "H2C", {"2": "G4"}, auto=False)), ["2"])
check("printer not in 3DFP: no positions", m.slot_positions(slots, spools, "Voron 2.4"), {})

# --- Load my AMS: this printer's spools in AMS order onto the filament slots
pr, plan, left = m.ams_load_plan(spools, H2C, "H2C", 4)
check("load printer", pr, "H2C")
check("load order", [(i, label, s["short_code"]) for i, label, s in plan],
      [(1, "A-1", "Y1"), (2, "A-2", "K2"), (3, "A-3", "K3"), (4, "A-4", "G4")])
check("load leftovers", [label for label, _ in left], ["B-4", "HT-A-1", "External Spool 1"])
check("load fewer spools than slots", len(m.ams_load_plan(spools, "Bambu Lab P2S 0.4 nozzle", "P2S", 4)[1]), 1)
check("load unknown printer", m.ams_load_plan(spools, "Voron", "ANY", 4)[0], "")

# --- history: rebuilt from events.log, and undo puts the grams back on today's weight
logtext = ("2026-11-24 10:00:00 slice: 12.30 g on H2C\n"
           "2026-11-24 10:02:00 deduct (print start): K2 (PETG Basic Black, H2C A-2): −12.3 g, 487.7 g left; "
           "Y1: −2.5 g, 497.5 g left\n2026-11-24 11:00:00 deduct (button): nothing\n")
hist = m.history_from_log(logtext)
check("history from log", [(h["when"], h["source"], [(i["code"], i["grams"], i["after"]) for i in h["items"]]) for h in hist],
      [("2026-11-24 10:02:00", "print start", [("K2", 12.3, 487.7), ("Y1", 2.5, 497.5)])])
entry = {"items": [{"code": "K2", "grams": 12.3}, {"code": "E0", "grams": 30.0, "location": "Printer: H2C, AMS: A-4"},
                   {"code": "NOPE", "grams": 1.0}]}
items, probs = m.undo_items(entry, {"K2": {"remaining_grams": 480}, "E0": {"remaining_grams": 0, "location": "Empty"}})
check("undo adds to today's weight", items[0], {"short_code": "K2", "remaining_grams": 492})
check("undo of an emptied spool puts it back", items[1], {"short_code": "E0", "remaining_grams": 30, "location": "Printer: H2C, AMS: A-4"})
check("undo names missing spools", probs, ["NOPE: no longer in your 3DFP spools"])

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

# deduct -> history -> undo, Mark empty, quota, with a stub API
class StubApi:
    def __init__(self):
        self.sent, self.quota = [], {"limit": 1000, "remaining": 90}

    def deduct(self, used):
        self.sent.append(("deduct", dict(used)))
        return {"data": [{"short_code": c, "deducted_grams": g, "remaining_grams": 500 - g} for c, g in used.items()]}

    def update(self, items):
        self.sent.append(("update", list(items)))
        return {"data": [{"short_code": i["short_code"], "remaining_grams": i.get("remaining_grams")} for i in items]}


api = StubApi()
cap.client = api
cap.post = lambda obj: None
cap.result = lambda ok, title, lines=(), more=(): notes.append(("result", ok, title, list(lines)))
cap.spools = [dict(s) for s in spools]
cap.last_slice = {"grams": [10.0, 2.5], "slots": slots[:2], "printer_name": H2C, "printer_code": "H2C", "deducted": False,
                  "when": "sliced 10:00"}
cap.resolve_links(refresh=False)
cap.job_deduct(manual=True)
h = cap.history()
check("deduction recorded", [(e["kind"], e["printer"], [(i["code"], i["grams"], i["after"], i["location"]) for i in e["items"]])
                             for e in h if not e.get("imported")],
      [("deduct", "H2C", [("K2", 10.0, 490.0, "Printer: H2C, AMS: A-2"), ("Y1", 2.5, 497.5, "Printer: H2C, AMS: A-1")])])
check("last deduction can be undone", (cap.last_undoable() or {}).get("id"), h[-1]["id"])
check("low quota warns once", sum("API requests left" in n[1] for n in notes if n[0] == 1), 0)
cap.save_quota()
cap.save_quota()
check("low quota warns once", sum("API requests left" in str(n[1]) for n in notes), 1)
check("quota kept for Settings", mo._read_json("quota.json", {})["remaining"], 90)
cap.spools[1]["remaining_grams"] = 470  # used again since the deduction
cap.job_undo(h[-1]["id"])
check("undo sends today's weight plus the grams", api.sent[-1],
      ("update", [{"short_code": "K2", "remaining_grams": 480}, {"short_code": "Y1", "remaining_grams": 500}]))
h = cap.history()
check("undo recorded and the deduction marked", (h[-1]["kind"], h[-2].get("undone")), ("undo", True))
check("nothing left to undo", cap.last_undoable(), None)
check("slice can be deducted again", cap.last_slice["deducted"], False)
cap.job_undo(h[-2]["id"])
check("no double undo", notes[-1][1:3], (False, "That was already undone."))
cap.job_empty("G4")
check("mark empty sends 0 g", api.sent[-1], ("update", [{"short_code": "G4", "remaining_grams": 0}]))
check("mark empty can be undone", cap.history()[-1]["items"][0]["grams"], 500.0)

# Load my AMS with the profile step stubbed: spools in AMS order, manual links cleared (auto-link finds them)
used = []
cap.use_profile = lambda action, kind, ident, slot, st, s, remember=True: (used.append((slot, ident, remember)) or (True, ident, ["x"], []))
cap.set_setting("slot_spools", {"1": "OLD", "4": "OLD4"})
cap.job_load_ams({"slots": slots[:3], "printer_name": H2C, "printer_code": "H2C"})
check("load AMS applies in order", used, [(1, "Y1", False), (2, "K2", False), (3, "K3", False)])
check("load AMS clears manual links it replaced", cap.settings()["slot_spools"], {"4": "OLD4"})
check("load AMS tells how many slots to add", "add 4 filaments" in " ".join(notes[-1][3]), True)

# slice check runs from the cached list and notifies
cap.spools_at = time.time()
cap.set_setting("slot_spools", {})
cap.last_slice = {"grams": [600.0], "slots": slots[:1], "printer_name": H2C, "printer_code": "H2C", "deducted": False}
notes.clear()
cap._slice_check_job(cap.last_slice)
check("slice check warns about the short spool", [c["kind"] for c in cap.last_slice["checks"]], ["low"])
check("slice check notification", notes and notes[0][0] == 1 and "needs 600 g" in notes[0][1], True)
posted = []
cap.post = posted.append
cap.set_setting("slot_spools", {"1": "K3"})  # relinked in Slots after the slice: the checks follow
cap.push_state()
st_msg = next(p for p in posted if p["type"] == "state")
check("state carries the checks and settings", ([c["text"] for c in st_msg["last_slice"]["checks"]],
                                                st_msg["settings"]["low_stock_g"]), (["Slot 1 (AMS: A-3) needs 600 g, spool K3 has 500 g"], 150))
check("state carries the quota", [p["remaining"] for p in posted if p["type"] == "quota"], [90])
cap.set_setting("slice_checks", False)
cap.push_state()
check("checks cleared when turned off", "checks" in cap.last_slice, False)
# a preset used in several slots no longer copies one spool into all of them while auto-link is on
cap.set_setting("slice_checks", True)
cap.set_setting("slot_spools", {})
cap.set_setting("preset_spools", {"Bambu PETG Basic @BBL H2C": "K3"})
cap.host_state = lambda: {"printer_name": H2C, "printer_code": "H2C", "nozzle": "0.4", "slots": slots, "catalog": {},
                          "all_filaments": [], "user_dir": "", "app_version": "", "signed_in": False}
posted.clear()
cap.push_state()
check("no preset relink with auto-link", cap.settings()["slot_spools"], {})
st_msg = next(p for p in posted if p["type"] == "state")
check("state names the slots", {k: v["name"] for k, v in st_msg["slot_pos"].items()},
      {"1": "AMS: A-2", "2": "AMS: A-1", "3": "AMS: B-4", "4": "AMS: A-3"})  # slot 5 is ABS: no ABS loaded
cap.set_setting("auto_link", False)
cap.push_state()
check("preset relink without auto-link", cap.settings()["slot_spools"], {"1": "K3", "2": "K3", "4": "K3"})
cap.set_setting("auto_link", True)
cap.on_message({"type": "clear_links"})
check("clear links", (cap.settings()["slot_spools"], cap.settings()["preset_spools"]), ({}, {}))
check("slice-check defaults", (m.DEFAULTS["slice_checks"], m.DEFAULTS["low_margin_g"], m.DEFAULTS["low_stock_g"]), (True, 10, 150))

print("\npanel script written to", js_path)
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
