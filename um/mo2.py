"""Mod Organizer 2 instances from an agent: read one, see who wins a conflict, run a tool inside its VFS.

    um mo2 list                                   # instances on this machine, and the Wabbajack list behind each
    um mo2 info "D:\\LoreRim"                      # game, Stock Game, profiles, counts, downloads, build
    um mo2 profiles "D:\\LoreRim"
    um mo2 mods "D:\\LoreRim" --enabled --limit 20          # winners first, with MO2's own priority numbers
    um mo2 plugins "D:\\LoreRim"                            # the plugin load order
    um mo2 conflicts "D:\\LoreRim" --file "meshes/actors/character/character assets/skeleton.nif"
    um mo2 conflicts "D:\\LoreRim" --mod "SkyUI" --limit 200
    um mo2 check "D:\\LoreRim"                              # drift: missing folders, orphans, overwrite, sharing
    um mo2 tools "D:\\LoreRim"                              # the executables MO2 is configured to launch
    um mo2 run "D:\\LoreRim" --tool xEdit64                 # through MO2, so the tool sees the virtual Data

Why this exists: MO2 does not install mods into the game. It keeps each one in its own folder and builds a
virtual Data tree at launch, so a tool started any other way reads the bare game and silently does the wrong
thing. Everything here either reads the real folders (safe, no MO2 needed) or hands the job to MO2 itself.

Facts this encodes, all checked against real instances rather than assumed:
- `modlist.txt` is written highest-priority-first, the reverse of MO2's left pane, so reading it top-down
  gets every conflict winner backwards. `+` enabled, `-` disabled, `*` an unmanaged base-game entry, and a
  `_separator` suffix marks a UI divider rather than a mod.
- `plugins.txt` uses `*` for "enabled", which is a different meaning from the `*` in `modlist.txt`.
- A portable instance keeps its data next to `ModOrganizer.ini`; a managed one keeps only the ini under
  %LOCALAPPDATA%\\ModOrganizer\\<name> and its mods at `base_directory`.
- Values in the ini are Qt-wrapped (`@ByteArray(...)`) with doubled backslashes, and the directory keys may
  carry a `%BASE_DIR%` placeholder.
- `gamePath` often points at a "Stock Game" copy inside the instance, so the Steam install `um scan` finds
  is not the one that runs.
- Instances share download folders (one list's `download_directory` pointing into another's), so a tidy-up
  in one can empty another.
- MO2 spells a game one way in `ModOrganizer.ini` ("Skyrim VR") and another in a mod's `meta.ini`
  ("SkyrimVR"); neither is the Nexus domain.
"""
from __future__ import annotations

import json
import os
import re
import string
import subprocess
from pathlib import Path

from um.common import die, emit, is_windows, is_wsl, to_posix, to_win

LOCAL_INSTANCES = "ModOrganizer"          # %LOCALAPPDATA%\ModOrganizer\<instance name>\ModOrganizer.ini
WABBAJACK_SETTINGS = "Wabbajack/saved_settings"
# MO2's overrides for where the data lives. Absent means "<base>/<name>".
DIR_KEYS = {"mods": "mod_directory", "profiles": "profiles_directory",
            "downloads": "download_directory", "overwrite": "overwrite_directory"}
META_KEYS = ("gameName", "modid", "version", "newestVersion", "installationFile", "repository", "ignoredVersion")


# --------------------------------------------------------------------------- reading MO2's files


def _unqt(value: str) -> str:
    """A value as MO2 stored it: Qt wraps strings it round-trips as @ByteArray(...) and doubles backslashes."""
    v = (value or "").strip()
    m = re.fullmatch(r"@ByteArray\((.*)\)", v, re.S)
    if m:
        v = m.group(1)
    return v.replace("\\\\", "\\")


