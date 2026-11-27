# Changelog

The 3DFP API that this plugin uses is a paid, subscription-based add-on to a 3dfilamentprofiles.com account
(also included in some Patreon tiers). Without an API key the plugin can't do anything.

## 1.2.0 (2026-11-27)

**Slice checks.** After each slice, the plugin checks the spool each used slot would be deducted from. It warns, in a
notification and under *Last slice* in Slots, when:
- the spool doesn't have enough filament for the print plus a spare margin (**Spare filament**, 10 g by default). A spool
  used by several slots is checked against their total;
- the spool's material doesn't match the slot's filament type, or its colour is clearly different;
- a slot you linked by hand has a spool that 3DFP has somewhere other than this printer (a sign it was swapped);
- a linked spool is no longer in your 3DFP spools.

The warnings update when you change a link in Slots. They use the spool list the plugin already has, and reload it (one
request) only when it's more than an hour old. Turn them off with **Check each slice against your spools** in Settings.

**Load my AMS** (Slots): sets OrcaSlicer's filament slots from the spools 3DFP has in the selected printer, in AMS order
(AMS HT and external spools last). Each slot gets that spool's 3DFP preset and colour. It asks you to confirm first, and
says how many filament slots to add when the AMS has more spools than OrcaSlicer has slots. With auto-link on, manual links
on the loaded slots are removed so they can't go stale.

**History tab and Undo.** Every deduction, spool marked empty and undo is listed, newest first. **Undo** (in History, and
**Undo last deduction** in Slots) adds the grams back to the spool's current 3DFP weight, so later deductions stay. A
spool that 3DFP moved to *Empty* goes back to its previous location. Undoing the last slice's deduction lets you deduct it
again, e.g. after linking the right spool. Deductions logged by earlier versions are imported from `events.log`.

**Spools tab** (renamed from *My spools*):
- **Mark empty** on an opened spool sets it to 0 g; 3DFP then applies your empty-spool setting. Press it twice to confirm.
- **low stock** filter: spools under the **Low stock below** weight (150 g by default), lowest first.
- **Totals**: filament left per material, with a *low* tag on materials under the low stock weight.

**API quota.** Settings shows the requests left this month and when that was last seen, also after a restart. You get one
warning a month when fewer than 100 are left. The quota is per account, so it includes other tools that use your key.

**Slot names.** Slots are shown with their position as named in your 3DFP locations, e.g. `Slot 1 (AMS: A-2)` or
`Slot 3 (AMS: HT-A-1)`, in the Slots list, the **Use in slot** picker, *Last slice* and the slice-check warnings. The position
comes from the spool linked to the slot, or the one auto-link would pick (marked *auto*).

**Description.** The plugin description and README now say that the 3DFP API is a paid subscription add-on.

## 1.1.2 (2026-11-26)

- New setting **Add new RFID spools to 3DFP**, shown only while the [3DFP AMS Sync](https://github.com/macdat-git/3dfp-ams-sync)
  service runs on the same PC (it leaves `ams_sync.json` in the plugin's data folder). It turns the service's adding of new
  Bambu RFID spools on or off; on by default.
- The README has a *3DFP AMS Sync* section on getting the service from GitHub, and the **Link slots to spools automatically**
  hint links to it.

## 1.1.1 (2026-11-25)

- **Deduct without asking** is on by default: filament is taken off the slots' 3DFP spools as soon as a print is sent, and
  the result shows as a notification. Turn it off to get a notification with a **Deduct** link instead.

## 1.1.0 (2026-11-25)

- **Link slots to spools automatically**: a used slot with no spool picked in Slots gets the spool that 3DFP places in the
  selected printer's AMS (same filament type, then closest colour, then AMS order). The spool list is re-read when the
  print starts. Slots that can't be matched raise a warning notification.
- Slices, print starts, links and deductions are written to `events.log` in the plugin's data folder.

## 1.0.3 (2026-11-23)

- Updated user presets stay in step with Orca Cloud sync: the preset's `.info` file is updated so a changed preset is
  uploaded, and a leftover `.info` no longer gets a new preset deleted.

## 1.0.2 (2026-11-23)

- The spool logo is the plugin's icon and panel logo.

## 1.0.1 (2026-11-23)

- First release: a docked panel to search the 3DFP catalog and your spools, apply a 3DFP OrcaSlicer preset to a filament
  slot and save it as a user preset, and deduct the grams a slice used from linked 3DFP spools.
