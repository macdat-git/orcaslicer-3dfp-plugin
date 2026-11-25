# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "3D Filament Profiles"
# description = "Connects OrcaSlicer to 3dfilamentprofiles.com: search the filament catalog or your own spools, pull a ready-made OrcaSlicer filament preset (your calibrated values for your spools), apply it to a filament slot and save it as a user preset, and deduct what a print used from your 3DFP spools. Needs a 3DFP API key (subscription or Patreon)."
# author = "Andy Mackey"
# version = "1.1.0"
# ///
"""OrcaSlicer 2.5 plugin for the 3D Filament Profiles API (https://3dfilamentprofiles.com/docs/api).

What it does, from a panel docked beside the 3D view:
  * Find      search the 3DFP filament catalog (brand / material / colour / hex).
  * My spools list the spools in your 3DFP account.
  * For either, "Use in slot N" fetches 3DFP's OrcaSlicer preset for your printer + nozzle, puts its
    values on the matching system preset in that filament slot (shows as an unsaved edit), and saves
    it as a user preset file so it is in the filament list from the next start.
  * Slots     link each filament slot to a 3DFP spool; after a print starts (or on demand) the grams
    the slice used are deducted from those spools in one API request.

The API client and helpers at the top have no dependency on the `orca` module, so they can be tested
with any Python 3.12 (see the __main__ block at the bottom).
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PLUGIN_VERSION = "1.1.0"
SITE = "https://3dfilamentprofiles.com"
API_BASE = SITE + "/api/v1"
USER_AGENT = "OrcaSlicer-3DFP-Plugin/" + PLUGIN_VERSION

# ---------------------------------------------------------------------------
# 3DFP API client (stdlib only)
# ---------------------------------------------------------------------------

FRIENDLY = {
    "invalid_key": "3DFP rejected the API key. Paste your key (starts with 3dfp_) in Settings.",
    "no_access": "Your 3DFP account has no active API access (needs the API subscription or a Patreon tier).",
    "blocked": "3DFP has blocked this account.",
    "rate_limited": "Monthly 3DFP API quota used up. It resets at 00:00 UTC on the 1st.",
    "no_base_preset": "3DFP has no OrcaSlicer base preset for this filament's material.",
    "not_found": "3DFP has no filament or spool with that ID.",
    "internal": "3DFP had an internal error. Try again.",
}


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def friendly(self):
        return FRIENDLY.get(self.code) or self.message or ("HTTP %s" % self.status)


class Client:
    def __init__(self, api_key, timeout=20, base=API_BASE):
        self.key = (api_key or "").strip()
        self.timeout = timeout
        self.base = base
        self.quota = {}  # {"limit": int, "remaining": int} from the last response

    def _note_quota(self, headers):
        try:
            lim, rem = headers.get("X-RateLimit-Limit"), headers.get("X-RateLimit-Remaining")
            if lim is not None and rem is not None:
                self.quota = {"limit": int(lim), "remaining": int(rem)}
        except Exception:
            pass

    def request(self, method, path, params=None, body=None):
        if not self.key:
            raise ApiError(401, "invalid_key", "No API key set")
        url = self.base + path
        if params:
            clean = {k: ("true" if v is True else "false" if v is False else v)
                     for k, v in params.items() if v not in (None, "")}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        data = None
        headers = {"Authorization": "Bearer " + self.key, "Accept": "application/json", "User-Agent": USER_AGENT}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                self._note_quota(r.headers)
                raw = r.read()
        except urllib.error.HTTPError as e:
            self._note_quota(e.headers or {})
            code, msg = "http_%d" % e.code, ""
            try:
                err = json.loads(e.read() or b"{}").get("error") or {}
                code, msg = err.get("code") or code, err.get("message") or ""
            except Exception:
                pass
            raise ApiError(e.code, code, msg or ("HTTP %d" % e.code)) from None
        except urllib.error.URLError as e:
            raise ApiError(0, "network", "Can't reach 3dfilamentprofiles.com: %s" % (e.reason,)) from None
        except TimeoutError:
            raise ApiError(0, "network", "3dfilamentprofiles.com timed out") from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError(200, "bad_response", "3DFP sent a response that isn't JSON") from None

    # --- filaments
    def search(self, q="", material="", brand="", page=1, limit=25):
        return self.request("GET", "/filaments", {"q": (q or "")[:100], "material": material, "brand": brand,
                                                  "page": page, "limit": limit})

    def filament(self, fid):
        return self.request("GET", "/filaments/%d" % int(fid))

    def filament_profile(self, fid, printer="ANY", nozzle="0.4", pressure_advance=False):
        return self.request("GET", "/filaments/%d/profile" % int(fid),
                            {"slicer": "orca", "printer": printer, "nozzle": nozzle,
                             "pressure_advance": bool(pressure_advance)})

    # --- spools
    def spools(self):
        return self.request("GET", "/my/spools")

    def spool_profile(self, code, printer="ANY", nozzle="0.4", pressure_advance=False):
        return self.request("GET", "/my/spools/%s/profile" % urllib.parse.quote(code, safe=""),
                            {"slicer": "orca", "printer": printer, "nozzle": nozzle,
                             "pressure_advance": bool(pressure_advance)})

    def deduct(self, used):
        """used: {short_code: grams}. One request for up to 50 spools."""
        body = [{"short_code": c, "used_grams": round(float(g), 2)} for c, g in used.items() if g and g > 0]
        if not body:
            return {"data": []}
        return self.request("PATCH", "/my/spools", body=body[:50])


# ---------------------------------------------------------------------------
# helpers: printer detection, setting serialization, preset files, G-code stats
# ---------------------------------------------------------------------------

# Bambu model names -> the printer codes the 3DFP profile endpoint takes. Order matters (longest first).
PRINTER_CODES = [("H2D Pro", "H2DP"), ("H2DP", "H2DP"), ("H2D", "H2D"), ("H2C", "H2C"), ("H2S", "H2S"),
                 ("X2D", "X2D"), ("X1 Carbon", "X1C"), ("X1C", "X1C"), ("X1E", "X1C"), ("X1", "X1C"),
                 ("P1S", "X1C"), ("P1P", "P1P"), ("P2S", "P2S"), ("A1 mini", "A1M"), ("A1M", "A1M"),
                 ("A2L", "A2L"), ("A1", "A1")]
PRINTER_CHOICES = ["auto", "ANY", "H2C", "P2S", "X1C", "P1P", "A1", "A1M", "A2L", "H2D", "H2DP", "H2S", "X2D"]
NOZZLES = ["0.2", "0.4", "0.6", "0.8"]


def printer_code(*names):
    """First Bambu model code found in the given names (printer_model, preset name); 'ANY' otherwise."""
    for s in names:
        s = str(s or "")
        if not re.search(r"bambu|bbl", s, re.I):
            continue
        for token, code in PRINTER_CODES:
            if re.search(r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])", s, re.I):
                return code
    return "ANY"


def nozzle_code(value):
    """'0.4' from 0.4 / '0.4,0.4' / ['0.4'] / 'Bambu Lab H2C 0.4 nozzle'; default '0.4'."""
    text = ",".join(as_list(value))
    m = re.search(r"(?<![\w.])\d*\.\d+(?![\w.])", text) or re.search(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", text)
    if not m:
        return "0.4"
    d = float(m.group())
    return min(NOZZLES, key=lambda n: abs(float(n) - d))


def as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    s = str(v)
    if ";" in s:
        return [x.strip().strip('"') for x in s.split(";")]
    return [x.strip() for x in s.split(",")] if s else []


_NUM = re.compile(r"^-?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?%?$")
_NEEDS_QUOTES = re.compile(r'[\s;"\\]')


def _cstyle(s):
    """One element of a ConfigOptionStrings, escaped the way OrcaSlicer serializes it."""
    if s and not _NEEDS_QUOTES.search(s):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") + '"'


def _scalar(v):
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return ("%.6f" % v).rstrip("0").rstrip(".") if v != int(v) else str(int(v))
    return str(v)


def setting_string(value, base_count=0):
    """JSON preset value -> the serialized string PresetTransaction.set() expects.

    Numeric/bool/percent vectors are comma separated ("220,220"); string vectors are ';'-joined
    C-style strings. A single numeric value is repeated to base_count entries (multi-variant printers
    such as the H2C keep one value per extruder variant)."""
    if not isinstance(value, (list, tuple)):
        return value if isinstance(value, str) else (json.dumps(value) if isinstance(value, dict) else _scalar(value))
    items = [_scalar(x) for x in value]
    if items and all(_NUM.match(x) or x.lower() == "nil" for x in items):
        if len(items) == 1 and base_count > 1:
            items = items * base_count
        return ",".join(items)
    return ";".join(_cstyle(x) for x in items)


# Keys that identify the preset rather than describe the filament: never staged onto a slot.
META_KEYS = {"name", "inherits", "from", "type", "version", "filament_settings_id", "setting_id", "base_id",
             "filament_id", "instantiation", "compatible_printers", "compatible_printers_condition",
             "compatible_prints", "compatible_prints_condition", "is_custom_defined", "description",
             "renamed_from", "alias"}


def slot_values(preset):
    """The settings of a 3DFP preset that are worth putting on a slot: {key: raw JSON value}."""
    return {k: v for k, v in (preset or {}).items() if k not in META_KEYS and v is not None}


_BAD_FILE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# OrcaSlicer's file audit blocks any path component containing these, case-insensitively.
_BLOCKED_WORDS = ("conf", "cert", "secret")


def preset_file_name(name):
    base = _BAD_FILE_CHARS.sub("_", name).strip(" .") or "3DFP filament"
    for w in _BLOCKED_WORDS:  # e.g. "Confetti" -> "C_onfetti", or OrcaSlicer refuses to write the file
        base = re.sub(w, lambda m: m.group()[0] + "_" + m.group()[1:], base, flags=re.I)
    return base + ".json"


# Sync fields OrcaSlicer keeps in the preset's .info file, never in its .json.
INFO_KEYS = ("setting_id", "base_id", "user_id", "sync_info", "updated_time")


def user_preset_json(preset, app_version=""):
    """3DFP's preset -> the JSON OrcaSlicer keeps for a user filament preset."""
    data = dict(preset)
    data.pop("type", None)
    for k in INFO_KEYS:
        data.pop(k, None)
    name = str(data.get("name") or "3DFP filament")
    data["name"] = name
    data["from"] = "User"
    data["filament_settings_id"] = [name]
    if not data.get("version"):
        data["version"] = app_version or "2.5.0.0"
    return data