def ini_values(path: Path) -> dict:
    """The flat `key=value` pairs from an ini's plain sections. Hand-rolled rather than configparser:
    ModOrganizer.ini holds keys like `1\\title=` and raw `%` signs that configparser rejects outright."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        die(f"cannot read {path}: {e}")
    section = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, sep, value = line.partition("=")
        if sep and section in ("General", "Settings", ""):
            out.setdefault(key.strip(), value)
    return out


def read_meta(path: Path) -> dict:
    """The few [General] keys we need out of a mod's meta.ini. Scanned line by line on purpose: MO2 pastes
    the whole Nexus description into these files, so they run to tens of KB."""
    want = {k.lower(): k for k in META_KEYS}
    found: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                key, sep, value = line.partition("=")
                real = want.get(key.strip().lower())
                if sep and real and real not in found:
                    found[real] = value.strip().strip('"')
                if len(found) == len(want):
                    break
    except OSError:
        return {}
    return found


# --------------------------------------------------------------------------- finding instances


def local_appdata() -> Path | None:
    if is_windows():
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) if base else None
    if is_wsl():                                  # /mnt/c/Users/<user>/AppData/Local
        for home in Path("/mnt/c/Users").glob("*"):
            d = home / "AppData" / "Local"
            if d.is_dir():
                return d
    return None


def wabbajack_installs() -> list[dict]:
    """Every Wabbajack install this machine remembers: which .wabbajack went where, and its metadata.
    %LOCALAPPDATA%\\Wabbajack\\saved_settings\\install-settings-<hash>.json, a few hundred bytes each."""
    base = local_appdata()
    if not base:
        return []
    out = []
    for f in sorted((base / WABBAJACK_SETTINGS).glob("install-settings-*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or not d.get("InstallLocation"):
            continue
        meta = d.get("Metadata") or {}
        out.append(dict(modlist=d.get("ModListLocation") or "", install=d["InstallLocation"],
                        downloads=d.get("DownloadLocation") or "", title=meta.get("title") or "",
                        author=meta.get("author") or "", game=meta.get("game") or "", settings=str(f)))
    return out


def find_instances() -> list[Path]:
    """Every ModOrganizer.ini we can reach: managed instances under %LOCALAPPDATA%, the install folders
    Wabbajack recorded, and a shallow sweep of each drive (an instance is nearly always D:\\<Name>)."""
    seen, out = set(), []

    def add(ini: Path):
        try:
            key = ini.resolve()
        except OSError:
            return
        if ini.is_file() and key not in seen:
            seen.add(key)
            out.append(ini)

    base = local_appdata()
    if base:
        for ini in sorted((base / LOCAL_INSTANCES).glob("*/ModOrganizer.ini")):
            add(ini)
    for rec in wabbajack_installs():
        add(Path(to_posix(rec["install"])) / "ModOrganizer.ini")
    roots: list[Path] = []
    if is_windows():
        roots = [Path(f"{d}:/") for d in string.ascii_uppercase if Path(f"{d}:/").exists()]
    elif is_wsl():
        roots = [p for p in Path("/mnt").glob("*") if p.is_dir()]
    else:
        roots = [Path.home()]
    for root in roots:
        for depth in ("*/ModOrganizer.ini", "*/*/ModOrganizer.ini"):
            try:
                for ini in sorted(root.glob(depth)):
                    add(ini)
            except OSError:
                continue
    return out


# --------------------------------------------------------------------------- one instance


def resolve(target: str) -> dict:
    """An instance folder, a managed instance's name, or an instance's ModOrganizer.ini -> everything about
    it. A portable instance keeps its data beside the ini; a managed one keeps it at base_directory."""
    p = Path(to_posix(str(target))).expanduser()
    ini = p if p.is_file() else p / "ModOrganizer.ini"
    if not ini.is_file():
        base = local_appdata()
        named = (base / LOCAL_INSTANCES / str(target) / "ModOrganizer.ini") if base else None
        if named and named.is_file():
            ini = named
        else:
            die(f"no ModOrganizer.ini at {p} (`um mo2 list` shows the instances found here)")
    values = ini_values(ini)
    portable = (ini.parent / "ModOrganizer.exe").is_file()
    base_dir = _unqt(values.get("base_directory", "")) or str(ini.parent)
    root = Path(to_posix(base_dir))

    def folder(name: str) -> Path:
        raw = _unqt(values.get(DIR_KEYS[name], ""))
        if not raw:
            return root / name
        return Path(to_posix(raw.replace("%BASE_DIR%", str(root))))

    game_path = _unqt(values.get("gamePath", ""))
    inst = dict(name=ini.parent.name, ini=str(ini), portable=portable, root=str(root),
                game=_unqt(values.get("gameName", "")), game_path=game_path,
                game_edition=_unqt(values.get("game_edition", "")),
                mo2_version=_unqt(values.get("version", "")),
                selected_profile=_unqt(values.get("selected_profile", "")),
                mods_dir=str(folder("mods")), profiles_dir=str(folder("profiles")),
                downloads_dir=str(folder("downloads")), overwrite_dir=str(folder("overwrite")))
    # A "Stock Game" is the game copied into the instance so a store update cannot change it under the list.
    inst["stock_game"] = bool(game_path) and Path(to_posix(game_path)).resolve().is_relative_to(root.resolve()) \
        if game_path and root.exists() else False
    wj = next((w for w in wabbajack_installs()
               if Path(to_posix(w["install"])).resolve() == root.resolve()), None) if root.exists() else None
    inst["wabbajack"] = wj
    inst["compiler_settings"] = sorted(p.name for p in ini.parent.glob("*.compiler_settings"))
    return inst


def profiles(inst: dict) -> list[str]:
    d = Path(inst["profiles_dir"])
    return sorted(p.name for p in d.glob("*") if p.is_dir()) if d.is_dir() else []


def profile_dir(inst: dict, profile: str | None = None) -> Path:
    name = profile or inst["selected_profile"]
    if not name:
        die(f"{inst['name']} has no selected profile; pass --profile (have: {', '.join(profiles(inst)) or 'none'})")
    d = Path(inst["profiles_dir"]) / name
    if not d.is_dir():
        die(f"no profile {name!r} in {inst['profiles_dir']} (have: {', '.join(profiles(inst)) or 'none'})")
    return d


def mod_order(inst: dict, profile: str | None = None) -> list[dict]:
    """The profile's mod list, highest priority first - the order the file itself is written in, which is
    MO2's left pane read bottom-up. `priority` is MO2's own number, where a bigger number wins."""
    listing = profile_dir(inst, profile) / "modlist.txt"
    if not listing.is_file():
        die(f"no modlist.txt in {listing.parent}")
    rows = []
    for line in listing.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        state, name = line[0], line[1:]
        if state not in "+-*":
            continue
        rows.append(dict(name=name, enabled=state == "+", unmanaged=state == "*",
                         separator=name.endswith("_separator")))
    for i, row in enumerate(rows):
        row["priority"] = len(rows) - 1 - i          # the file is written winners-first
    return rows


def enabled_mods(inst: dict, profile: str | None = None) -> set[str]:
    return {m["name"] for m in mod_order(inst, profile) if m["enabled"] and not m["separator"]}


def plugins(inst: dict, profile: str | None = None) -> list[dict]:
    """The plugin load order. plugins.txt carries the enabled flag (`*`), loadorder.txt the order; where
    both exist loadorder.txt is the order and plugins.txt only says which are on."""
    d = profile_dir(inst, profile)

    def lines(name):
        f = d / name
        return [l.strip() for l in f.read_text(encoding="utf-8", errors="replace").splitlines()
                if l.strip() and not l.startswith("#")] if f.is_file() else []

    on = {l.lstrip("*") for l in lines("plugins.txt") if l.startswith("*")}
    order = [l.lstrip("*") for l in lines("loadorder.txt")] or [l.lstrip("*") for l in lines("plugins.txt")]
    return [dict(index=i, name=n, enabled=n in on) for i, n in enumerate(order)]


def installed_mods(inst: dict, profile: str | None = None, enabled_only: bool = False) -> list[dict]:
    """Every mod folder with a meta.ini, newest-priority first, with what MO2 recorded about it."""
    mods_dir = Path(inst["mods_dir"])
    if not mods_dir.is_dir():
        die(f"no mods folder at {mods_dir}")
    order = {m["name"]: m for m in mod_order(inst, profile)} if Path(inst["profiles_dir"]).is_dir() else {}
    out = []
    for meta in sorted(mods_dir.glob("*/meta.ini")):
        folder = meta.parent.name
        row = order.get(folder)
        if enabled_only and row is not None and not row["enabled"]:
            continue
        m = read_meta(meta)
        mod_id = (m.get("modid") or "").strip()
        out.append(dict(folder=folder, mod_id=int(mod_id) if mod_id.isdigit() else 0,
                        game=m.get("gameName") or "", version=m.get("version") or "",
                        newest=m.get("newestVersion") or "", ignored=m.get("ignoredVersion") or "",
                        file=m.get("installationFile") or "", repository=m.get("repository") or "",
                        enabled=None if row is None else row["enabled"],
                        priority=None if row is None else row["priority"]))
    out.sort(key=lambda r: (-(r["priority"] if r["priority"] is not None else -1), r["folder"].lower()))
    return out


# --------------------------------------------------------------------------- conflicts


def winner(inst: dict, rel: str, profile: str | None = None) -> dict:
    """Which mod actually provides one virtual Data path. Highest priority wins, and `overwrite/` beats
    every mod because MO2 writes a tool's output there."""
    from um.common import safe_relpath
    safe = safe_relpath(rel, allow_backslash=True)
    if safe is None:
        die(f"{rel!r} has to be a path inside Data, relative and without ..")
    mods_dir = Path(inst["mods_dir"])
    providers = []
    over = Path(inst["overwrite_dir"]) / safe
    if over.exists():
        providers.append(dict(mod="<overwrite>", priority=10**9, enabled=True, path=str(over)))
    for m in mod_order(inst, profile):
        if m["separator"] or not m["enabled"]:
            continue
        candidate = mods_dir / m["name"] / safe
        if candidate.exists():
            providers.append(dict(mod=m["name"], priority=m["priority"], enabled=True, path=str(candidate)))
    providers.sort(key=lambda p: -p["priority"])
    return dict(file=safe.as_posix(), providers=providers,
                winner=providers[0]["mod"] if providers else None, count=len(providers))


