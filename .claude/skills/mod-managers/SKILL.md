---
name: mod-managers
description: Work inside a mod manager instead of fighting it - Mod Organizer 2, Wabbajack modlists, Vortex, the Nexus Mods App, and Linux/Proton. Covers finding the instance that actually runs, reading its mod order and plugin load order, working out which mod really provides a file, running xEdit/Synthesis/DynDOLOD/BodySlide so they see the virtual Data tree, and changing a curated modlist without breaking it. Use when a game is managed by MO2/Vortex/Wabbajack, when a mod "isn't doing anything", when asked about load order or conflicts, before touching an existing modded setup, or whenever the game folder looks unmodded but the game is clearly modded.
---

# Working inside a mod manager

Most heavily modded games are not modded in their own folder. A manager keeps each mod separately and
assembles the game's data at launch. Edit the game folder and nothing happens; read the game folder and you
see a game with no mods. Everything below exists because of that one fact.

## 0. Find the setup that actually runs

```
um mo2 list                      # every MO2 instance on this machine + the Wabbajack list behind each
um mo2 info "D:\MyList"          # game, gamePath, folders, profiles, counts, build
```

`um scan` finds the *store* install. That is frequently **not** what launches: a Wabbajack list usually
copies the game into the instance as `Stock Game`, and `gamePath` in `ModOrganizer.ini` points there.
Patching the Steam copy of such a setup changes nothing. `um mo2 info` prints both and flags it.

If there is no MO2 instance, work out which manager owns the game before anything else — the answer changes
whether files on disk mean what they appear to mean (`references/other-managers.md`):

| Manager | Where mods live | Is the game folder modded? |
|---|---|---|
| Mod Organizer 2 | `<instance>/mods/<mod>/` | **No** — virtual at runtime |
| Wabbajack | an MO2 instance it built | No (plus `Stock Game`) |
| Vortex | a staging folder | **Yes** — hardlinked/copied in |
| Nexus Mods App | its own store | Yes — deployed in |
| Manual | the game folder | Yes |

## 1. Read before you touch

```
um mo2 mods "D:\MyList" --enabled --limit 40     # highest priority first: the top of this list wins
um mo2 plugins "D:\MyList"                       # the plugin load order
um mo2 conflicts "D:\MyList" --file "meshes/actors/character/character assets/skeleton.nif"
um mo2 conflicts "D:\MyList" --mod "XP32 Maximum Skeleton Special Extended"
um mo2 check "D:\MyList"                         # drift, orphans, a non-empty overwrite, shared downloads
```

Two orderings exist and they are not the same thing. **Mod priority** decides which mod's *files* win
(meshes, textures, scripts). **Plugin load order** decides which `.esp`/`.esm` record wins. A mod can win
one and lose the other. "My retexture isn't showing" is a priority question; "my item has the wrong stats"
is a load-order question.

Never hand-read `modlist.txt` top-down to answer "who wins" — it is written **highest priority first**,
the reverse of MO2's left pane. `um mo2 mods` and `um mo2 conflicts` already account for it;
`references/mo2.md` has the file formats if something needs parsing directly.

## 2. Run tools through the manager, never beside it

xEdit/SSEEdit, LOOT, Synthesis, Nemesis, Pandora, BodySlide, DynDOLOD, TexGen, zEdit, CAO: every one of
these reads the game's Data folder. Launched on their own they read the *bare* game and will happily
generate a patch against the wrong inputs, or write output nowhere useful.

```
um mo2 tools "D:\MyList"               # what this instance is configured to launch
um mo2 run "D:\MyList" --tool xEdit64  # hands it to MO2, which builds the VFS first
```

MO2 must not already be running, and it stays open while the tool does. Anything a tool writes into the
virtual Data lands in `<instance>/overwrite/` — that is a staging area, not a mod. Output left there is
loaded at the highest priority and belongs to nobody, so move it into its own mod folder when it matters.
`um mo2 check` flags a non-empty `overwrite/`.

## 3. Changing a curated modlist

A Wabbajack list is a hash-verified build, not a starting point. The user's own list may say so on its top
separator ("ADDING ANY MODS VOIDS SUPPORT"). Adding mods does not corrupt anything, but it does mean the
author's support, the list's own verification, and any future re-install no longer describe what is
installed. So:

- **Make a new profile** for experiments (MO2 copies the current one). The mod order, plugin order and INIs
  are per-profile, so a profile is a free, reversible branch.
- **Keep additions separate** and obviously named, so `um mo2 check` and a human can both see them.
- **Never tidy the download folder.** Instances share them — one list's `download_directory` often points
  into another's, so clearing it in one empties it for the others. `um mo2 info` prints the real path and
  `um mo2 check` warns when it is shared.
- **`um backup` the profile folder** before reordering anything; it is small and it is the whole experiment.
- For a from-scratch build, let Wabbajack install it and treat the result as read-only, then branch.

`references/wabbajack.md` covers what a `.wabbajack` file contains, how to tell which list built an
instance, and what can and cannot be verified locally.

## 4. Getting mods in

```
um nexus search "..." --game <domain>            # find them (no account needed)
um nexus updates "D:\MyList" --enabled-only      # what the setup is missing
um nexus install <archive> --to "D:\MyList\mods" --yes
```

`um nexus install` unpacks into one folder per mod the way MO2 keeps them and writes the `meta.ini` keys MO2
uses, so update-checking keeps working afterwards. It will not apply a FOMOD's choices — a scripted
installer with real options belongs in MO2's own installer. After installing by hand, the mod exists but is
not in any profile's `modlist.txt`: MO2 adds it (disabled, lowest priority) on next start, and it has to be
enabled and placed deliberately.

## 5. Verify like the game does

A mod manager setup is only correct at launch, so confirm there rather than on disk:

- `um mo2 check` for structural drift.
- Launch the game through the manager and read its logs — for Bethesda games, `Documents/My Games/<Game>/`
  (`SKSE/skse64.log`, crash logs from Crash Logger / .NET Script Framework name the offending plugin).
- `um win shot`/`um win record` to see it in the real game (see `game-automation`).

## Pitfalls

1. **The game folder looks unmodded.** It is. Find the instance (step 0) rather than concluding the mods
   are not installed.
2. **A mod with no `meta.ini`** was built by Wabbajack or made by hand; nothing can version-check it, and
   `um nexus updates` reports it rather than guessing.
3. **MO2 spells games three ways.** `ModOrganizer.ini` says "Skyrim VR", a mod's `meta.ini` says "SkyrimVR",
   and Nexus' domain is a third thing. Never pass one where another is expected.
4. **Nexus has no public game entry for the VR editions**, so a VR instance needs
   `um nexus updates --game <flat game domain>`; its mods came from those pages anyway.
5. **Editing files in `mods/` edits the mod, not a copy.** There is no undo. `um backup` first.
6. **Two instances, one downloads folder** — see step 3.
7. **Separators are real folders.** `mods/<name>_separator/` exists for every divider in the UI; they are
   not mods and are not missing files.

## References

- `references/mo2.md` — the instance layout, every file format, the VFS, profiles, overwrite, the CLI.
- `references/wabbajack.md` — modlists: what they are, what is verifiable, how to branch one safely.
- `references/other-managers.md` — Vortex, the Nexus Mods App, Linux/Proton and Amethyst.
- `um mo2 --help` and `um nexus --help` carry the same facts as command help.
- Engine-level work (plugins, Papyrus, SKSE, BSA) is `mod-any-game/references/engines/bethesda.md`.