def write_user_preset(user_dir, preset, app_version="", backup_dir=None):
    """Write preset into <user_dir>/filament/. Returns (path, replaced_existing)."""
    fdir = os.path.join(user_dir, "filament")
    if not os.path.isdir(fdir):
        os.makedirs(fdir, exist_ok=True)
    data = user_preset_json(preset, app_version)
    path = os.path.join(fdir, preset_file_name(data["name"]))
    replaced = os.path.exists(path)
    if replaced and backup_dir:
        try:
            os.makedirs(backup_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            with open(path, "rb") as src, open(os.path.join(backup_dir, stamp + " " + os.path.basename(path)), "wb") as dst:
                dst.write(src.read())
        except OSError:
            pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=4, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)
    refresh_info(path, replaced)
    return path, replaced


def read_info(path):
    """A preset's .info file ("key = value" lines) -> {key: value}, in file order."""
    info = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            k, sep, v = line.partition("=")
            if sep:
                info[k.strip()] = v.strip()
    return info


def refresh_info(json_path, replaced):
    """Bring the preset's .info in step with a .json the plugin just wrote, so Orca Cloud sync keeps it.

    OrcaSlicer writes the .info itself and records the preset's cloud ID there. On each sync it deletes a local
    preset whose ID the cloud doesn't list, unless it's marked as syncing. It also keeps whichever copy has the newer
    updated_time. So:
    - no .info: leave it to OrcaSlicer, which uploads the preset as new;
    - a .info left over from a removed preset: clear its ID, so it's uploaded as new instead of deleted;
    - an updated preset: newer updated_time and sync_info "update", so the change is uploaded, not overwritten.
    Returns the info written, or None."""
    info_path = os.path.splitext(json_path)[0] + ".info"
    if not os.path.exists(info_path):
        return None
    info = read_info(info_path)
    setting_id = info.get("setting_id", "")
    if not replaced or setting_id in ("", "null"):
        info["setting_id"] = ""
        if info.get("sync_info") != "create":
            info["sync_info"] = ""
    elif info.get("sync_info") != "create":
        info["sync_info"] = "update"
    info["updated_time"] = str(int(time.time()))
    for k in INFO_KEYS:
        info.setdefault(k, "")
    tmp = info_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.writelines("%s = %s\n" % (k, v) for k, v in info.items())
    os.replace(tmp, info_path)
    return info


def pick_base(inherits, ftype, catalog):
    """Choose the slot preset to put 3DFP's values on.

    catalog: {preset name: {"type": filament_type, "system": bool}} of the presets the slot can select.
    Prefers 3DFP's own base, then a visible system preset of the same filament type ("Generic X" first)."""
    inherits = (inherits or "").strip()
    if inherits:
        # OrcaSlicer names some 0.4 presets with a " 0.4 nozzle" suffix and some without
        stem = re.sub(r"\s+\d\.\d+ nozzle$", "", inherits)
        for cand in (inherits, stem, stem + " 0.4 nozzle"):
            if cand in catalog:
                return cand
    ftype = (ftype or "").upper()
    same = [n for n, p in catalog.items() if p.get("system") and (p.get("type") or "").upper() == ftype]
    if not same:
        return None
    def words(n):  # material/variant words only: "Bambu PLA Silk+ @BBL H2C" -> {"pla", "silk"}
        return set(re.findall(r"[a-z0-9.]+", n.split("@")[0].lower())) - {"bambu", "lab", "generic"}
    want = words(inherits)

    def rank(n):
        w = words(n)
        return (-len(want & w), len(w - want), not n.lower().startswith("generic "), len(n), n)
    same.sort(key=rank)
    return same[0]


def first_hex(rgb):
    """'#88BFFF,#DA63C4' -> '#88BFFF'; None when there's no colour."""
    m = re.search(r"#[0-9A-Fa-f]{6}", rgb or "")
    return m.group().upper() if m else None


GCODE_GRAMS = re.compile(r"^; filament used \[g\] = (.+)$", re.M)
GCODE_TOTAL = re.compile(r"^; total filament used \[g\] = ([\d.]+)", re.M)