def mod_conflicts(inst: dict, mod: str, profile: str | None = None, limit: int = 300) -> dict:
    """What one mod wins and loses. Checking every file against every mod is not viable on a 4,000 mod
    list, so this walks the one mod's files (up to `limit`) and asks about each."""
    mods_dir = Path(inst["mods_dir"])
    folder = mods_dir / mod
    if not folder.is_dir():
        die(f"no mod folder {folder}")
    order = {m["name"]: m for m in mod_order(inst, profile)}
    me = order.get(mod)
    if me is None:
        die(f"{mod!r} is not in that profile's modlist.txt")
    files = [p for p in sorted(folder.rglob("*")) if p.is_file() and p.name.lower() != "meta.ini"][:limit + 1]
    truncated = len(files) > limit
    wins, loses = [], []
    for p in files[:limit]:
        rel = p.relative_to(folder).as_posix()
        w = winner(inst, rel, profile)
        if w["count"] > 1:
            (wins if w["winner"] == mod else loses).append(dict(file=rel, against=w["winner"]))
    return dict(mod=mod, priority=me["priority"], enabled=me["enabled"], checked=min(len(files), limit),
                truncated=truncated, wins=wins, loses=loses)


# --------------------------------------------------------------------------- health check


def check(inst: dict, profile: str | None = None) -> dict:
    """Drift between what the profile lists and what is on disk, plus the states that quietly break a list."""
    mods_dir = Path(inst["mods_dir"])
    rows = mod_order(inst, profile)
    # Separators are kept: MO2 gives each one a real folder in mods/, so leaving them out of the listed set
    # makes every single one look like an orphan. Unmanaged (*) entries are the opposite - base-game data
    # with no folder of their own.
    listed = {m["name"] for m in rows if not m["unmanaged"]}
    on_disk = {p.name for p in mods_dir.glob("*") if p.is_dir()} if mods_dir.is_dir() else set()
    problems: list[str] = []
    notes: list[str] = []
    missing = sorted(listed - on_disk)
    orphans = sorted(on_disk - listed)
    if missing:
        problems.append(f"{len(missing)} listed in the profile with no folder in mods/ "
                        f"(e.g. {', '.join(missing[:4])})")
    if orphans:
        notes.append(f"{len(orphans)} folders in mods/ the profile never mentions - left over, or belonging "
                     f"to another profile (e.g. {', '.join(orphans[:4])})")
    empty = sorted(m["name"] for m in rows if m["enabled"] and not m["separator"] and not m["unmanaged"]
                   and (mods_dir / m["name"]).is_dir() and not any((mods_dir / m["name"]).iterdir()))
    for name in empty[:20]:
        problems.append(f"enabled but the folder is empty: {name}")
    over = Path(inst["overwrite_dir"])
    if over.is_dir():
        loose = [p for p in over.rglob("*") if p.is_file()][:1]
        if loose:
            notes.append(f"overwrite/ is not empty - a tool wrote into the virtual Data and the output is "
                         f"sitting outside every mod ({over})")
    no_meta = sorted(m["name"] for m in rows if m["enabled"] and not m["separator"] and not m["unmanaged"]
                     and (mods_dir / m["name"]).is_dir() and not (mods_dir / m["name"] / "meta.ini").is_file())
    if no_meta:
        notes.append(f"{len(no_meta)} enabled mods have no meta.ini, so nothing can check them for updates "
                     f"(e.g. {', '.join(no_meta[:3])})")
    downloads = Path(inst["downloads_dir"])
    if not downloads.is_dir():
        notes.append(f"download_directory does not exist: {downloads}")
    else:
        others = [i for i in find_instances() if i.parent != Path(inst["ini"]).parent]
        for other in others:
            try:
                if Path(resolve(str(other.parent))["downloads_dir"]).resolve() == downloads.resolve():
                    notes.append(f"shares its download folder with {other.parent.name} ({downloads}) - "
                                 "clearing it there empties it here too")
            except SystemExit:
                continue
    return dict(instance=inst["name"], profile=profile or inst["selected_profile"],
                listed=len(listed), on_disk=len(on_disk), missing=missing, orphans=orphans,
                problems=problems, notes=notes)


