<img src="assets/icon.png" alt="3D Filament Profiles plugin logo" width="96" align="right">

# 3D Filament Profiles plugin for OrcaSlicer 2.5

Connects OrcaSlicer to [3dfilamentprofiles.com](https://3dfilamentprofiles.com) via its API
([docs](https://3dfilamentprofiles.com/docs/api)), a paid subscription add-on to a 3DFP account. Changes are listed in [CHANGELOG.md](CHANGELOG.md). It's a single file, `threedfp_profiles.py`, with no dependencies beyond Python's standard library.

> Unofficial community plugin, not affiliated with 3D Filament Profiles or OrcaSlicer. Review generated filament
> settings before printing: they come from community data.

**Requirements**
- OrcaSlicer **2.5 or newer**, which has the Python plugin system.
- A 3DFP API key. API access is a **paid, subscription-based add-on** to a 3dfilamentprofiles.com account (some Patreon
  tiers include it). A free 3DFP account has no API access, and the plugin can't do anything without a key.

It was developed and tested on Windows with a Bambu Lab H2C. The printer detection also covers the X1/P1/P2S/A1/A2L/H2/X2D
families, and other printers get 3DFP's printer-neutral ("ANY") profile.

## What it does

A **3DFP** panel docks on the right of the Prepare/Preview tabs:

| Tab | |
|---|---|
| **Find** | Search the 3DFP catalog (brand, material, colour, hex). Click a filament for temps, density, flow ratio, max volumetric speed, price. |
| **Spools** | Your 3DFP spools with grams left and location. **low stock** lists the spools under the low stock weight (150 g by default), lowest first; **Totals** shows the filament left per material. Open a spool for **Mark empty**. |
| **Slots** | Link each filament slot to a spool; **Load my AMS**; the grams per slot from the last slice and its checks; **Deduct from 3DFP spools** and **Undo last deduction**. |
| **History** | Every deduction, spool marked empty and undo, newest first, each with **Undo**. |
| **Settings** | API key, printer/nozzle override (auto-detected from the printer preset, e.g. H2C / P2S, 0.4), pressure advance, options below. |

**Use in slot N** (for a catalog filament or one of your spools):
1. Asks 3DFP for its OrcaSlicer preset for your printer + nozzle (for a spool, your calibrated temps/flow/K are used).
2. Selects the matching system preset in that slot (e.g. `Bambu PLA Basic @BBL H2C`, or a Generic preset of the
   same type if that one isn't enabled) and applies the 3DFP values on top. It shows as an unsaved edit, and you can save it from the filament settings.
3. Also writes it as a user preset (`%APPDATA%\OrcaSlicer\user\<id>\filament\<name>.json`), which shows up in the filament list
   **after OrcaSlicer restarts**. If a file with that name already exists, the old one is backed up to the plugin's data folder first.
   If you're signed in to Orca Cloud, OrcaSlicer uploads a new preset at that restart. An updated preset is marked in its `.info`
   file so the change is uploaded too, rather than replaced by the older cloud copy.

**Save preset** only does step 3.

**Spool deduction:** after slicing, the grams per slot are read from the G-code. When a print is sent, the plugin
subtracts them from the slots' spools and shows the result as a notification. This is on by default. If you turn off **Deduct
without asking** in Settings, you get a notification with a **Deduct** link instead, or you can use the **Deduct from 3DFP spools**
button in Slots. The deduction itself is one API request.

Which spool a slot uses:
1. The spool you picked for it in **Slots** (or set with **Use in slot** on a spool), if any.
2. Otherwise (**Link slots to spools automatically**, on by default), the spool your 3DFP spool list places in the
   selected printer's AMS. It must have the same filament type, and the closest colour wins. On a tie, the first AMS
   position wins, and the other spool is shown as an alternative. The plugin re-reads your spools when the print starts
   (one more request), so the 3DFP locations need to be current. [3DFP AMS Sync](#3dfp-ams-sync) keeps
   them current, or you can set them on 3DFP by hand. 3DFP locations look like `Printer: H2C, AMS: A-1`, `Printer: H2C, AMS: HT-A-1` or
   `Printer: P2S, External Spool`. The printer name (`H2C`) must appear in the OrcaSlicer printer preset name.
3. A slot that used filament but gets no spool isn't deducted, and a warning notification says which slot and why.

The plugin can't see the AMS mapping you confirm in the send dialog: the OrcaSlicer plugin API doesn't expose it. If you
remap a slot to a different tray there, pick that spool in **Slots** before sending.

**Slot names:** OrcaSlicer numbers its filament slots 1, 2, 3…, which don't say where the spool is. The panel adds the position,
named as in your 3DFP locations, so `Slot 1 (AMS: A-2)` is the spool in AMS A-2. For a slot you linked by hand it's that spool's
position; otherwise it's the spool auto-link would pick for the slot (marked *auto*, and with *or* when two spools match
equally). A slot you linked by hand to a spool that 3DFP has outside this printer shows that spool's location (such as
`Shelf: To Review`) in the warning colour: pick the loaded spool, or press **Clear links** to remove all hand-picked links so
auto-link takes over. The Slots list, the **Use in slot** picker, *Last slice* and the slice-check warnings all use these names.

With auto-link on, the plugin doesn't link a slot to a spool just because it uses a preset that was linked to that spool
before (one preset is often in several slots). With auto-link off it does, so a slot keeps its spool when you pick the same
preset again.

**Slice checks** (on by default, **Check each slice against your spools** in Settings): after each slice, the plugin looks at the
spool each used slot would be deducted from and warns, in a notification and under *Last slice* in Slots, when:
- the spool doesn't have enough filament for the print plus a spare margin (**Spare filament**, 10 g by default). A spool used by
  several slots is checked against their total;
- the spool's material doesn't match the slot's filament type (PLA slot, PETG spool), or its colour is clearly different;
- a slot you linked by hand has a spool that 3DFP has somewhere other than this printer (on a shelf, in the other printer), which
  usually means the spool was swapped since;
- a linked spool isn't in your 3DFP spools any more.

It uses the spool list the plugin already has, and reloads it (one request) only when it's more than an hour old.

**Load my AMS** (Slots) sets the filament slots from the spools 3DFP has in the selected printer: slot 1 gets the spool in AMS
A-1, slot 2 the next AMS position, and so on (AMS HT and external spools last). Each slot gets that spool's 3DFP preset, as
with **Use in slot**, and its colour. With auto-link on, any link you set by hand on those slots is removed, because auto-link finds the
spools from their locations; with it off, the slots are linked to the spools. If OrcaSlicer has fewer filament slots than the AMS
has spools, the result says how many to add. Press it twice: the first press asks you to confirm. It costs one request for the
spool list plus one per spool.

**Undo** puts back what a deduction or **Mark empty** took off. It adds the grams to the spool's weight on 3DFP now, so
anything deducted since stays deducted. A spool that 3DFP moved to *Empty* goes back to its location from before. If you undo the
last slice's deduction, you can deduct it again, e.g. after linking the right spool. History is kept in `history.json` in the
plugin's data folder (the last 300 entries); deductions logged before 1.2.0 are imported from `events.log`, without their locations.

**Mark empty** sets a spool to 0 g; 3DFP then applies your empty-spool setting, as on the website (it moves to *Empty*).

Slices, slice checks, print starts, the links chosen and each deduction are logged to `events.log` in the plugin's data folder.

## 3DFP AMS Sync

[**3DFP AMS Sync**](https://github.com/macdat-git/3dfp-ams-sync) is a separate, optional companion service for Windows. It watches
your Bambu Lab printers over the local network and moves spools between 3DFP locations as you load and unload the AMS, so the
auto-link above always finds the right spool. It also adds new Bambu RFID spools to your 3DFP spools. To get it:

1. Download it from GitHub (**Code → Download ZIP** on the [repository page](https://github.com/macdat-git/3dfp-ams-sync)) and unpack it, or
   run `git clone https://github.com/macdat-git/3dfp-ams-sync.git`.
2. Follow the setup in its README: Python 3.10+, your printers' access codes, and a 3DFP location for each AMS slot. It reads the API key
   from this plugin's settings, so you don't enter it twice.
3. Start it in dry run mode, check what it would do, then switch it to live and add the logon task.

While the service runs, it leaves an `ams_sync.json` file in this plugin's data folder, and Settings then shows **Add new RFID spools to
3DFP** (on by default). The plugin only stores that choice, as `ams_add_spools` in `settings.json`; the service reads it before
adding a spool. Without the service the setting is hidden and does nothing.

## Install

1. OrcaSlicer → main menu → **Plugins** → the arrow on **Browse plugins** → **Install local plugin** → pick `threedfp_profiles.py`.
2. Approve the permission prompt: HTTPS to `3dfilamentprofiles.com`, worker threads, and read access to Orca's temp slice folder.
3. Select the plugin and press **Run** to open the panel. After that it opens by itself when a project opens; you can turn that off in Settings.
4. **Settings** → paste your API key (`3dfp_…`, from the *API access* page in the 3DFP user menu; the API is a paid subscription add-on, or included in some Patreon tiers).

To update the plugin, copy the new file over the installed copy at
`%APPDATA%\OrcaSlicer\orca_plugins\threedfp_profiles.py\threedfp_profiles.py` and restart OrcaSlicer. That keeps the approved
permissions. Using **Install local plugin** again also works, but it resets the permissions and you'll be asked to approve them again.

The panel shares the right-hand dock with other plugin panels (SlicePilot, MakerWorld). Drag its edge to make it taller.

## API quota

Searches only run when you press Search or Enter, filament details are cached for the session, and the spool list is fetched only when you press
Refresh, when a print starts, or when a slice check finds it more than an hour old. A deduction for all slots is one request; an undo is two
(the current weights, then the update). The footer and Settings show the requests left this month (`API 982/1000`). The plugin keeps the
last count it saw, with the time, so Settings shows it after a restart too. The quota is per account, so anything else using your key, such
as 3DFP AMS Sync, uses it too. Below 100 requests left you get one warning a month.

## Orca Cloud upload files

Orca Cloud requires a target suffix right before `.py`/`.whl`. It accepts exactly these: `any`, `win_x86_64`, `win_arm64`, `win`,
`macosx_arm64`, `macosx_x86_64`, `macosx_universal`, `macosx`, `linux_x86_64`, `linux_arm64`, `linux`. The plugin is pure
Python, so one `_any` file covers every OS:

```powershell
python tools/make_upload_files.py              # dist/threedfp_profiles_any.py  <- upload this
python tools/make_upload_files.py win_x86_64   # per-OS copies instead, if ever needed (identical content)
```

**Icon:** upload `assets/icon.png`, a square 320×320 PNG with a transparent background. OrcaSlicer shows a plugin's picture
from its Orca Cloud listing, and a plugin can't set it from its own file. The panel header uses an embedded copy, `assets/icon_64.png`.
After changing the logo, run `python tools/embed_logo.py` to refresh that copy.

## Tests

```powershell
python tests/test_offline.py          # any Python 3.12+, no OrcaSlicer or API key needed
```

The test script checks printer detection, setting serialization, preset file writing (including the Orca Cloud `.info`), base-preset choice and G-code
parsing. It also loads the plugin against a stub `orca` module.

## Command-line API check (no OrcaSlicer needed)

```powershell
& "C:\Program Files\OrcaSlicer\python\python.exe" -I threedfp_profiles.py 3dfp_YOURKEY search sunlu pla matte
& "C:\Program Files\OrcaSlicer\python\python.exe" -I threedfp_profiles.py 3dfp_YOURKEY profile 101 H2C 0.4
& "C:\Program Files\OrcaSlicer\python\python.exe" -I threedfp_profiles.py 3dfp_YOURKEY spools
```

## Notes / limits

- The OrcaSlicer plugin API can edit a slot's preset but can't create a new preset in a running session. That's why the saved preset file only appears after a restart, while the slot edit works right away.
- Orca Cloud sync keeps a preset's cloud ID in the `.info` file next to its `.json`, and OrcaSlicer writes that file itself.
  On each sync, OrcaSlicer deletes a local preset that has a cloud ID the cloud doesn't list. So never make up a `setting_id`:
  leave it empty and OrcaSlicer uploads the preset as new. The plugin never writes one. It clears the ID in a `.info` left over
  from a removed preset, and on an update it sets `sync_info = update` and a newer `updated_time`.
- The API key is stored in plain text in `%APPDATA%\OrcaSlicer\orca_plugins\plugin_data\<plugin key>\settings.json`.