def gcode_grams(path, head_bytes=80_000, tail_bytes=400_000):
    """Per-filament grams from the stats comments OrcaSlicer writes at the top and bottom of a G-code file."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(head_bytes).decode("utf-8", "replace")
        f.seek(max(0, size - tail_bytes))
        tail = f.read().decode("utf-8", "replace")
    for text in (tail, head):
        m = GCODE_GRAMS.search(text)
        if m:
            return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", m.group(1))]
    for text in (tail, head):
        m = GCODE_TOTAL.search(text)
        if m:
            return [float(m.group(1))]
    return []


def deduct_plan(grams, slot_spools):
    """grams: per-slot list (slot 1 first). slot_spools: {"1": "ABC123"}. -> {short_code: grams} (summed)."""
    out = {}
    for i, g in enumerate(grams, 1):
        code = slot_spools.get(str(i))
        if code and g and g > 0.005:
            out[code] = round(out.get(code, 0.0) + g, 2)
    return out


# --- auto-link: each used slot -> the spool 3DFP has in this printer's AMS (AMS sync, or the user, keeps the
# locations current). The OrcaSlicer plugin API doesn't expose the AMS mapping of a print job, so this matches the
# way OrcaSlicer's own auto-mapping does: same filament type, then the closest colour, then AMS order.
LOCATION_RE = re.compile(r"^\s*Printer:\s*(?P<printer>[^,]+?)\s*,\s*(?P<where>.+?)\s*$", re.I)
AMS_RE = re.compile(r"^AMS:\s*(HT-)?([A-H])-(\d+)$", re.I)


def spool_position(location):
    """'Printer: H2C, AMS: A-1' -> ('H2C', 'A-1', sort key); external spools sort last. None for other places."""
    m = LOCATION_RE.match(location or "")
    if not m:
        return None
    printer, where = m.group("printer"), m.group("where")
    a = AMS_RE.match(where)
    if a:
        ht = bool(a.group(1))
        return printer, ("HT-" if ht else "") + a.group(2).upper() + "-" + a.group(3), (int(ht), a.group(2).upper(), int(a.group(3)))
    n = re.search(r"(\d+)\s*$", where)
    return printer, where, (2, "", int(n.group(1)) if n else 0)


def _word_in(word, text):
    return bool(word) and re.search(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9])", (text or "").lower())


def printer_spools(spools, printer_name, code=""):
    """The 3DFP printer whose name appears in the OrcaSlicer printer preset name (or equals the printer code),
    and its spools as [(label, sort key, spool)]. ("", []) if none or more than one printer matches."""
    groups = {}
    for sp in spools or []:
        pos = spool_position(sp.get("location"))
        if pos:
            groups.setdefault(pos[0], []).append((pos[1], pos[2], sp))
    hits = [p for p in groups if _word_in(p, printer_name) or (code and p.lower() == code.lower())]
    if len(hits) != 1:
        return "", []
    return hits[0], sorted(groups[hits[0]], key=lambda t: t[1])


def _ftype(t):
    return re.sub(r"[\s_]+", "-", str(t or "").strip().upper())


def type_score(slot_type, spool):
    """2: same type (PLA = PLA, or PLA-CF = PLA + CF); 1: same base type (PLA-CF vs PLA); 0: different."""
    st, mat = _ftype(slot_type), _ftype(spool.get("material"))
    if not st or not mat:
        return 0
    if st == mat or st == _ftype("%s-%s" % (spool.get("material"), spool.get("material_type"))):
        return 2
    return 1 if st.split("-")[0] == mat.split("-")[0] else 0


def _rgb(h):
    h = first_hex(h)
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5)) if h else None


def colour_distance(a, b):
    """'Redmean' RGB distance, 0 (same) to about 765. Unknown colours count as far apart."""
    p, q = _rgb(a), _rgb(b)
    if not p or not q:
        return 999.0
    r = (p[0] + q[0]) / 2
    dr, dg, db = p[0] - q[0], p[1] - q[1], p[2] - q[2]
    return ((2 + r / 256) * dr * dr + 4 * dg * dg + (2 + (255 - r) / 256) * db * db) ** 0.5


def link_slots(slots, grams, spools, printer_name, code="", manual=None):
    """Choose a spool for every slot that used filament.
    slots: [{"preset", "color", "type"}] (slot 1 first); grams: per-slot grams; manual: {"1": short code} links the
    user set, which win. -> (links {"1": {"code", "source", "where", "alts"}}, unmatched [{"slot", "why"}])."""
    manual = manual or {}
    links, unmatched, taken = {}, [], set()
    used = [str(i) for i, g in enumerate(grams or [], 1) if g and g > 0.005]
    for k in used:
        if manual.get(k):
            links[k] = {"code": manual[k], "source": "manual", "where": "", "alts": []}
            taken.add(manual[k])
    printer, here = printer_spools(spools, printer_name, code)
    for k in used:
        if k in links:
            continue
        sl = slots[int(k) - 1] if int(k) <= len(slots) else {}
        if not here:
            unmatched.append({"slot": k, "why": "3DFP has no spools in a printer named like “%s”" % (printer_name or code)})
            continue
        cands = []
        for label, key, sp in here:
            if (sp.get("remaining_grams") or 0) <= 0:
                continue
            ts = type_score(sl.get("type"), sp)
            if ts:
                cands.append((-ts, round(colour_distance(sl.get("color"), sp.get("rgb"))), sp.get("short_code") in taken,
                              key, label, sp))
        if not cands:
            unmatched.append({"slot": k, "why": "no %s spool in %s's AMS on 3DFP" % (sl.get("type") or "matching", printer)})
            continue
        cands.sort(key=lambda c: c[:4])
        best = cands[0]
        alts = [c[4] for c in cands[1:] if c[0] == best[0] and abs(c[1] - best[1]) <= 1]
        links[k] = {"code": best[5].get("short_code"), "source": "auto", "where": "%s %s" % (printer, best[4]), "alts": alts}
        taken.add(best[5].get("short_code"))
    return links, unmatched


def links_plan(grams, links):
    return deduct_plan(grams, {k: v["code"] for k, v in (links or {}).items()})


# ---------------------------------------------------------------------------
# docked panel
# ---------------------------------------------------------------------------

# Plugin logo (assets/icon_64.png), embedded so the plugin stays one file. Regenerate with tools/embed_logo.py.
# LOGO-BEGIN
LOGO_PNG_B64 = (
    "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"
)
# LOGO-END

PANEL_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
:root{--bg:var(--orca-bg,#fff);--fg:var(--orca-fg,#1b1f24);--muted:var(--orca-muted,#6b7480);--accent:var(--orca-accent,#009688);
--accent-fg:var(--orca-accent-fg,#fff);--border:var(--orca-border,#e1e4e8);--font:var(--orca-font,system-ui,"Segoe UI",sans-serif);
--card:color-mix(in srgb,var(--fg) 4%,var(--bg));--soft:color-mix(in srgb,var(--accent) 12%,var(--bg));
--err:#d64545;--err-soft:color-mix(in srgb,#d64545 12%,var(--bg));--ok:#1f9d6b;--ok-soft:color-mix(in srgb,#1f9d6b 12%,var(--bg));--warn:#b07800}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:12.5px/1.45 var(--font);display:flex;flex-direction:column;height:100vh}
header{display:flex;align-items:center;gap:8px;padding:10px 12px 0}
.logo{width:30px;height:30px;flex:none;object-fit:contain}
.title{font-weight:700;font-size:14px}.sub{color:var(--muted);font-size:11px}
nav{display:flex;gap:2px;padding:8px 12px 0;border-bottom:1px solid var(--border)}
nav button{flex:1;background:none;border:0;border-bottom:2px solid transparent;color:var(--muted);padding:6px 2px;font:inherit;font-weight:600;cursor:pointer}
nav button.on{color:var(--accent);border-color:var(--accent)}
main{flex:1;overflow:auto;padding:10px 12px}
section{display:none}section.on{display:block}
input,select{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:5px 7px}
input:focus,select:focus{outline:2px solid color-mix(in srgb,var(--accent) 40%,transparent);border-color:var(--accent)}
.row{display:flex;gap:6px;align-items:center}.grow{flex:1;min-width:0}
.btn{font:inherit;font-weight:600;border:0;border-radius:6px;padding:6px 10px;background:var(--accent);color:var(--accent-fg);cursor:pointer;white-space:nowrap}
.btn.ghost{background:var(--card);color:var(--fg);border:1px solid var(--border)}
.btn:disabled{opacity:.5;cursor:default}
.list{margin-top:8px;display:flex;flex-direction:column;gap:6px}
.item{border:1px solid var(--border);border-radius:8px;background:var(--card);padding:7px 9px;cursor:pointer}
.item.open{border-color:var(--accent);cursor:default}
.head{display:flex;gap:8px;align-items:center}
.sw{width:22px;height:22px;border-radius:50%;flex:none;border:1px solid color-mix(in srgb,var(--fg) 25%,transparent)}
.name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.meta{color:var(--muted);font-size:11.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tag{font-size:10.5px;font-weight:600;padding:1px 6px;border-radius:99px;background:var(--soft);color:var(--accent);white-space:nowrap}
.tag.grey{background:var(--card);color:var(--muted);border:1px solid var(--border)}
.det{margin-top:8px;border-top:1px dashed var(--border);padding-top:7px}
dl{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;margin:0 0 8px}dt{color:var(--muted)}dd{margin:0}
.actions{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.empty{color:var(--muted);text-align:center;padding:18px 6px}
.pager{display:flex;justify-content:space-between;align-items:center;margin-top:8px;color:var(--muted)}
.card{border:1px solid var(--border);border-radius:8px;background:var(--card);padding:9px 10px;margin-bottom:10px}
.label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin-bottom:5px}
.slot{display:grid;grid-template-columns:22px 1fr;gap:4px 8px;align-items:center;padding:6px 0;border-bottom:1px solid var(--border)}
.slot:last-child{border:0}.slot select{width:100%}
.field{margin-bottom:9px}.field .t{font-weight:600}.field .d{color:var(--muted);font-size:11.5px;margin-bottom:3px}
.field input[type=text],.field input[type=password],.field select{width:100%}
.check{display:flex;gap:8px;align-items:flex-start;margin-bottom:8px}.check input{margin-top:3px}
.msg{margin-top:8px;border-radius:8px;padding:8px 10px;font-size:12px}
.msg.ok{background:var(--ok-soft)}.msg.err{background:var(--err-soft)}
.msg ul{margin:4px 0 0;padding-left:17px}.msg li{margin:1px 0;overflow-wrap:anywhere}
footer{display:flex;gap:8px;align-items:center;border-top:1px solid var(--border);padding:6px 12px;color:var(--muted);font-size:11px}
.spin{width:11px;height:11px;border-radius:50%;border:2px solid var(--border);border-top-color:var(--accent);animation:r .8s linear infinite;display:none}
.busy .spin{display:inline-block}@keyframes r{to{transform:rotate(1turn)}}
a{color:var(--accent);cursor:pointer;text-decoration:none}
.keywarn{border:1px solid var(--warn);background:color-mix(in srgb,#e0a100 12%,var(--bg));border-radius:8px;padding:8px 10px;margin-bottom:10px}
</style></head><body>
<header><img class="logo" src="data:image/png;base64,__LOGO__" alt=""><div><div class="title">3D Filament Profiles</div><div class="sub" id="printer">-</div></div></header>
<nav><button data-tab="find" class="on">Find</button><button data-tab="spools">My spools</button><button data-tab="slots">Slots</button><button data-tab="settings">Settings</button></nav>
<main>
<div id="nokey" class="keywarn" style="display:none">Add your 3DFP API key in <a data-goto="settings">Settings</a> first. Get it from the API access page in the user menu on 3dfilamentprofiles.com.</div>
<section id="find" class="on">
  <div class="row"><input id="q" class="grow" placeholder="Brand, material, colour or #hex" maxlength="100">
  <select id="mat" title="Material"><option value="">All</option><option>pla</option><option>petg</option><option>abs</option><option>asa</option><option>tpu</option><option>pa</option><option>pc</option><option>pet</option><option>pva</option><option>hips</option></select>
  <button class="btn" id="go">Search</button></div>
  <div id="results" class="list"><div class="empty">Search the 3DFP catalog, then pick a filament to use it in a slot.</div></div>
  <div class="pager" id="pager" style="display:none"><button class="btn ghost" id="prev">&lsaquo; Prev</button><span id="pinfo"></span><button class="btn ghost" id="next">Next &rsaquo;</button></div>
</section>
<section id="spools">
  <div class="row"><input id="sq" class="grow" placeholder="Filter spools"><button class="btn ghost" id="sref">Refresh</button></div>
  <div class="row" style="margin-top:6px"><label class="row"><input type="checkbox" id="sempty"> show empty</label></div>
  <div id="slist" class="list"><div class="empty">Press Refresh to load your spools from 3DFP.</div></div>
</section>
<section id="slots">
  <div class="card"><div class="label">Spool in each filament slot</div><div id="slotlist"></div>
  <div class="row" style="margin-top:8px"><button class="btn ghost" id="slref">Refresh slots</button></div></div>
  <div class="card"><div class="label">Last slice</div><div id="slice" class="meta" style="white-space:normal">Slice a plate to see the grams per slot.</div>
  <div class="row" style="margin-top:8px"><button class="btn" id="deduct" disabled>Deduct from 3DFP spools</button></div></div>
</section>
<section id="settings">
  <div class="field"><div class="t">API key</div><div class="d" id="keyinfo">Starts with 3dfp_. Stored in this plugin's data folder.</div>
    <div class="row"><input type="password" id="key" class="grow" placeholder="3dfp_..." autocomplete="off"><button class="btn" id="savekey">Save</button></div></div>
  <div class="field"><div class="t">Printer for profiles</div><div class="d">Auto uses the printer preset selected now.</div><select id="s_printer"></select></div>
  <div class="field"><div class="t">Nozzle</div><select id="s_nozzle"><option value="auto">auto</option><option>0.2</option><option>0.4</option><option>0.6</option><option>0.8</option></select></div>
  <label class="check"><input type="checkbox" id="s_pa"><span><b>Include pressure advance</b><br><span class="meta" style="white-space:normal">3DFP leaves K out by default because it depends on printer and nozzle.</span></span></label>
  <label class="check"><input type="checkbox" id="s_save"><span><b>Also save as a user preset</b><br><span class="meta" style="white-space:normal">Writes the preset file, so it is in the filament list from the next start.</span></span></label>
  <label class="check"><input type="checkbox" id="s_color"><span><b>Set the slot colour</b> from the filament</span></label>
  <label class="check"><input type="checkbox" id="s_offer"><span><b>Offer to deduct</b> filament from 3DFP spools when a print starts</span></label>
  <label class="check"><input type="checkbox" id="s_auto"><span><b>Deduct without asking</b> when a print starts</span></label>
  <label class="check"><input type="checkbox" id="s_link"><span><b>Link slots to spools automatically</b><br><span class="meta" style="white-space:normal">A slot with no spool picked in Slots uses the spool 3DFP has in this printer's AMS with the same type and closest colour (keep 3DFP locations current, e.g. with AMS sync).</span></span></label>
  <label class="check"><input type="checkbox" id="s_open"><span><b>Open this panel</b> when OrcaSlicer opens a project</span></label>
</section>
<div id="out"></div>
</main>
<footer id="foot"><span class="spin"></span><span id="status" class="grow">Ready</span><span id="quota"></span></footer>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const post=o=>window.orca&&orca.postMessage(o);
let S={slots:[],slot_spools:{},settings:{}},RES=null,PAGE=1,OPEN=null,SPOOLS=[],DET={};
function tab(t){document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.tab===t));
 document.querySelectorAll('section').forEach(s=>s.classList.toggle('on',s.id===t));$('out').innerHTML='';}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>tab(b.dataset.tab));
document.querySelectorAll('[data-goto]').forEach(a=>a.onclick=()=>tab(a.dataset.goto));
function sw(rgb){const c=String(rgb||'').match(/#[0-9a-f]{6}/ig)||[];let bg='repeating-linear-gradient(45deg,#ccc 0 3px,#fff 3px 6px)';
 if(c.length===1)bg=c[0];else if(c.length>1){const s=100/c.length;bg='conic-gradient('+c.map((x,i)=>x+' '+(i*s)+'% '+((i+1)*s)+'%').join(',')+')'}
 return '<span class="sw" style="background:'+bg+'"></span>'}
function slotOptions(){const n=Math.max(1,S.slots.length);let h='';for(let i=1;i<=n;i++){const s=S.slots[i-1];h+='<option value="'+i+'">Slot '+i+(s?' \u2013 '+esc(s.preset).slice(0,28):'')+'</option>'}return h}
function actions(kind,id,url){return '<div class="actions"><select class="slot-pick">'+slotOptions()+'</select>'+
 '<button class="btn" data-act="use" data-kind="'+kind+'" data-id="'+esc(id)+'">Use in slot</button>'+
 '<button class="btn ghost" data-act="save" data-kind="'+kind+'" data-id="'+esc(id)+'" title="Only write the user preset file">Save preset</button>'+
 (url?'<a data-url="'+esc(url)+'">Open on 3DFP</a>':'')+'</div>'}
function wireActions(root){root.querySelectorAll('[data-act]').forEach(b=>b.onclick=e=>{e.stopPropagation();
  const pick=b.parentElement.querySelector('.slot-pick');post({type:b.dataset.act,kind:b.dataset.kind,id:b.dataset.id,slot:+(pick?pick.value:1)})});
 root.querySelectorAll('[data-url]').forEach(a=>a.onclick=e=>{e.stopPropagation();post({type:'open_url',url:a.dataset.url})});}
function t(v,u){return v==null||v===''?'\u2013':esc(v)+(u||'')}
function range(a,b,u){if(a==null&&b==null)return '\u2013';if(a==null)return esc(b)+u;if(b==null||a==b)return esc(a)+u;return esc(a)+'\u2013'+esc(b)+u}
function detail(d){if(!d)return '<div class="meta">Loading details\u2026</div>';
 const price=(d.stores||[]).map(s=>esc(s.currency)+' '+esc(s.price)+(s.availability&&s.availability!=='in_stock'?' ('+esc(s.availability).replace('_',' ')+')':'')).join(', ');
 return '<dl><dt>Nozzle</dt><dd>'+range(d.temp_min,d.temp_max,' \u00b0C')+'</dd><dt>Bed</dt><dd>'+range(d.bed_temp_min,d.bed_temp_max,' \u00b0C')+'</dd>'+
 (d.chamber_temp!=null?'<dt>Chamber</dt><dd>'+t(d.chamber_temp,' \u00b0C')+'</dd>':'')+
 '<dt>Density</dt><dd>'+(d.density?(d.density/1000).toFixed(2)+' g/cm\u00b3':'\u2013')+'</dd><dt>Flow ratio</dt><dd>'+t(d.flow_ratio)+'</dd>'+
 '<dt>Max vol. speed</dt><dd>'+t(d.max_volumetric_speed,' mm\u00b3/s')+'</dd><dt>Dry</dt><dd>'+(d.dry_temp_max?esc(d.dry_temp_max)+' \u00b0C, '+t(d.dry_time,' h'):'\u2013')+'</dd>'+
 (d.td_value!=null?'<dt>TD</dt><dd>'+t(d.td_value)+'</dd>':'')+(price?'<dt>Price</dt><dd>'+price+'</dd>':'')+'</dl>'}
function renderResults(){const box=$('results');if(!RES){return}
 if(!RES.data.length){box.innerHTML='<div class="empty">No filaments found.</div>';$('pager').style.display='none';return}
 box.innerHTML=RES.data.map(f=>{const open=OPEN==='f'+f.id;return '<div class="item'+(open?' open':'')+'" data-fid="'+f.id+'"><div class="head">'+sw(f.rgb)+
  '<div class="grow"><div class="name">'+esc(f.brand_name)+' '+esc(f.material)+(f.material_type?' '+esc(f.material_type):'')+'</div><div class="meta">'+esc(f.color)+(f.rgb?' \u00b7 '+esc(f.rgb):'')+'</div></div>'+
  (f.discontinued?'<span class="tag grey">discontinued</span>':'')+'</div>'+(open?'<div class="det">'+detail(DET[f.id])+actions('filament',f.id,f.url)+'</div>':'')+'</div>'}).join('');
 box.querySelectorAll('.item').forEach(el=>el.onclick=()=>{const id=+el.dataset.fid;if(OPEN==='f'+id)return;OPEN='f'+id;if(!DET[id])post({type:'details',id});renderResults()});
 wireActions(box);const last=RES.data.length<RES.limit;$('pager').style.display=(PAGE>1||!last)?'flex':'none';
 $('prev').disabled=PAGE<=1;$('next').disabled=last||PAGE>=20;$('pinfo').textContent='Page '+PAGE+(RES.total?' of ~'+Math.min(20,Math.ceil(RES.total/RES.limit)):'');}
function search(p){PAGE=p||1;OPEN=null;post({type:'search',q:$('q').value.trim(),material:$('mat').value,page:PAGE})}
$('go').onclick=()=>search(1);$('q').onkeydown=e=>{if(e.key==='Enter')search(1)};$('mat').onchange=()=>search(1);
$('prev').onclick=()=>search(PAGE-1);$('next').onclick=()=>search(PAGE+1);
function spoolLabel(s){return esc(s.brand)+' '+esc(s.material)+(s.material_type?' '+esc(s.material_type):'')+' \u2013 '+esc(s.color)}
function renderSpools(){const box=$('slist');const f=$('sq').value.trim().toLowerCase();
 let rows=SPOOLS.filter(s=>($('sempty').checked||s.remaining_grams>0)&&(!f||[s.short_code,s.brand,s.material,s.material_type,s.color,s.location,s.rgb].join(' ').toLowerCase().includes(f)));
 if(!SPOOLS.length){box.innerHTML='<div class="empty">No spools loaded. Press Refresh.</div>';return}
 if(!rows.length){box.innerHTML='<div class="empty">No spools match.</div>';return}
 box.innerHTML=rows.map(s=>{const open=OPEN==='s'+s.short_code;return '<div class="item'+(open?' open':'')+'" data-code="'+esc(s.short_code)+'"><div class="head">'+sw(s.rgb)+
  '<div class="grow"><div class="name">'+spoolLabel(s)+'</div><div class="meta">'+esc(s.short_code)+' \u00b7 '+esc(s.remaining_grams)+' g'+(s.location?' \u00b7 '+esc(s.location):'')+'</div></div></div>'+
  (open?'<div class="det">'+(s.notes?'<div class="meta" style="white-space:normal;margin-bottom:6px">'+esc(s.notes)+'</div>':'')+actions('spool',s.short_code,s.spool_url)+'</div>':'')+'</div>'}).join('');
 box.querySelectorAll('.item').forEach(el=>el.onclick=()=>{OPEN='s'+el.dataset.code;renderSpools()});wireActions(box);}
$('sref').onclick=()=>post({type:'spools'});$('sq').oninput=renderSpools;$('sempty').onchange=renderSpools;
function renderSlots(){const box=$('slotlist');if(!S.slots.length){box.innerHTML='<div class="meta">No filament slots (open the Prepare tab).</div>';return}
 const opts=c=>'<option value="">\u2014 '+((S.settings||{}).auto_link?'auto':'no spool')+' \u2014</option>'+SPOOLS.filter(s=>s.remaining_grams>0||s.short_code===c).map(s=>'<option value="'+esc(s.short_code)+'"'+(s.short_code===c?' selected':'')+'>'+esc(s.short_code)+' \u00b7 '+spoolLabel(s)+' ('+esc(s.remaining_grams)+' g)</option>').join('')+
  (c&&!SPOOLS.some(s=>s.short_code===c)?'<option value="'+esc(c)+'" selected>'+esc(c)+'</option>':'');
 box.innerHTML=S.slots.map((s,i)=>{const c=S.slot_spools[String(i+1)]||'';return '<div class="slot">'+sw(s.color)+'<div class="meta" title="'+esc(s.preset)+'">Slot '+(i+1)+': '+esc(s.preset)+'</div><span></span><select data-slot="'+(i+1)+'">'+opts(c)+'</select></div>'}).join('');
 box.querySelectorAll('select').forEach(el=>el.onchange=()=>post({type:'map',slot:+el.dataset.slot,code:el.value}));
 const ls=S.last_slice;if(ls&&ls.grams&&ls.grams.length){const L=ls.links||ls.preview||{};$('slice').innerHTML=ls.grams.map((g,i)=>{const l=L[String(i+1)];return 'Slot '+(i+1)+': <b>'+g.toFixed(2)+' g</b>'+(l?' \u2192 '+esc(l.code)+(l.source==='auto'?' <span>(auto, '+esc(l.where)+(l.alts&&l.alts.length?', or '+esc(l.alts.join('/')):'')+')</span>':''):(g>0.005?' \u2192 <span>no spool</span>':''))}).join('<br>')+
  '<br><span>'+esc(ls.when||'')+(ls.deducted?' \u00b7 already deducted':'')+'</span>';$('deduct').disabled=!S.plan_ok||ls.deducted;}
 else{$('slice').textContent='Slice a plate to see the grams per slot.';$('deduct').disabled=true}}
$('slref').onclick=()=>post({type:'state'});$('deduct').onclick=()=>post({type:'deduct'});
function fillSettings(){const st=S.settings||{};const sel=$('s_printer');sel.innerHTML=(S.printer_choices||['auto']).map(p=>'<option'+(p===st.printer?' selected':'')+'>'+esc(p)+'</option>').join('');
 $('s_nozzle').value=st.nozzle||'auto';$('s_pa').checked=!!st.pressure_advance;$('s_save').checked=!!st.save_preset;$('s_color').checked=!!st.set_color;
 $('s_offer').checked=!!st.offer_deduct;$('s_auto').checked=!!st.auto_deduct;$('s_link').checked=!!st.auto_link;$('s_open').checked=!!st.auto_open;
 $('keyinfo').textContent=S.has_key?'Saved key ends in \u2026'+S.key_tail+'. Paste a new one to replace it.':'Starts with 3dfp_. Stored in this plugin\'s data folder.';}
[['s_printer','printer'],['s_nozzle','nozzle']].forEach(([id,k])=>$(id).onchange=()=>post({type:'set',key:k,value:$(id).value}));
[['s_pa','pressure_advance'],['s_save','save_preset'],['s_color','set_color'],['s_offer','offer_deduct'],['s_auto','auto_deduct'],['s_link','auto_link'],['s_open','auto_open']].forEach(([id,k])=>$(id).onchange=()=>post({type:'set',key:k,value:$(id).checked}));
$('savekey').onclick=()=>{const v=$('key').value.trim();if(v){post({type:'set',key:'api_key',value:v});$('key').value=''}};
function showResult(r){const ul=a=>a&&a.length?'<ul>'+a.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':'';
 $('out').innerHTML='<div class="msg '+(r.ok?'ok':'err')+'"><b>'+esc(r.title)+'</b>'+ul(r.lines)+(r.more&&r.more.length?'<details><summary>Details</summary>'+ul(r.more)+'</details>':'')+'</div>';
 $('out').scrollIntoView({block:'end'})}
function onMsg(d){switch(d.type){
 case 'state':S=d;$('printer').textContent=(d.printer_name||'No printer')+' \u00b7 profiles for '+d.printer_code+' / '+d.nozzle+' mm';$('nokey').style.display=d.has_key?'none':'block';
  if(d.spools&&!SPOOLS.length)SPOOLS=d.spools;fillSettings();renderSlots();renderSpools();if(RES)renderResults();break;
 case 'results':RES=d.result;PAGE=d.page;renderResults();break;
 case 'details':DET[d.id]=d.filament;renderResults();break;
 case 'spools':SPOOLS=d.spools||[];renderSpools();renderSlots();break;
 case 'result':showResult(d);break;
 case 'busy':$('foot').classList.toggle('busy',!!d.on);$('status').textContent=d.text||(d.on?'Working\u2026':'Ready');
  document.querySelectorAll('[data-act],#go,#sref,#deduct').forEach(b=>{if(d.on)b.disabled=true;else if(b.id!=='deduct')b.disabled=false});if(!d.on)renderSlots();break;
 case 'quota':$('quota').textContent=d.limit?'API '+d.remaining+'/'+d.limit:'';break;
 case 'refresh':post({type:'state'});break;}}
(function boot(){if(window.orca&&orca.onMessage){orca.onMessage(onMsg);post({type:'hello'})}else setTimeout(boot,50)})();
</script></body></html>"""