# --------------------------------------------------------------------------- launching tools inside the VFS


def tools(inst: dict) -> list[str]:
    """The executables MO2 is configured to launch (its [customExecutables] titles), which is the list of
    things that must run inside the virtual Data tree: xEdit, Synthesis, Nemesis, DynDOLOD, BodySlide."""
    text = Path(inst["ini"]).read_text(encoding="utf-8", errors="replace")
    return [_unqt(m.group(1)) for m in re.finditer(r"^\d+\\title=(.*)$", text, re.M)]


def run(inst: dict, tool: str, profile: str | None = None, args: list[str] | None = None):
    """Hand the job to MO2, which is the only way the tool sees the virtual Data tree."""
    exe = Path(inst["ini"]).parent / "ModOrganizer.exe"
    if not exe.is_file():
        die(f"no ModOrganizer.exe next to {inst['ini']}; a managed instance is launched from its own install")
    have = tools(inst)
    if tool not in have:
        die(f"{tool!r} is not one of this instance's executables. `um mo2 tools` lists them; add it in MO2 "
            f"first (Tools > Executables). Closest: {', '.join(t for t in have if tool.lower() in t.lower()) or 'none'}")
    cmd = [to_win(exe) if is_wsl() else str(exe)]
    if profile or inst["selected_profile"]:
        cmd += ["-p", profile or inst["selected_profile"]]
    cmd += [f"moshortcut://:{tool}", *(args or [])]
    print("launching through MO2:", " ".join(cmd))
    print("  (MO2 must not already be running, and it stays open while the tool does)")
    try:
        subprocess.Popen(cmd, cwd=str(exe.parent))
    except OSError as e:
        die(f"could not start MO2: {e}")


