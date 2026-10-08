# Wabbajack modlists

A modlist is a *build recipe*, not a mod pack. Wabbajack ships a small `.wabbajack` file; the installer
downloads every archive from its original source, verifies hashes, applies binary patches, and lays out a
Mod Organizer 2 instance. Nothing in the list redistributes anyone's mod.

That shape explains the rules: the result is reproducible and verified, so an in-place change makes the
installed setup stop matching the thing the author supports and the thing a re-install would produce.

## Telling that a list built an instance

Three independent signals, cheapest first:

1. **`%LOCALAPPDATA%\Wabbajack\saved_settings\install-settings-<hash>.json`** — one per install this machine
   has ever done, a few hundred bytes each. This is the authoritative mapping and what `um mo2 list` uses:

   ```json
   {
     "ModListLocation": "D:\\Wabbajack\\4.2.1.4\\downloaded_mod_lists\\LoreRim_@@_LoreRim.wabbajack",
     "InstallLocation": "D:\\LoreRim",
     "DownloadLocation": "D:\\LoreRim\\downloads",
     "Metadata": { "title": "...", "author": "...", "game": "SkyrimSpecialEdition", "tags": ["Featured"] }
   }
   ```

2. **`<instance>/*.compiler_settings`** — JSON left in the instance root. Careful: these are the *author's*
   build settings, shipped along with the list (the paths and image inside point at the author's machine).
   They prove a list built this instance; they are not a recipe you can rebuild from.

3. **`<instance>/Stock Game/`** plus `gamePath` pointing into the instance — the list's own copy of the game,
   so a store update cannot change it underneath. Also `__temp__/`, `cache/` and a `logs/` folder.

```
um mo2 list                 # instances, each tagged with the Wabbajack list behind it
um mo2 info "D:\MyList"     # the modlist file, title, author, and the compiler_settings present
```

## Inside a .wabbajack file

A zip. Entries are GUID-named (the inlined files) plus one entry called `modlist`, which is the manifest:

| Key | What it holds |
|---|---|
| `Name`, `Author`, `Version`, `WabbajackVersion`, `Website`, `Description`, `Readme`, `Image` | the list's identity |
| `GameType` | e.g. `SkyrimSpecialEdition`, `SkyrimVR` — the list's game, which the mods need not match |
| `Archives[]` | every download: `Name`, `Size`, `Hash` (base64), `Meta` (ini text with the source), `State` (`$type`: `NexusDownloader`, `HttpDownloader`, …) |
| `Directives[]` | how files are produced: `FromArchive`, `PatchedFromArchive`, `InlineFile`, `CreateBSA`, `TransformedTexture` |

**Size warning.** On a large list the manifest is tens of MB compressed and ~125 MB expanded, with hundreds
of thousands of directives (one real list: 1,811 archives, 452,199 directives). Reading it needs the whole
JSON in memory, so do not touch it casually — use the `saved_settings` files for identity, and only open the
manifest for a question that genuinely needs the file-level truth.

`GameType` is worth reading once: it explains a VR list whose mods carry flat-game Nexus ids, because the
mods were downloaded from the flat game's pages.

## What can and cannot be verified locally

- **Can**: structural drift — what the profile lists versus what is in `mods/`, enabled-but-empty mods, a
  non-empty `overwrite/`, mods with no `meta.ini`, a shared download folder. That is `um mo2 check`.
- **Can, with the manifest**: whether a given file matches the build.
- **Cannot, from the instance alone**: whether a mod's *contents* were edited. MO2 records a version, not a
  hash. Wabbajack's own installer re-verifies against the manifest; that is the tool for "is this list
  intact", and it needs the `.wabbajack` file and the downloads.

## Changing a list without breaking it

In rough order of safety:

1. **A new profile.** MO2 copies the current one; mod order, plugin order and INIs are all per-profile. Free
   and reversible, and the author's profile stays pristine. `um backup` the profile folder first.
2. **Additions in clearly named mod folders**, placed deliberately in priority rather than left at the
   bottom. After a hand install the mod is not in any profile yet — MO2 adds it disabled on next start.
3. **A second instance** for anything structural. Disk is cheaper than a broken 4,000-mod list.
4. **Never tidy `downloads/`.** Lists share it (one instance's `download_directory` pointing into another's
   is common), and it is what a re-install or verify needs. `um mo2 check` warns when it is shared.

Tell the user plainly when a change puts them outside the list's support — many lists say so on their first
separator, and that is the author's position, not a technical limit.

## Related

- Running tools, file formats, priority direction: `mo2.md`.
- Finding and updating the mods themselves: `um nexus search` / `um nexus updates`.
- Wabbajack also ships a CLI (`wabbajack-cli.exe` next to the app, under its versioned folder) for
  compiling and installing; the GUI is the supported path for installing and verifying a list.