# ---------------------------------------------------------------------------
# OrcaSlicer plugin
# ---------------------------------------------------------------------------
try:
    import orca  # only inside OrcaSlicer
except ImportError:
    orca = None

DEFAULTS = {
    "api_key": "",
    "printer": "auto",
    "nozzle": "auto",
    "pressure_advance": False,
    "save_preset": True,
    "set_color": True,
    "offer_deduct": True,
    "auto_deduct": False,
    "auto_link": True,     # match used slots to the spools 3DFP has in this printer's AMS
    "auto_open": True,
    "slot_spools": {},     # {"1": "ABC123"}
    "preset_spools": {},   # {preset name: short code}, to relink a slot when that preset is selected again
}

_storage = []


def _v(x):
    return x() if callable(x) else x


def storage_dir():
    """Per-plugin folder. orca.host.plugin.storage() only answers inside plugin callbacks, so keep the first answer."""
    if not _storage:
        try:
            _storage.append(os.path.realpath(os.fspath(orca.host.plugin.storage())))
        except Exception:  # not inside a plugin callback yet: use the folder OrcaSlicer gives local plugins
            d = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "OrcaSlicer", "orca_plugins",
                             "plugin_data", "threedfp_profiles")
            return os.path.realpath(d)
    return _storage[0]