# --------------------------------------------------------------------------- CLI


def main(a):
    c = a.cmd
    if c == "list":
        inis = find_instances()
        wj = {Path(to_posix(w["install"])).name: w for w in wabbajack_installs()}
        rows = []
        for ini in inis:
            try:
                inst = resolve(str(ini))
            except SystemExit:
                continue
            rows.append(inst)
        if a.json:
            return emit(rows, True)
        if not rows:
            die("no Mod Organizer 2 instances found (looked under %LOCALAPPDATA%\\ModOrganizer, Wabbajack's "
                "recorded install folders, and two levels down each drive)")
        for inst in rows:
            mods = Path(inst["mods_dir"])
            n = sum(1 for _ in mods.glob("*/")) if mods.is_dir() else 0
            built = inst["wabbajack"] or wj.get(Path(inst["root"]).name)
            tag = f"  [Wabbajack: {built['title'] or Path(built['modlist']).stem}]" if built else ""
            kind = "portable" if inst["portable"] else "managed"
            print(f"{inst['root']}")
            print(f"   {kind}, {inst['game'] or '?'}, MO2 {inst['mo2_version'] or '?'}, {n} mods, "
                  f"profile {inst['selected_profile'] or '?'}{tag}")
        return
    inst = resolve(a.instance)
    if c == "info":
        if a.json:
            return emit(inst, True)
        print(f"{inst['name']}  ({'portable' if inst['portable'] else 'managed'}, MO2 {inst['mo2_version'] or '?'})")
        print(f"  game:          {inst['game'] or '?'}  {('(' + inst['game_edition'] + ')') if inst['game_edition'] else ''}")
        print(f"  gamePath:      {inst['game_path'] or '?'}" + ("   <- Stock Game: a copy inside the instance, "
              "not the store install" if inst["stock_game"] else ""))
        print(f"  root:          {inst['root']}")
        for key in ("mods_dir", "profiles_dir", "downloads_dir", "overwrite_dir"):
            print(f"  {key.replace('_dir', '/') + ':':14} {inst[key]}")
        print(f"  profiles:      {', '.join(profiles(inst)) or 'none'}")
        print(f"  selected:      {inst['selected_profile'] or '?'}")
        mods = Path(inst["mods_dir"])
        if mods.is_dir():
            try:
                rows = mod_order(inst)
                real = [m for m in rows if not m["separator"]]
                print(f"  mods:          {sum(1 for _ in mods.glob('*/'))} folders, {len(real)} in the profile, "
                      f"{sum(1 for m in real if m['enabled'])} enabled, "
                      f"{sum(1 for m in rows if m['separator'])} separators")
            except SystemExit:
                pass
        if inst["wabbajack"]:
            w = inst["wabbajack"]
            print(f"  built by:      Wabbajack - {w['title'] or '?'} by {w['author'] or '?'} ({w['game'] or '?'})")
            print(f"             {w['modlist'] or '?'}")
        if inst["compiler_settings"]:
            print(f"  also:          {', '.join(inst['compiler_settings'])} (the list author's build settings, "
                  "shipped with the list - not a recipe you can rebuild here)")
        return
    if c == "profiles":
        names = profiles(inst)
        if a.json:
            return emit(names, True)
        for n in names:
            print(("* " if n == inst["selected_profile"] else "  ") + n)
        return
    if c == "mods":
        rows = [m for m in mod_order(inst, a.profile) if not m["separator"] or a.separators]
        if a.enabled:
            rows = [m for m in rows if m["enabled"]]
        if a.json:
            return emit(rows, True)
        print(f"{len(rows)} entries, highest priority first (the winner of a conflict is the one nearer the top):")
        for m in rows[:a.limit]:
            state = "+" if m["enabled"] else ("*" if m["unmanaged"] else "-")
            kind = "  --- separator" if m["separator"] else ""
            print(f"  {m['priority']:>5} {state} {m['name'][:66]}{kind}")
        if len(rows) > a.limit:
            print(f"  ... {len(rows) - a.limit} more (--limit, or --json)")
        return
    if c == "plugins":
        rows = plugins(inst, a.profile)
        if a.json:
            return emit(rows, True)
        print(f"{len(rows)} plugins, {sum(1 for p in rows if p['enabled'])} enabled, in load order:")
        for p in rows[:a.limit]:
            print(f"  {p['index']:>5} {'*' if p['enabled'] else ' '} {p['name']}")
        if len(rows) > a.limit:
            print(f"  ... {len(rows) - a.limit} more (--limit, or --json)")
        return
    if c == "conflicts":
        if a.file:
            res = winner(inst, a.file, a.profile)
            if a.json:
                return emit(res, True)
            if not res["providers"]:
                print(f"no enabled mod provides {res['file']} (nor does overwrite/)")
                return
            print(f"{res['file']}: {res['count']} provider(s), the winner first")
            for i, p in enumerate(res["providers"]):
                print(f"  {'WINS  ' if i == 0 else 'hidden'} {p['priority']:>9}  {p['mod']}")
            return
        if a.mod:
            res = mod_conflicts(inst, a.mod, a.profile, a.limit)
            if a.json:
                return emit(res, True)
            print(f"{res['mod']} (priority {res['priority']}, {'enabled' if res['enabled'] else 'disabled'}): "
                  f"{res['checked']} files checked" + (" (--limit for more)" if res["truncated"] else ""))
            print(f"  overrides another mod in {len(res['wins'])} files, is overridden in {len(res['loses'])}")
            for row in res["loses"][:a.limit if a.limit < 40 else 20]:
                print(f"    hidden by {row['against'][:44]:<44} {row['file'][:60]}")
            return
        die("pass --file <path inside Data> or --mod <mod folder name>")
    if c == "check":
        res = check(inst, a.profile)
        if a.json:
            return emit(res, True)
        print(f"{res['instance']} / {res['profile']}: {res['listed']} mods in the profile, {res['on_disk']} "
              f"folders on disk")
        for x in res["problems"]:
            print("  PROBLEM ", x)
        for x in res["notes"]:
            print("  note    ", x)
        if not res["problems"] and not res["notes"]:
            print("  nothing to flag")
        return
    if c == "tools":
        names = tools(inst)
        if a.json:
            return emit(names, True)
        if not names:
            print("no executables configured in this instance (MO2: Tools > Executables)")
        for n in names:
            print("  " + n)
        return
    if c == "run":
        run(inst, a.tool, a.profile, a.args)
        return


