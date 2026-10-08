# Vortex, the Nexus Mods App, and Linux

MO2 is the one with a virtual file system. The others deploy into the game folder, which inverts most of the
advice in `mo2.md`: here the game folder *is* modded, and the thing to be careful about is writing into it
behind the manager's back.

## Vortex

Nexus' long-standing manager. It stages each mod in its own folder and then **deploys** into the game —
hardlinks by default, copies where the filesystem can't (a different volume), or symlinks if configured.

Verified layout:

```
<staging root>/<game domain>/              e.g. D:\Vortex Mods\skyrimse\
  __vortex_staging_folder                  marker: {"instance":"<uuid>","game":"skyrimse"}
  <Mod Name>-<mod id>-<version>-<stamp>/   one folder per mod
```

Two facts worth having:

1. **The marker file identifies a staging root** and names the game domain, which is Nexus' own domain
   (`skyrimse` here, not MO2's `SkyrimSE`). Finding `__vortex_staging_folder` is how you know a folder full
   of mods is Vortex's rather than someone's downloads.
2. **The folder names are the Nexus archive names**, so each one carries the mod id and version:
   `'Pumping Iron' and 'Hand to Hand (Adamant)' Patch-97490-1-0-1724779805`. That is enough to version-check
   a whole Vortex setup without touching Vortex's own state:

   ```
   um nexus updates "D:\Vortex Mods\skyrimse" --game skyrimspecialedition
   ```

   (Confirmed on a real 2,432-mod staging folder: 2,250 resolved and checked, none of them carrying a
   `meta.ini`.) The version is only as good as the name, so a hand-renamed folder drops out and is reported.

What is **not** readable without extra dependencies: Vortex keeps its real state (enabled set, deployment
targets, rules, load order) in a **LevelDB** at `%APPDATA%\Vortex\state.v2\*.ldb`. Don't hand-parse it and
don't add a dependency for it — ask Vortex, or ask the user.

When a game is deployed, Vortex writes a manifest into the deploy target (`vortex.deployment.json`) listing
every deployed file and the mod it came from. That is the honest answer to "which mod provides this file"
for a Vortex setup. No game on this machine currently has one, so check it exists before relying on it.

Practical consequences:

- **Purge before editing the game folder.** Deployed files are hardlinks: editing one edits the staged copy
  too, and Vortex may overwrite or revert it on the next deploy. Change the staged mod, then redeploy.
- **Hardlinks mean staging must share a volume with the game**, which is why the staging root sits on the
  same drive.
- Vortex sorts plugins with LOOT internally; load order is Vortex's business, not a file you should edit.

## Nexus Mods App

Nexus' newer, cross-platform manager, actively replacing Vortex. It keeps mods in its own content store and
deploys into the game, with a transactional "apply/revert" model rather than Vortex's deploy/purge, and it is
designed for Linux and the Steam Deck as first-class targets.

It is not installed on this machine, so treat any layout detail as unverified: locate its data directory and
confirm before writing anything that depends on it. The safe assumptions are the general ones — the game
folder is really modded, and the app owns what it deployed, so changes belong in the app.

## Linux, Proton and the Steam Deck

- **MO2 under Proton** works but is fiddly: MO2's VFS hooks Windows processes, so MO2 and the game must run
  in the **same Proton prefix**, and every tool it launches too. Steam Tinker Launch or a dedicated prefix is
  the usual arrangement. Paths inside the prefix are Windows paths (`Z:\...` maps the Linux root), which is
  what `ModOrganizer.ini` will contain.
- **Amethyst** ([ChrisDKN/Amethyst-Mod-Manager](https://github.com/ChrisDKN/Amethyst-Mod-Manager)) manages
  Bethesda mods natively on Linux, no prefix needed.
- **The Nexus Mods App** is the first-party Linux route.
- From WSL, `um` maps paths for you (`um.common.to_posix` / `to_win`), so an instance on `D:` reads as
  `/mnt/d/...`. `um mo2` sweeps `/mnt/*` for instances when it runs there.
- Case sensitivity bites: a Linux filesystem distinguishes `Meshes/` from `meshes/` where the game and every
  mod assume it does not. Keep mods on a case-insensitive mount, or expect missing-asset bugs that look like
  load-order problems.

## Picking one, when asked

- **An existing curated modlist** → whatever it was built for, which is MO2 (`wabbajack.md`).
- **Experiments, or anything an agent will read** → MO2: mods stay separate, profiles are free branches, the
  game folder stays clean, and the state is plain text (`mo2.md`).
- **Already on Vortex and happy** → leave it; the cost of migrating a large setup is real.
- **Linux-first** → Nexus Mods App or Amethyst over fighting a Proton prefix.

Get any manager from its own official page. Look-alike repositories with an installer in their releases are
a routine way to spread malware in this community.