def data_dir():
    """OrcaSlicer's data folder: storage is <data_dir>/orca_plugins/plugin_data/<key>."""
    p = storage_dir()
    parts = os.path.normpath(p).split(os.sep)
    low = [x.lower() for x in parts]
    if "orca_plugins" in low:
        return os.sep.join(parts[:low.index("orca_plugins")])
    return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "OrcaSlicer")


def _read_json(name, default):
    path = os.path.join(storage_dir(), name)
    if not os.path.exists(path):  # check first: the permission hook asks about paths that don't exist
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(name, value):
    os.makedirs(storage_dir(), exist_ok=True)
    path = os.path.join(storage_dir(), name)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(value, f, indent=1)
    os.replace(path + ".tmp", path)


if orca is not None:

    class ThreeDFPPanel(orca.script.ScriptPluginCapabilityBase):
        """Run (Plugins dialog) opens the panel; lifecycle events feed the spool deduction."""

        def __init__(self):
            super().__init__()
            self.panel = None
            self.auto_opened = False
            self.lock = threading.Lock()
            self.busy = False
            self.client = None
            self.spools = []
            self.last_slice = {}
            self.last_state = None
            self.seen_events = set()

        # ----- identity
        def get_name(self):
            return "3D Filament Profiles"

        # ----- settings (plugin storage, not the shared plugin config: it holds the API key)
        def settings(self):
            s = dict(DEFAULTS)
            s.update(_read_json("settings.json", {}))
            return s

        def set_setting(self, key, value):
            s = self.settings()
            s[key] = value
            _write_json("settings.json", s)
            if key == "api_key":
                self.client = None

        def api(self):
            if self.client is None:
                self.client = Client(self.settings().get("api_key", ""))
            return self.client

        # ----- lifecycle
        def on_load(self):
            storage_dir()  # prime while inside a plugin callback
            self.spools = _read_json("spools.json", [])

        def on_unload(self):
            self.panel = None

        def execute(self):
            storage_dir()
            try:
                self.open_panel()
            except Exception as e:
                return orca.ExecutionResult.failure(orca.PluginResult.RecoverableError, "Couldn't open the panel: %s" % e)
            return orca.ExecutionResult.success("3D Filament Profiles panel opened")

        def on_lifecycle_event(self, event, ctx):
            name = getattr(event, "name", "") or str(event).rsplit(".", 1)[-1]
            if name not in self.seen_events:
                self.seen_events.add(name)
                self.log("event seen: " + name)
            if name == "GCodeExportFinished":
                self._on_gcode(ctx)
            elif name == "PrintJobStarted":
                self._on_print_started()
            elif name in ("ProjectOpened", "NewProject") and not self.auto_opened \
                    and threading.current_thread() is threading.main_thread():
                try:
                    if self.settings().get("auto_open", True):
                        self.open_panel()
                    self.auto_opened = True
                except Exception:
                    pass  # UI not ready yet; try on the next event
            elif name in ("PresetSelected", "PresetChanged", "FilamentChanged", "ProjectOpened"):
                self.post({"type": "refresh"})

        # ----- panel plumbing
        def open_panel(self):
            if self.panel is not None and self.panel.is_open():
                self.panel.show()
                return
            self.panel = orca.host.ui.create_dock_panel(
                html=PANEL_HTML.replace("__LOGO__", LOGO_PNG_B64), title="3DFP", width=340, height=640,
                on_message=self.on_message, on_close=self._on_close, dock="right")

        def _on_close(self):
            self.panel = None

        def post(self, obj):
            p = self.panel
            if p is None:
                return
            try:
                if p.is_open():
                    p.post(obj)
            except Exception as e:
                print("3DFP: panel post failed:", e)

        def result(self, ok, title, lines=(), more=()):
            self.post({"type": "result", "ok": ok, "title": title, "lines": list(lines), "more": list(more)})

        def notify(self, text, warn=False):
            try:
                lvl = orca.host.ui.NotificationLevel
                orca.host.ui.push_notification(lvl.WarningNotificationLevel if warn else lvl.RegularNotificationLevel,
                                               "3DFP: " + text)
            except Exception:
                pass

        # ----- background jobs (HTTP and preset edits must not run on the UI thread)
        def spawn(self, label, fn, *args):
            with self.lock:
                if self.busy:
                    self.result(False, "Still working on the last request, one moment.")
                    return False
                self.busy = True
            self.post({"type": "busy", "on": True, "text": label})

            def run():
                try:
                    fn(*args)
                except ApiError as e:
                    self.result(False, e.friendly(), more=["%s (HTTP %s)" % (e.code, e.status)])
                except Exception as e:
                    self.result(False, "Something went wrong", ["%s: %s" % (type(e).__name__, e)])
                finally:
                    with self.lock:
                        self.busy = False
                    if self.client and self.client.quota:
                        self.post(dict(type="quota", **self.client.quota))
                    self.post({"type": "busy", "on": False})
                    self.post({"type": "refresh"})

            threading.Thread(target=run, name="3dfp-worker", daemon=True).start()
            return True

        # ----- reading OrcaSlicer state (UI thread)
        def host_state(self):
            s = self.settings()
            st = {"printer_name": "", "printer_code": "ANY", "nozzle": "0.4", "slots": [], "catalog": {},
                  "all_filaments": [], "user_dir": "", "app_version": "", "signed_in": False}
            try:
                info = orca.host.app_info() or {}
                st["app_version"] = str(info.get("version", "") or "")
                st["signed_in"] = bool(info.get("is_signed_in"))
            except Exception:
                pass
            try:
                b = orca.host.preset_bundle()
            except Exception:
                b = None
            if b is not None:
                try:
                    pp = b.current_printer_preset()
                    st["printer_name"] = pp.name
                    model = pp.config_value("printer_model")
                    st["printer_code"] = printer_code(model, pp.name, _v(getattr(pp, "inherits", "")) or "")
                    st["nozzle"] = nozzle_code(pp.config_value("nozzle_diameter") or pp.name)
                except Exception:
                    pass
                try:
                    names = list(b.current_filament_preset_names())
                    try:
                        colors = as_list(b.full_config_value("filament_colour"))
                    except Exception:
                        colors = []
                    try:
                        types = as_list(b.full_config_value("filament_type"))
                    except Exception:
                        types = []
                    if len(types) < len(names):  # fall back to each preset's own type
                        types = list(types) + [self._preset_type(b, n) for n in names[len(types):]]
                    st["slots"] = [{"preset": n, "color": colors[i] if i < len(colors) else "", "type": types[i] or ""}
                                   for i, n in enumerate(names)]
                except Exception:
                    pass
                st["catalog"] = self._catalog(b)
                try:  # every filament preset, hidden ones too: valid parents for a saved user preset
                    st["all_filaments"] = list(b.filaments.preset_names())
                except Exception:
                    pass
                st["user_dir"] = self._user_dir(b, st["signed_in"])
            if s.get("printer") not in (None, "", "auto"):
                st["printer_code"] = s["printer"]
            if s.get("nozzle") not in (None, "", "auto"):
                st["nozzle"] = s["nozzle"]
            return st

        def _preset_type(self, b, name):
            try:
                return (as_list(b.filaments.find_preset(name).config_value("filament_type")) or [""])[0]
            except Exception:
                return ""

        def _catalog(self, b):
            out = {}
            try:
                coll = b.filaments
                for i in range(coll.size()):
                    p = coll.preset(i)
                    if not (_v(p.is_visible) and _v(p.is_compatible)):
                        continue
                    try:
                        ft = (as_list(p.config_value("filament_type")) or [""])[0]
                    except Exception:
                        ft = ""
                    out[p.name] = {"type": ft, "system": bool(_v(p.is_system))}
            except Exception:
                pass
            return out

        def _user_dir(self, b, signed_in):
            """Folder holding the active user's presets: <data_dir>/user/<id or 'default'>."""
            root = os.path.join(data_dir(), "user")
            try:  # an existing user preset shows where they live
                for coll in (b.filaments, b.prints, b.printers):
                    for i in range(coll.size()):
                        p = coll.preset(i)
                        if _v(p.is_user) and getattr(p, "file", ""):
                            d = os.path.dirname(os.path.dirname(os.fspath(p.file)))
                            if os.path.basename(os.path.dirname(d)).lower() == "user":
                                return d
            except Exception:
                pass
            if not signed_in:
                return os.path.join(root, "default")
            try:
                ids = [d for d in os.listdir(root) if d != "default" and os.path.isdir(os.path.join(root, d))]
            except OSError:
                ids = []
            if ids:
                return max((os.path.join(root, d) for d in ids), key=os.path.getmtime)
            return os.path.join(root, "default")

        def push_state(self):
            s = self.settings()
            st = self.host_state()
            slot_spools = dict(s.get("slot_spools") or {})
            changed = False
            for i, sl in enumerate(st["slots"], 1):  # relink a slot whose preset we know a spool for
                code = (s.get("preset_spools") or {}).get(sl["preset"])
                if code and str(i) not in slot_spools:
                    slot_spools[str(i)] = code
                    changed = True
            for k in list(slot_spools):
                if not k.isdigit() or int(k) > max(1, len(st["slots"])):
                    del slot_spools[k]
                    changed = True
            if changed:
                self.set_setting("slot_spools", slot_spools)
            key = s.get("api_key") or ""
            self.last_state = st if st.get("slots") else self.last_state
            ls = self.last_slice
            if ls.get("grams") and not ls.get("deducted"):
                if s.get("auto_link", True):
                    links, unmatched = link_slots(ls.get("slots") or st["slots"], ls["grams"], self.spools,
                                                  ls.get("printer_name") or st["printer_name"],
                                                  ls.get("printer_code") or st["printer_code"], slot_spools)
                else:
                    links, _ = link_slots([], ls["grams"], [], "", "", slot_spools)
                ls["preview"] = {k: {"code": v["code"], "source": v["source"], "where": v["where"], "alts": v["alts"]}
                                 for k, v in links.items()}
            plan = links_plan(ls.get("grams") or [], ls.get("preview") or {})
            self.post({
                "type": "state", "printer_name": st["printer_name"], "printer_code": st["printer_code"],
                "nozzle": st["nozzle"], "slots": st["slots"], "slot_spools": slot_spools,
                "settings": {k: s[k] for k in ("printer", "nozzle", "pressure_advance", "save_preset", "set_color",
                                               "offer_deduct", "auto_deduct", "auto_link", "auto_open")},
                "printer_choices": PRINTER_CHOICES, "has_key": bool(key), "key_tail": key[-4:] if key else "",
                "spools": self.spools, "last_slice": {k: v for k, v in ls.items() if k in ("grams", "when", "deducted", "preview", "links")},
                "plan_ok": bool(plan) or bool(ls.get("grams") and s.get("auto_link", True) and key),
            })
            if self.client and self.client.quota:
                self.post(dict(type="quota", **self.client.quota))

        # ----- messages from the panel (UI thread)
        def on_message(self, d):
            t = (d or {}).get("type")
            try:
                if t in ("hello", "state"):
                    self.push_state()
                elif t == "set":
                    key = d.get("key")
                    if key in DEFAULTS and key not in ("slot_spools", "preset_spools"):
                        val = d.get("value")
                        if key == "api_key":
                            val = str(val or "").strip()
                            if val and not val.startswith("3dfp_"):
                                self.result(False, "That doesn't look like a 3DFP API key (they start with 3dfp_).")
                                return
                        self.set_setting(key, val)
                        if key == "api_key":
                            self.result(True, "API key saved.")
                    self.push_state()
                elif t == "search":
                    self.spawn("Searching 3DFP\u2026", self.job_search, str(d.get("q") or ""),
                               str(d.get("material") or ""), int(d.get("page") or 1))
                elif t == "details":
                    self.spawn("Loading filament\u2026", self.job_details, int(d["id"]))
                elif t == "spools":
                    self.spawn("Loading your spools\u2026", self.job_spools)
                elif t in ("use", "save"):
                    st = self.host_state()
                    slot = int(d.get("slot") or 1)
                    self.spawn("Fetching the OrcaSlicer profile\u2026", self.job_profile, t, d.get("kind"),
                               str(d.get("id")), slot, st)
                elif t == "map":
                    s = self.settings()
                    m = dict(s.get("slot_spools") or {})
                    code = str(d.get("code") or "")
                    slot = str(int(d.get("slot") or 0))
                    if code:
                        m[slot] = code
                    else:
                        m.pop(slot, None)
                    self.set_setting("slot_spools", m)
                    st = self.host_state()  # remember preset -> spool too
                    i = int(slot) - 1
                    if 0 <= i < len(st["slots"]):
                        ps = dict(s.get("preset_spools") or {})
                        if code:
                            ps[st["slots"][i]["preset"]] = code
                        else:
                            ps.pop(st["slots"][i]["preset"], None)
                        self.set_setting("preset_spools", ps)
                    self.push_state()
                elif t == "deduct":
                    self.start_deduct(manual=True)
                elif t == "open_url":
                    url = str(d.get("url") or "")
                    if url.startswith(("https://3dfilamentprofiles.com/", "https://spooldb.com/", "https://3dfp.to/")):
                        import webbrowser
                        webbrowser.open(url)
            except Exception as e:
                self.result(False, "Something went wrong", ["%s: %s" % (type(e).__name__, e)])

        # ----- jobs (worker thread)
        def job_search(self, q, material, page):
            r = self.api().search(q=q, material=material, page=page)
            self.post({"type": "results", "result": r, "page": page})

        def job_details(self, fid):
            self.post({"type": "details", "id": fid, "filament": self.api().filament(fid)})

        def job_spools(self):
            r = self.api().spools() or {}
            self.spools = [{k: sp.get(k) for k in ("short_code", "spool_url", "brand", "material", "material_type", "color",
                                                   "rgb", "notes", "location", "remaining_grams", "filament_id")}
                           for sp in (r.get("data") or [])]
            self.spools.sort(key=lambda sp: (sp.get("brand") or "", sp.get("material") or "", sp.get("color") or ""))
            try:
                _write_json("spools.json", self.spools)
            except OSError:
                pass
            self.post({"type": "spools", "spools": self.spools})

        def job_profile(self, action, kind, ident, slot, st):
            s = self.settings()
            args = dict(printer=st["printer_code"], nozzle=st["nozzle"], pressure_advance=bool(s.get("pressure_advance")))
            if kind == "spool":
                prof = self.api().spool_profile(ident, **args)
                rgb = next((sp.get("rgb") for sp in self.spools if sp.get("short_code") == ident), None)
            else:
                prof = self.api().filament_profile(int(ident), **args)
                rgb = None
            preset = (prof or {}).get("preset") or {}
            if not preset.get("name"):
                raise ApiError(200, "bad_response", "3DFP didn't return a preset")
            name = preset["name"]
            lines, more = [], []
            for a in prof.get("applied") or []:
                more.append("%s = %s  (from %s)" % (a.get("label") or a.get("key"), a.get("value"), a.get("source")))
            for o in prof.get("omitted") or []:
                more.append("left out %s = %s: %s %s" % (o.get("source"), o.get("value"), o.get("reason"), o.get("detail") or ""))
            more += ["note: " + w for w in prof.get("warnings") or []]

            ftype = (as_list(preset.get("filament_type")) or [""])[0]
            inherits = prof.get("inherits") or preset.get("inherits") or ""
            base = pick_base(inherits, ftype, st["catalog"])

            saved = None
            if action == "save" or s.get("save_preset"):
                file_preset = dict(preset)
                known = set(st.get("all_filaments") or [])
                if known and inherits not in known:  # OrcaSlicer drops a user preset whose parent is missing
                    parent = base or pick_base(inherits, ftype, {n: {"type": ftype, "system": True} for n in known})
                    if parent:
                        file_preset["inherits"] = parent
                        more.append("OrcaSlicer has no “%s”, so the saved preset inherits “%s”." % (inherits, parent))
                if not st["user_dir"]:
                    lines.append("Couldn't find the OrcaSlicer user preset folder, preset not saved.")
                else:
                    try:
                        path, replaced = write_user_preset(st["user_dir"], file_preset, st["app_version"],
                                                           os.path.join(storage_dir(), "backups"))
                        saved = path
                        lines.append(("Updated" if replaced else "Saved") + " user preset \u201c%s\u201d. It shows in the "
                                     "filament list after OrcaSlicer restarts." % name)
                        more.append("file: " + path)
                    except PermissionError:
                        lines.append("OrcaSlicer blocked writing the preset file.")
                    except OSError as e:
                        lines.append("Couldn't write the preset file: %s" % e)

            if action == "save":
                self.result(saved is not None, ("Saved " if saved else "Not saved: ") + name, lines, more)
                return

            # apply to the slot now, on top of a system preset the slot can select
            if not base:
                lines.insert(0, "No %s system preset is available for this printer, so it wasn't applied to a slot. "
                                "Tick one in the filament selection, or restart to use the saved preset." % (ftype or "matching"))
                self.result(False, "Not applied: " + name, lines, more)
                return
            if base != inherits:
                more.append("3DFP's base \u201c%s\u201d isn't in this slot's list (tick it in the filament selection "
                            "to use it); used \u201c%s\u201d." % (inherits, base))
            values = slot_values(preset)
            colour = first_hex(rgb) or first_hex(",".join(as_list(preset.get("default_filament_colour"))))
            if s.get("set_color") and colour:  # OrcaSlicer's filament presets carry the slot colour here
                values["default_filament_colour"] = [colour]
            applied, skipped = self.apply_to_slot(slot, base, values, st["catalog"])
            if kind == "spool":  # this slot now holds that spool
                m = dict(s.get("slot_spools") or {})
                m[str(slot)] = ident
                self.set_setting("slot_spools", m)
                ps = dict(self.settings().get("preset_spools") or {})
                ps[base] = ident
                ps[name] = ident
                self.set_setting("preset_spools", ps)
            lines.insert(0, "Slot %d now uses \u201c%s\u201d with %d 3DFP setting%s (unsaved edit: save it from the "
                            "filament settings to keep it in this session)." % (slot, base, len(applied), "" if len(applied) == 1 else "s"))
            if skipped:
                lines.append("%d setting%s couldn't be applied (see details)." % (len(skipped), "" if len(skipped) == 1 else "s"))
                more += ["skipped %s: %s" % (k, e) for k, e in skipped]
            self.result(True, name, lines, more)

        def apply_to_slot(self, slot, base, values, catalog):
            """Select base in the slot, then stage and commit all values; fall back to one key at a time."""
            ed = orca.host.preset.filament
            base_counts = {}
            try:
                bp = orca.host.preset_bundle().filaments.find_preset(base)
                for k, v in values.items():
                    try:
                        bv = bp.config_value(k)
                        base_counts[k] = len(bv) if isinstance(bv, (list, tuple)) else len(as_list(bv))
                    except Exception:
                        pass
            except Exception:
                pass
            ser = {k: setting_string(v, base_counts.get(k, 0)) for k, v in values.items()}
            tx = ed.begin_update(slot, preset_name=base)
            for k, v in ser.items():
                tx.set(k, v)
            try:
                r = tx.commit()
                return list(getattr(r, "keys", None) or ser.keys()), []
            except ValueError:
                pass  # one bad key rejects the batch: apply the rest one by one
            applied, skipped = [], []
            for k, v in ser.items():
                try:
                    ed.update(slot, k, v)
                    applied.append(k)
                except Exception as e:
                    skipped.append((k, str(e)[:160]))
            return applied, skipped

        # ----- filament use -> 3DFP spools
        def log(self, text):
            """events.log in the plugin's data folder: slices, print starts, links and deductions, for checking later."""
            try:
                path = os.path.join(storage_dir(), "events.log")
                if os.path.exists(path) and os.path.getsize(path) > 256_000:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        keep = f.read()[-128_000:]
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(keep[keep.find("\n") + 1:])
                with open(path, "a", encoding="utf-8") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text.replace("\n", " ") + "\n")
            except Exception:
                pass

        def _state_safe(self):
            try:
                st = self.host_state()
                if st.get("slots"):
                    self.last_state = st
                    return st
            except Exception:
                pass
            return self.last_state or {"slots": [], "printer_name": "", "printer_code": "ANY"}

        def _on_gcode(self, ctx):
            ok = getattr(getattr(orca, "LifecycleEvtCode", None), "Ok", None)
            path = str(getattr(ctx, "msg", "") or "")
            if (ok is not None and getattr(ctx, "code", ok) != ok) or not path.lower().endswith(".gcode") or "\n" in path:
                return
            try:
                grams = gcode_grams(path)
            except OSError as e:
                self.log("slice: couldn't read the G-code (%s: %s)" % (type(e).__name__, e))
                return
            if grams:
                st = self._state_safe()
                self.last_slice = {"grams": grams, "when": time.strftime("sliced %H:%M"), "deducted": False,
                                   "slots": st.get("slots") or [], "printer_name": st.get("printer_name", ""),
                                   "printer_code": st.get("printer_code", "")}
                self.log("slice: %s g on %s" % (", ".join("%.2f" % g for g in grams), st.get("printer_name") or "?"))
                self.post({"type": "refresh"})
            else:
                self.log("slice: no filament stats in " + os.path.basename(path))

        def _on_print_started(self):
            s = self.settings()
            if not (s.get("auto_deduct") or s.get("offer_deduct")):
                return
            if not self.last_slice.get("grams"):
                self.log("print started: no slice in this session, nothing to deduct")
                self.notify("a print started, but there's no slice from this session to deduct.", warn=True)
                return
            if self.last_slice.get("deducted"):
                self.log("print started: this slice was already deducted")
                return
            threading.Thread(target=self._print_started_job, name="3dfp-print", daemon=True).start()

        def _print_started_job(self):
            try:
                plan, links, unmatched = self.resolve_links(refresh=True)
            except ApiError as e:
                self.log("print started: %s" % e.friendly())
                self.notify("couldn't read your spools, nothing deducted: " + e.friendly(), warn=True)
                return
            except Exception as e:
                self.log("print started: %s: %s" % (type(e).__name__, e))
                return
            self.log("print started: links %s; plan %s%s" % (
                json.dumps({k: [v["code"], v["source"], v["where"]] + (["or " + "/".join(v["alts"])] if v["alts"] else [])
                            for k, v in links.items()}),
                json.dumps(plan), "; unmatched " + json.dumps(unmatched) if unmatched else ""))
            self.post({"type": "refresh"})
            if unmatched:
                self.notify("not deducted for " + "; ".join("slot %s (%s)" % (u["slot"], u["why"]) for u in unmatched)
                            + ". Pick the spool in the 3DFP panel's Slots tab.", warn=True)
            if not plan:
                return
            if self.settings().get("auto_deduct"):
                self.start_deduct(manual=False)
                return
            text = ", ".join("%.1f g from %s" % (g, self._spool_name(c, links)) for c, g in plan.items())
            try:
                lvl = orca.host.ui.NotificationLevel
                orca.host.ui.push_notification(lvl.RegularNotificationLevel, "3DFP: deduct " + text + "?",
                                               hyper_text="Deduct", on_click=lambda: (self.start_deduct(False), True)[1])
            except Exception as e:
                self.log("couldn't show the deduct notification: %s" % e)

        def _spool_name(self, code, links=None):
            sp = next((x for x in self.spools if x.get("short_code") == code), None) or {}
            where = next((v["where"] for v in (links or {}).values() if v["code"] == code and v["where"]), "")
            name = " ".join(str(x) for x in (sp.get("material"), sp.get("material_type"), sp.get("color")) if x)
            info = ", ".join(x for x in (name, where) if x)
            return "%s (%s)" % (code, info) if info else code

        def resolve_links(self, refresh):
            """Links for the last slice: the user's links first, then auto-links from the spools' 3DFP locations.
            refresh: reload the spool list first (1 request), since AMS changes move spools. Worker thread only."""
            s = self.settings()
            ls = self.last_slice
            grams = ls.get("grams") or []
            manual = dict(s.get("slot_spools") or {})
            need = [str(k) for k, g in enumerate(grams, 1) if g > 0.005 and not manual.get(str(k))]
            if not s.get("auto_link", True):
                links, _ = link_slots([], grams, [], "", "", manual)
                unmatched = [{"slot": k, "why": "no spool linked"} for k in need]
            else:
                if need and refresh and s.get("api_key"):
                    self.job_spools()
                links, unmatched = link_slots(ls.get("slots") or [], grams, self.spools, ls.get("printer_name", ""),
                                              ls.get("printer_code", ""), manual)
            plan = links_plan(grams, links)
            ls.update(links=links, unmatched=unmatched, plan=plan)
            return plan, links, unmatched

        def start_deduct(self, manual):
            if self.last_slice.get("deducted"):
                if manual:
                    self.result(False, "This slice was already deducted.")
                return
            if not self.last_slice.get("grams"):
                if manual:
                    self.result(False, "Nothing to deduct: slice first.")
                return
            self.spawn("Updating your 3DFP spools…", self.job_deduct, manual)

        def job_deduct(self, manual):
            if not manual and self.last_slice.get("plan"):  # resolved when the print started
                plan = self.last_slice["plan"]
                links = self.last_slice.get("links") or {}
                unmatched = self.last_slice.get("unmatched") or []
            else:
                plan, links, unmatched = self.resolve_links(refresh=True)
            if not plan:
                self.log("deduct: nothing to deduct; unmatched " + json.dumps(unmatched))
                self.result(False, "Nothing to deduct", ["Slot %s: %s" % (u["slot"], u["why"]) for u in unmatched]
                            or ["No slot used filament."])
                return
            r = self.api().deduct(plan) or {}
            ok_lines, bad = [], []
            for e in r.get("data") or []:
                if e.get("error"):
                    bad.append("%s: %s" % (e.get("short_code"), (e["error"] or {}).get("message")))
                else:
                    ok_lines.append("%s: −%s g, %s g left" % (self._spool_name(e.get("short_code"), links),
                                                                   e.get("deducted_grams"), e.get("remaining_grams")))
                    for sp in self.spools:
                        if sp.get("short_code") == e.get("short_code"):
                            sp["remaining_grams"] = e.get("remaining_grams")
            self.log("deduct (%s): %s" % ("button" if manual else "print start", "; ".join(ok_lines + bad) or "no answer"))
            if ok_lines:
                self.last_slice["deducted"] = True
                try:
                    _write_json("spools.json", self.spools)
                except OSError:
                    pass
                self.post({"type": "spools", "spools": self.spools})
            more = ["Slot %s not deducted: %s" % (u["slot"], u["why"]) for u in unmatched]
            self.result(not bad, "Spools updated" if not bad else "Some spools weren't updated", ok_lines + bad, more)
            if not manual:
                self.notify("deducted " + "; ".join(ok_lines) if ok_lines else "spool update failed: " + "; ".join(bad),
                            warn=bool(bad))

    @orca.plugin
    class ThreeDFPPlugin(orca.base):
        def register_capabilities(self):
            import tempfile
            roots = []
            for base in (tempfile.gettempdir(), os.path.realpath(tempfile.gettempdir())):
                p = os.path.join(base, "orcaslicer_model") + os.sep + "*"
                if p not in roots:
                    roots.append(p)
            # HTTPS to 3DFP (a scheme + host grant covers every path), worker threads, and reading the
            # G-code OrcaSlicer slices into its temp folder (for the grams used per slot).
            for kwargs in ({"network_http": [SITE], "threading": True, "fs_read": roots},
                           {"network_http": [SITE], "threading": True}):
                try:
                    orca.request_permissions(**kwargs)
                    break
                except (TypeError, AttributeError, ValueError):
                    continue
            orca.register_capability(ThreeDFPPanel)


if __name__ == "__main__" and orca is None:
    # Command line, for testing the API side: python threedfp_profiles.py <api key> search <text>
    #                                          python threedfp_profiles.py <api key> profile <filament id> [printer] [nozzle]
    import sys
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(0)
    c = Client(sys.argv[1])
    cmd = sys.argv[2]
    try:
        if cmd == "search":
            for f in c.search(" ".join(sys.argv[3:]))["data"]:
                print(f["id"], f["brand_name"], f["material"], f.get("material_type") or "", "-", f["color"], f.get("rgb") or "")
        elif cmd == "profile":
            p = c.filament_profile(int(sys.argv[3]), *(sys.argv[4:6]))
            print(json.dumps(p, indent=2))
        elif cmd == "spools":
            for sp in c.spools()["data"]:
                print(sp["short_code"], sp["brand"], sp["material"], sp.get("material_type") or "", sp["color"], sp["remaining_grams"], "g")
        print("quota:", c.quota)
    except ApiError as e:
        print("error:", e.status, e.code, "-", e.friendly())
        sys.exit(1)