def register(sub):
    import argparse
    p = sub.add_parser("mo2", help="Mod Organizer 2: read an instance, resolve conflicts, run a tool in its VFS",
                       description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    cs = p.add_subparsers(dest="cmd", metavar="<cmd>")

    q = cs.add_parser("list", help="Mod Organizer 2 instances on this machine")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=main)

    def inst_parser(name, helptext):
        q = cs.add_parser(name, help=helptext)
        q.add_argument("instance", help="the instance folder, its ModOrganizer.ini, or a managed instance's name")
        q.add_argument("--json", action="store_true")
        q.set_defaults(func=main)
        return q

    inst_parser("info", "game, Stock Game, folders, profiles, counts, Wabbajack build")
    inst_parser("profiles", "the instance's profiles (* marks the selected one)")

    q = inst_parser("mods", "the profile's mods, highest priority first")
    q.add_argument("--profile")
    q.add_argument("--enabled", action="store_true", help="only the ones switched on")
    q.add_argument("--separators", action="store_true", help="keep the UI separators in the list")
    q.add_argument("--limit", type=int, default=40)

    q = inst_parser("plugins", "the plugin load order")
    q.add_argument("--profile")
    q.add_argument("--limit", type=int, default=40)

    q = inst_parser("conflicts", "who actually provides a file, or what one mod wins and loses")
    q.add_argument("--profile")
    q.add_argument("--file", help="a path inside Data, e.g. \"meshes/actors/character/skeleton.nif\"")
    q.add_argument("--mod", help="a mod folder name, to see what it overrides and what hides it")
    q.add_argument("--limit", type=int, default=300, help="files to check with --mod")

    q = inst_parser("check", "drift and the states that quietly break a list")
    q.add_argument("--profile")

    inst_parser("tools", "the executables MO2 can launch (these are the ones that need the VFS)")

    q = inst_parser("run", "launch one of them through MO2, so it sees the virtual Data")
    q.add_argument("--tool", required=True, help="a title from `um mo2 tools`")
    q.add_argument("--profile")
    q.add_argument("args", nargs="*", help="extra arguments for the tool")
