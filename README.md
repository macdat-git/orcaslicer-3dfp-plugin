<img src="assets/icon.png" alt="3D Filament Profiles plugin logo" width="96" align="right">

# 3D Filament Profiles plugin for OrcaSlicer 2.5

Connects OrcaSlicer to [3dfilamentprofiles.com](https://3dfilamentprofiles.com) via its API
([docs](https://3dfilamentprofiles.com/docs/api)). It's a single file, `threedfp_profiles.py`, with no dependencies beyond Python's standard library.

> Unofficial community plugin, not affiliated with 3D Filament Profiles or OrcaSlicer. Review generated filament
> settings before printing: they come from community data.

**Requirements**
- OrcaSlicer **2.5 or newer**, which has the Python plugin system.
- A 3DFP API key: the API subscription, or a Patreon tier that includes it.

It was developed and tested on Windows with a Bambu Lab H2C. The printer detection also covers the X1/P1/P2S/A1/A2L/H2/X2D
families, and other printers get 3DFP's printer-neutral ("ANY") profile.

## What it does

A **3DFP** panel docks on the right of the Prepare/Preview tabs:

| Tab | |
|---|---|
| **Find** | Search the 3DFP catalog (brand, material, colour, hex). Click a filament for temps, density, flow ratio, max volumetric speed, price. |
| **My spools** | Your 3DFP spools with grams left and location. |
| **Slots** | Link each filament slot to a spool; shows the grams per slot from the last slice; **Deduct from 3DFP spools**. |
| **Settings** | API key, printer/nozzle override (auto-detected from the printer preset, e.g. H2C / P2S, 0.4), pressure advance, options below. |

**Use in slot N** (for a catalog filament or one of your spools):
1. Asks 3DFP for its OrcaSlicer preset for your printer + nozzle (for a spool, your calibrated temps/flow/K are used).
2. Selects the matching system preset in that slot (e.g. `Bambu PLA Basic @BBL H2C`, or a Generic preset of the
   same type if that one isn't enabled) and applies the 3DFP values on top. It shows as an unsaved edit, and you can save it from the filament settings.
3. Also writes it as a user preset (`%APPDATA%\OrcaSlicer\user\<id>\filament\<name>.json`), which shows up in the filament list
   **after OrcaSlicer restarts**. If a file with that name already exists, the old one is backed up to the plugin's data folder first.

**Save preset** only does step 3.

**Spool deduction:** after slicing, the grams per slot are read from the G-code. When a print starts, a notification
offers to subtract them from the spools linked in **Slots** (or does it automatically if you turn that on). This uses a single API request.

## Install

1. OrcaSlicer → main menu → **Plugins** → the arrow on **Browse plugins** → **Install local plugin** → pick `threedfp_profiles.py`.
2. Approve the permission prompt: HTTPS to `3dfilamentprofiles.com`, worker threads, and read access to Orca's temp slice folder.
3. Select the plugin and press **Run** to open the panel. After that it opens by itself when a project opens; you can turn that off in Settings.
4. **Settings** → paste your API key (`3dfp_…`, from the *API access* page in the 3DFP user menu; it needs the API subscription or a Patreon tier).

To update the plugin, copy the new file over the installed copy at
`%APPDATA%\OrcaSlicer\orca_plugins\threedfp_profiles.py\threedfp_profiles.py` and restart OrcaSlicer. That keeps the approved
permissions. Using **Install local plugin** again also works, but it resets the permissions and you'll be asked to approve them again.

The panel shares the right-hand dock with other plugin panels (SlicePilot, MakerWorld). Drag its edge to make it taller.

## API quota

Searches only run when you press Search or Enter, filament details are cached for the session, and the spool list is fetched only when you press
Refresh. A deduction for all slots is one request. The footer shows the remaining monthly quota (`API 982/1000`).

## Orca Cloud upload files

Orca Cloud wants the target OS at the end of the file name. The plugin is pure Python, so every target gets an identical copy:

```powershell
python tools/make_upload_files.py              # dist/threedfp_profiles_win_x86_64.py, _win_arm64, _macos_arm64, ...
python tools/make_upload_files.py win_x86_64   # only the targets you name
```

**Icon:** upload `assets/icon.png`, a square 320×320 PNG with a transparent background. OrcaSlicer shows a plugin's picture
from its Orca Cloud listing, and a plugin can't set it from its own file. The panel header uses an embedded copy, `assets/icon_64.png`.
After changing the logo, run `python tools/embed_logo.py` to refresh that copy.

## Tests

```powershell
python tests/test_offline.py          # any Python 3.12+, no OrcaSlicer or API key needed
```

The test script checks printer detection, setting serialization, preset file writing, base-preset choice and G-code
parsing. It also loads the plugin against a stub `orca` module.

## Command-line API check (no OrcaSlicer needed)

```powershell
& "C:\Program Files\OrcaSlicer\python\python.exe" -I threedfp_profiles.py 3dfp_YOURKEY search sunlu pla matte
& "C:\Program Files\OrcaSlicer\python\python.exe" -I threedfp_profiles.py 3dfp_YOURKEY profile 101 H2C 0.4
& "C:\Program Files\OrcaSlicer\python\python.exe" -I threedfp_profiles.py 3dfp_YOURKEY spools
```

## Notes / limits

- The OrcaSlicer plugin API can edit a slot's preset but can't create a new preset in a running session. That's why the saved preset file only appears after a restart, while the slot edit works right away.
- The API key is stored in plain text in `%APPDATA%\OrcaSlicer\orca_plugins\plugin_data\<plugin key>\settings.json`.
