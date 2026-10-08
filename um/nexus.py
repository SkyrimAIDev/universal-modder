"""Nexus Mods as a source of gameplay changes: find mods, see what a setup is missing, fetch and install.

    um nexus games skyrim                        # name -> Nexus domain + id (what every other command wants)
    um nexus search "alternate start" --game skyrimspecialedition
    um nexus search --game skyrimspecialedition --category Gameplay --sort updated --limit 20
    um nexus categories skyrimspecialedition     # the categories its most-downloaded mods sit in
    um nexus show skyrimspecialedition 272       # version, author, requirements, description
    um nexus files skyrimspecialedition 272      # the downloadable files, newest first
    um nexus updates "D:\\LoreRim" --enabled-only   # a Mod Organizer 2 setup vs what Nexus has now
    um nexus key                                 # is NEXUS_API_KEY set, and is the account Premium
    um nexus download skyrimspecialedition 272 --out downloads     # Premium, or:
    um nexus download "nxm://skyrimspecialedition/mods/272/files/..." --out downloads
    um nexus install downloads/Thing-272-4-2.7z --to "D:\\LoreRim\\mods" --yes

Two APIs, deliberately: searching, reading and update-checking go through the keyless v2 GraphQL endpoint,
so the common case needs no account at all. Only `download` and `key` touch the v1 REST API, which wants a
personal key (NEXUS_API_KEY in your own shell, or NEXUS_API_KEY_FILE pointing at a file - never a key
committed to a repo or pasted into a chat).

Lessons this encodes: generated download links are Premium-only, so a free account hands over an nxm:// URL
from the website's "Mod manager download" button instead and this reads the key/expires out of it; a 4,000
mod Mod Organizer list cannot be checked one request at a time, so updates batches through modsByUid (a mod's
uid is gameId << 32 | modId) and reports anything it could not resolve rather than quietly skipping it;
MO2's meta.ini already records the Nexus mod id, so update-checking needs no hashing and no name guessing;
and a downloaded archive is a stranger's zip, so install refuses any member whose path leaves the target.
"""
from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from um.common import die, emit, safe_relpath, to_posix

GRAPHQL = "https://api.nexusmods.com/v2/graphql"
REST = "https://api.nexusmods.com/v1"
SITE = "https://www.nexusmods.com"
UA = "universal-modder (+https://github.com/rehan-remade/universal-modder)"
# modsByUid never returns more than 80 nodes however many uids it is given; staying well under that and
# checking what came back is what keeps a 4,000 mod list honest instead of silently truncated.
BATCH = 50
PAUSE = 0.15                  # between batches: Nexus asks for no bulk scraping

# Mod Organizer's own game names (meta.ini's gameName, and ModOrganizer.ini's display name) -> Nexus domain.
# Only mappings checked against the live API are here; anything else is resolved by name, and the VR editions
# are deliberately absent because the v2 game list has no entry for them - pass --game explicitly for those.
MO2_DOMAINS = {
    "skyrim": "skyrim", "skyrimse": "skyrimspecialedition", "skyrim special edition": "skyrimspecialedition",
    "oblivion": "oblivion", "oblivion remastered": "oblivionremastered", "morrowind": "morrowind",
    "fallout3": "fallout3", "fallout 3": "fallout3", "falloutnv": "newvegas", "fallout new vegas": "newvegas",
    "newvegas": "newvegas", "fallout4": "fallout4", "fallout 4": "fallout4", "starfield": "starfield",
    "enderal": "enderal", "enderalse": "enderalspecialedition", "enderal special edition": "enderalspecialedition",
    "cyberpunk2077": "cyberpunk2077", "cyberpunk 2077": "cyberpunk2077",
}
SORTS = {"downloads": "downloads", "endorsements": "endorsements", "updated": "updatedAt",
         "new": "createdAt", "name": "name", "relevance": "relevance", "size": "size"}
# Nexus' own file buckets. MAIN is what a mod page offers first; OLD_VERSION/ARCHIVED/REMOVED are history.
CURRENT_FILES = ("MAIN", "UPDATE", "OPTIONAL", "MISCELLANEOUS")

MOD_FIELDS = """modId name version author summary downloads endorsements createdAt updatedAt category
                adultContent status pictureUrl game { id name domainName }"""


# --------------------------------------------------------------------------- the keyless GraphQL API


def gql(query: str, variables: dict | None = None, timeout: float = 60) -> dict:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA}
    for attempt in range(3):
        req = urllib.request.Request(GRAPHQL, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            die(f"nexus graphql HTTP {e.code}: {e.read()[:400].decode(errors='replace')}")
        except urllib.error.URLError as e:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            die(f"nexus graphql: {e.reason}")
    if payload.get("errors"):
        die("nexus graphql: " + "; ".join(str(e.get("message", e)) for e in payload["errors"]))
    return payload["data"]


# --------------------------------------------------------------------------- the v1 REST API (needs a key)


def api_key(required: bool = True) -> str | None:
    """The personal key, from the environment only. Deliberately not read out of a .env in the working
    directory: a key found next to whatever folder you happen to be in is not a key you chose to use."""
    k = (os.environ.get("NEXUS_API_KEY") or "").strip()
    if not k and os.environ.get("NEXUS_API_KEY_FILE"):
        try:
            k = Path(os.environ["NEXUS_API_KEY_FILE"]).expanduser().read_text(encoding="utf-8").strip()
        except OSError as e:
            die(f"NEXUS_API_KEY_FILE is set but unreadable: {e}")
    if k:
        return k
    if required:
        die("this needs a personal Nexus API key. Make one under 'API Key' at "
            f"{SITE}/users/myaccount?tab=api and `export NEXUS_API_KEY=...` (or set NEXUS_API_KEY_FILE to a "
            "file holding it). Keep it in your own shell: not in a file in a repo, not pasted into a chat. "
            "Searching, `show`, `files` and `updates` all work without one.")
    return None


def rest(path: str, timeout: float = 60) -> tuple[dict, dict]:
    """GET a v1 endpoint with the key. Returns (json, rate-limit headers)."""
    req = urllib.request.Request(REST + path, headers={"Accept": "application/json", "User-Agent": UA})
    # add_unredirected_header: urllib replays a request's headers on a 3xx, and the key has no business
    # going anywhere but api.nexusmods.com.
    req.add_unredirected_header("apikey", api_key())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            limits = {k: v for k, v in r.headers.items() if k.lower().startswith("x-rl-")}
            return json.loads(r.read() or b"{}"), limits
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode(errors="replace")
        if e.code == 401:
            die(f"Nexus rejected the API key (401): {detail}. Check NEXUS_API_KEY against {SITE}/users/myaccount?tab=api")
        if e.code == 403:
            die(f"Nexus refused this (403): {detail}. Generated download links are a Premium feature - for a free "
                "account, click 'Mod manager download' on the mod page and pass the nxm:// URL to `um nexus download`.")
        if e.code == 429:
            die(f"Nexus rate limit reached (429): {detail}. The free allowance is per hour and per day; wait it out.")
        die(f"nexus v1 HTTP {e.code} on {path}: {detail}")
    except urllib.error.URLError as e:
        die(f"nexus v1 {path}: {e.reason}")


# --------------------------------------------------------------------------- games


_GAME_CACHE: dict[str, dict] = {}


def resolve_game(text: str) -> dict:
    """A Nexus domain, a game's real name, or Mod Organizer's name for it -> {id, name, domainName}."""
    key = str(text or "").strip()
    if not key:
        die("which game? pass --game with a Nexus domain (skyrimspecialedition) or a name (\"Baldur's Gate 3\")")
    low = key.lower()
    if low in _GAME_CACHE:
        return _GAME_CACHE[low]
    for candidate in (MO2_DOMAINS.get(low), low if re.fullmatch(r"[a-z0-9]+", low) else None):
        if not candidate:
            continue
        g = gql("query G($d:String!){ game(domainName:$d){ id name domainName } }", {"d": candidate})["game"]
        if g:
            _GAME_CACHE[low] = g
            return g
    hits = search_games(key, limit=5)
    exact = [g for g in hits if g["name"].lower() == low or g["domainName"].lower() == low]
    if exact or len(hits) == 1:
        g = (exact or hits)[0]
        _GAME_CACHE[low] = g
        return g
    if hits:
        die(f"{key!r} matches several games; pass one of these domains with --game:\n  "
            + "\n  ".join(f"{g['domainName']:<28} {g['name']}" for g in hits))
    die(f"no Nexus game matches {key!r}. `um nexus games <text>` lists what does.")


def search_games(text: str | None = None, limit: int = 20) -> list[dict]:
    q = """query S($n:[GameNameFieldFilterValue!],$count:Int!){
             games(filter:{name:$n}, sort:[{mods:{direction:DESC}}], count:$count){
               nodes{ id name domainName modCount downloadCount } } }"""
    name = [{"value": text, "op": "WILDCARD"}] if text else []
    nodes = gql(q, {"n": name, "count": limit})["games"]["nodes"]
    return sorted(nodes, key=lambda g: -(g.get("modCount") or 0))


# --------------------------------------------------------------------------- search / show / files


def search(game: str, text: str | None = None, category: str | None = None, sort: str = "downloads",
           limit: int = 20, offset: int = 0, adult: bool = False) -> dict:
    g = resolve_game(game)
    if sort not in SORTS:
        die(f"--sort must be one of {', '.join(SORTS)}")
    filt: dict = {"gameDomainName": [{"value": g["domainName"], "op": "EQUALS"}]}
    if text:
        filt["name"] = [{"value": text, "op": "WILDCARD"}]
    if category:
        filt["categoryName"] = [{"value": category, "op": "EQUALS"}]
    if not adult:
        filt["adultContent"] = [{"value": False, "op": "EQUALS"}]
    q = ("query S($f:ModsFilter,$s:[ModsSort!],$count:Int!,$offset:Int!){"
         " mods(filter:$f, sort:$s, count:$count, offset:$offset){ totalCount nodes{ " + MOD_FIELDS + " } } }")
    sort_arg = [{SORTS[sort]: {"direction": "ASC" if sort == "name" else "DESC"}}]
    page = gql(q, {"f": filt, "s": sort_arg, "count": min(limit, 80), "offset": offset})["mods"]
    return dict(game=g, total=page["totalCount"], mods=page["nodes"])


def show(game: str, mod_id: int) -> dict:
    g = resolve_game(game)
    q = ("query M($m:ID!,$g:ID!){ mod(modId:$m, gameId:$g){ " + MOD_FIELDS + " description fileSize uploader{ name } } }")
    mod = gql(q, {"m": str(mod_id), "g": str(g["id"])})["mod"]
    if not mod:
        die(f"no mod {mod_id} on {g['domainName']}")
    mod["url"] = mod_url(g["domainName"], mod_id)
    return mod


def files(game: str, mod_id: int, everything: bool = False) -> list[dict]:
    g = resolve_game(game)
    q = """query F($m:ID!,$g:ID!){ modFiles(modId:$m, gameId:$g){
             fileId name version category sizeInBytes size date description primary uri } }"""
    out = gql(q, {"m": str(mod_id), "g": str(g["id"])})["modFiles"] or []
    if not everything:
        out = [f for f in out if f.get("category") in CURRENT_FILES]
    return sorted(out, key=lambda f: (-(f.get("date") or 0), f.get("name") or ""))


def mod_url(domain: str, mod_id) -> str:
    return f"{SITE}/{domain}/mods/{mod_id}"


def file_bytes(f: dict) -> int:
    """A file's size in bytes. sizeInBytes is a GraphQL BigInt, so it arrives as a string; `size` is in KB."""
    raw = f.get("sizeInBytes")
    if raw not in (None, ""):
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return int(f.get("size") or 0) * 1024


# --------------------------------------------------------------------------- Mod Organizer 2 setups


META_KEYS = ("gameName", "modid", "version", "newestVersion", "installationFile", "repository", "ignoredVersion")


def read_meta(path: Path) -> dict:
    """The handful of [General] keys we need out of a mod's meta.ini. Scanned line by line on purpose: MO2
    pastes the whole Nexus description into these files, so they run to tens of KB and configparser would
    read (and choke on) all of it."""
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


def mo2_root(path: Path) -> Path | None:
    return path if (path / "ModOrganizer.ini").exists() else None


def mo2_setting(ini: Path, key: str) -> str | None:
    """A value out of ModOrganizer.ini, unwrapping Qt's @ByteArray(...) form."""
    cp = configparser.RawConfigParser(strict=False)
    try:
        cp.read(ini, encoding="utf-8")
    except (configparser.Error, OSError):
        return None
    for section in cp.sections():
        if cp.has_option(section, key):
            v = (cp.get(section, key) or "").strip()
            m = re.fullmatch(r"@ByteArray\((.*)\)", v, re.S)
            return (m.group(1) if m else v).replace("\\\\", "\\")
    return None


def enabled_mods(root: Path, profile: str | None = None) -> set[str] | None:
    """The mod folder names a profile has switched on ("+Name" in its modlist.txt), or None if unreadable."""
    name = profile or mo2_setting(root / "ModOrganizer.ini", "selected_profile")
    if not name:
        return None
    listing = root / "profiles" / name / "modlist.txt"
    if not listing.exists():
        die(f"no such profile: {listing} (profiles here: "
            + ", ".join(sorted(p.name for p in (root / 'profiles').glob('*') if p.is_dir())) + ")")
    on = set()
    for line in listing.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("+") and not line.endswith("_separator"):
            on.add(line[1:])
    return on


def installed(path: str, profile: str | None = None, enabled_only: bool = False) -> list[dict]:
    """Every Nexus-sourced mod in an MO2 instance (or any folder of mod folders holding a meta.ini)."""
    root = Path(to_posix(path)).expanduser()
    if not root.is_dir():
        die(f"not a folder: {root}")
    mods_dir = root / "mods" if (root / "mods").is_dir() else root
    on = enabled_mods(root, profile) if (mo2_root(root) or profile) else None
    if enabled_only and on is None:
        die(f"--enabled-only needs an MO2 instance (a folder with ModOrganizer.ini); {root} has none")
    out = []
    for meta in sorted(mods_dir.glob("*/meta.ini")):
        folder = meta.parent.name
        if enabled_only and on is not None and folder not in on:
            continue
        m = read_meta(meta)
        mod_id = (m.get("modid") or "").strip()
        if not mod_id.isdigit() or int(mod_id) <= 0:
            continue                                  # hand-made or Wabbajack-built mods carry no Nexus id
        out.append(dict(folder=folder, mod_id=int(mod_id), game=m.get("gameName") or "",
                        version=m.get("version") or "", newest=m.get("newestVersion") or "",
                        ignored=m.get("ignoredVersion") or "", file=m.get("installationFile") or "",
                        enabled=None if on is None else folder in on))
    return out


# --------------------------------------------------------------------------- update checking


def version_key(s: str) -> tuple:
    """Comparable digits out of a freeform version ("4.2.6a", "v1.0.0.0", "5.1SE" all occur in the wild).
    Trailing zeros go: MO2 stores every version padded to x.y.z.w, so its 1.1.0.0 and Nexus' 1.1 are one
    release, and without this a whole modlist reads as "ahead of Nexus"."""
    nums = [int(n) for n in re.findall(r"\d+", str(s or ""))[:6]]
    while nums and nums[-1] == 0:
        nums.pop()
    return tuple(nums)


def version_tag(s: str) -> str:
    """The letters in a version, which is where a 4.2.6a-style re-release of 4.2.6 hides."""
    return re.sub(r"[^a-z]", "", str(s or "").strip().lower().lstrip("v"))


def compare_versions(local: str, remote: str) -> str:
    """same / newer / older / differs, where "differs" means the two carry no comparable numbers (or differ
    only by a letter) and a human should look rather than be told a direction."""
    a_raw, b_raw = (local or "").strip(), (remote or "").strip()
    if a_raw.lower().lstrip("v") == b_raw.lower().lstrip("v"):
        return "same"
    a, b = version_key(a_raw), version_key(b_raw)
    if not a or not b:
        return "differs"
    if a != b:
        return "newer" if b > a else "older"
    return "same" if version_tag(a_raw) == version_tag(b_raw) else "differs"


def uid(game_id: int, mod_id: int) -> str:
    """Nexus' mod uid packs both ids into one 64-bit value, which is what the batch query takes."""
    return str((int(game_id) << 32) | int(mod_id))


def fetch_mods(game_id: int, mod_ids: list[int], progress: bool = False) -> dict[int, dict]:
    """modId -> its current record, in batches. Whatever Nexus doesn't return is simply absent, so the
    caller can report it instead of mistaking a removed mod for an up-to-date one."""
    q = "query U($uids:[ID!]!,$count:Int!){ modsByUid(uids:$uids, count:$count){ nodes{ " + MOD_FIELDS + " } } }"
    out: dict[int, dict] = {}
    for i in range(0, len(mod_ids), BATCH):
        chunk = mod_ids[i:i + BATCH]
        nodes = gql(q, {"uids": [uid(game_id, m) for m in chunk], "count": BATCH})["modsByUid"]["nodes"]
        for n in nodes:
            out[int(n["modId"])] = n
        # \r only reads as progress on a terminal; piped or captured it would be one line per batch
        if progress and sys.stderr.isatty():
            print(f"\r  {min(i + BATCH, len(mod_ids))}/{len(mod_ids)} checked", end="", file=sys.stderr)
        if i + BATCH < len(mod_ids):
            time.sleep(PAUSE)
    if progress and sys.stderr.isatty():
        print("", file=sys.stderr)
    return out


def updates(path: str, profile: str | None = None, enabled_only: bool = False, progress: bool = True,
            game: str | None = None) -> dict:
    """Compare what a setup has installed against what Nexus lists now. Each mod's game comes from its own
    meta.ini unless `game` overrides it, which is what a VR instance needs: Nexus' public API has no entry
    for Skyrim VR or Fallout 4 VR, while the mods in such a setup were downloaded from the flat game's
    pages anyway and resolve perfectly against that domain."""
    mods = installed(path, profile, enabled_only)
    if not mods:
        die(f"no Nexus-sourced mods under {path}: looked for */meta.ini carrying a modid")
    forced = resolve_game(game) if game else None
    # Resolve once per distinct MO2 name (a failed lookup is not cached, so per-mod would re-query), then
    # merge by the domain it resolved to: a real list spells the same game both "SkyrimSE" and
    # "Skyrim Special Edition", and those belong in one batch run.
    by_name: dict[str, list[dict]] = {}
    for m in mods:
        by_name.setdefault("" if forced else m["game"], []).append(m)
    groups: dict[str, dict] = {}
    unresolved = []
    for game_name, group in sorted(by_name.items()):
        g = forced
        if not g and game_name:
            try:
                g = resolve_game(game_name)
            except SystemExit:
                g = None
        if not g:
            why = (f"MO2 calls this game {game_name!r} and no Nexus domain matches it"
                   if game_name else "this mod's meta.ini names no game")
            unresolved += [dict(m, why=why + " - re-run with --game <domain> to check these anyway") for m in group]
            continue
        groups.setdefault(g["domainName"], {"game": g, "mods": []})["mods"] += group
    rows = []
    for domain, slot in sorted(groups.items()):
        g, group = slot["game"], slot["mods"]
        # Several mod folders often come from one Nexus page (a mod and its addons), so ask once per id.
        ids = sorted({m["mod_id"] for m in group})
        if progress:
            print(f"{len(group)} mods ({len(ids)} Nexus pages) to check against {domain}", file=sys.stderr)
        remote = fetch_mods(g["id"], ids, progress=progress)
        for m in group:
            r = remote.get(m["mod_id"])
            if not r:
                unresolved.append(dict(m, why="Nexus returned nothing for this mod id (removed, hidden or wrong)"))
                continue
            state = compare_versions(m["version"], r["version"])
            if state != "same" and m["ignored"] and compare_versions(m["ignored"], r["version"]) == "same":
                state = "ignored"          # MO2 remembers a version you chose to skip; don't nag about it
            rows.append(dict(folder=m["folder"], mod_id=m["mod_id"], enabled=m["enabled"], state=state,
                             local=m["version"], remote=r["version"], name=r["name"],
                             category=r.get("category"), status=r.get("status"), updated=r.get("updatedAt"),
                             url=mod_url(g["domainName"], m["mod_id"])))
    order = {"newer": 0, "differs": 1, "older": 2, "ignored": 3, "same": 4}
    rows.sort(key=lambda r: (order.get(r["state"], 9), r["folder"].lower()))
    counts = {s: sum(1 for r in rows if r["state"] == s) for s in order}
    return dict(checked=len(rows), counts=counts, mods=rows, unresolved=unresolved)


# --------------------------------------------------------------------------- downloading


def nxm_parse(url: str) -> dict:
    """nxm://<domain>/mods/<id>/files/<id>?key=..&expires=.. - what the website's "Mod manager download"
    button hands to a mod manager. That key/expires pair is what lets a free account generate a link."""
    u = urllib.parse.urlparse(url)
    parts = [p for p in u.path.split("/") if p]
    if u.scheme != "nxm" or len(parts) < 4 or parts[0] != "mods" or parts[2] != "files":
        die(f"cannot read {url!r}; expected nxm://<game>/mods/<mod id>/files/<file id>?key=...&expires=...")
    qs = urllib.parse.parse_qs(u.query)
    return dict(domain=u.netloc, mod_id=int(parts[1]), file_id=int(parts[3]),
                key=(qs.get("key") or [None])[0], expires=(qs.get("expires") or [None])[0])


def download_links(domain: str, mod_id: int, file_id: int, nxm: dict | None = None) -> list[dict]:
    path = f"/games/{domain}/mods/{mod_id}/files/{file_id}/download_link.json"
    if nxm and nxm.get("key"):
        path += "?" + urllib.parse.urlencode({"key": nxm["key"], "expires": nxm["expires"] or ""})
    data, limits = rest(path)
    if limits:
        print("  nexus rate limit: " + ", ".join(f"{k.replace('X-RL-', '')}={v}" for k, v in sorted(limits.items())),
              file=sys.stderr)
    links = data if isinstance(data, list) else [data]
    links = [l for l in links if isinstance(l, dict) and l.get("URI")]
    if not links:
        die(f"Nexus gave no download URI for {domain}/mods/{mod_id}/files/{file_id}; it answered: {json.dumps(data)[:300]}")
    return links


def download(out: str = "downloads", game: str | None = None, mod_id: int | None = None,
             file_id: int | None = None, nxm: str | None = None) -> Path:
    if nxm:
        info = nxm_parse(nxm)
        domain, mod_id, file_id = info["domain"], info["mod_id"], info["file_id"]
    else:
        info = None
        if not (game and mod_id):
            die("give a game and a mod id, or an nxm:// URL")
        domain = resolve_game(game)["domainName"]
        if file_id is None:
            avail = [f for f in files(domain, mod_id) if f.get("category") == "MAIN"] or files(domain, mod_id)
            if not avail:
                die(f"{mod_url(domain, mod_id)} lists no downloadable files")
            file_id = avail[0]["fileId"]
            print(f"picked the newest MAIN file: {avail[0]['name']} v{avail[0]['version']} (--file {file_id})")
    link = download_links(domain, mod_id, file_id, info)[0]
    uri = link["URI"]
    # The name comes out of the CDN's URL, so it is checked like any other remote path before it is used.
    raw = urllib.parse.unquote(Path(urllib.parse.urlparse(uri).path).name)
    rel = safe_relpath(raw) if raw else None
    name = raw if rel is not None and len(rel.parts) == 1 else f"{mod_id}-{file_id}.bin"
    dest = Path(to_posix(out)).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / name
    print(f"downloading {name} from {link.get('short_name') or link.get('name') or 'Nexus'}")
    req = urllib.request.Request(uri, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=600) as r, open(target, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
    except (urllib.error.URLError, OSError) as e:
        target.unlink(missing_ok=True)
        die(f"download failed: {e}")
    print(f"{target}  ({target.stat().st_size / 2**20:.1f} MB)")
    return target


# --------------------------------------------------------------------------- installing


#   "(2)Barbarian Bodypaints - CBBE-31826-1-0-1579138592.7z" -> name, 31826, 1-0, stamp
# Both <name> and <ver> are lazy on purpose: a greedy name swallows the mod id (it reads the trailing "-1-0-"
# as id and version), and a greedy version swallows the timestamp.
NEXUS_FILENAME = re.compile(r"^(?P<name>.+?)-(?P<mod_id>\d+)-(?P<ver>\w[\w.-]*?)-(?P<stamp>\d{9,})\.\w+$")


def from_filename(path: Path) -> dict:
    """Nexus names a download "<mod name>-<mod id>-<version with dashes>-<unix stamp>.<ext>", which is
    where MO2 gets a mod's id when you install by hand. Best effort: flags win over this."""
    m = NEXUS_FILENAME.match(path.name)
    if not m:
        return {}
    return dict(mod_id=int(m.group("mod_id")), version=m.group("ver").replace("-", "."), name=m.group("name"))


def neighbour_game(mods_dir: Path) -> str:
    """MO2's own name for the game, copied from whatever is already installed next door. Guessing the casing
    ("SkyrimSE", not "skyrimse") would be wrong often enough to matter, and the mods folder we are writing
    into already holds the right answer; an empty string when it is the first mod in a fresh folder."""
    counts: dict[str, int] = {}
    for meta in sorted(mods_dir.glob("*/meta.ini"))[:200]:
        name = read_meta(meta).get("gameName") or ""
        if name:
            counts[name] = counts.get(name, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def sevenzip() -> str | None:
    """7-Zip, for the .7z and .rar archives Nexus is full of. Looked up rather than required: .zip needs
    nothing, and a missing 7z should name itself rather than fail obscurely."""
    for name in ("7z", "7za", "7zz"):
        found = shutil.which(name)
        if found:
            return found
    for guess in (r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if Path(to_posix(guess)).exists():
            return to_posix(guess)
    return None


def archive_members(archive: Path) -> list[str]:
    """Every path an archive would write, so they can be vetted before a single byte lands."""
    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive) as zf:
            return [i.filename for i in zf.infolist()]
    exe = sevenzip()
    if not exe:
        die(f"{archive.name} needs 7-Zip to unpack (.zip is handled natively). Install 7-Zip "
            "(https://www.7-zip.org) or put 7z on PATH, then run this again.")
    r = subprocess.run([exe, "l", "-ba", "-slt", str(archive)], capture_output=True, text=True)
    if r.returncode:
        die(f"7z could not read {archive.name}: {(r.stderr or r.stdout).strip()[-400:]}")
    return [line.partition("=")[2].strip() for line in r.stdout.splitlines() if line.startswith("Path = ")]


def vet_members(members: list[str], archive: Path, dest: Path) -> list[str]:
    """Refuse an archive that would write outside dest. These come from strangers on the internet, and
    both zip members and 7z entries can name ../x or a drive root."""
    bad = [m for m in members if m and safe_relpath(m, allow_backslash=True) is None]
    if bad:
        die(f"{archive.name} would write outside {dest} ({bad[0]!r}" + (f" and {len(bad) - 1} more" if len(bad) > 1 else "")
            + "); refusing to unpack it. Open it by hand if you trust it.")
    return [m for m in members if m]


def extract(archive: Path, dest: Path):
    members = vet_members(archive_members(archive), archive, dest)
    if any(Path(m.replace("\\", "/")).name.lower() == "moduleconfig.xml" for m in members):
        print(f"NOTE: {archive.name} is a FOMOD (scripted) installer - its options are not applied by a plain "
              "unpack. Install it through Mod Organizer or Vortex if the choices matter.")
    dest.mkdir(parents=True, exist_ok=True)
    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
        return
    exe = sevenzip()
    r = subprocess.run([exe, "x", "-y", f"-o{dest}", str(archive)], capture_output=True, text=True)
    if r.returncode:
        die(f"7z failed on {archive.name}: {(r.stderr or r.stdout).strip()[-600:]}")


def install(archive: str, to: str, name: str | None = None, game: str | None = None, mod_id: int | None = None,
            version: str | None = None, yes: bool = False, force: bool = False) -> Path:
    """Unpack a downloaded archive into a mods folder, as one folder per mod the way MO2 keeps them."""
    src = Path(to_posix(archive)).expanduser()
    if not src.is_file():
        die(f"no such archive: {src}")
    guess = from_filename(src)
    folder = name or guess.get("name") or src.stem
    dest_root = Path(to_posix(to)).expanduser()
    target = dest_root / folder
    members = vet_members(archive_members(src), src, target)
    existing = target.is_dir() and any(target.iterdir())
    print(f"install {src.name} -> {target}")
    print(f"  {len(members)} entries" + (", over an existing folder" if existing else ", a new folder"))
    if existing and not force:
        die(f"{target} already exists and is not empty; pass --force to unpack over it (its current contents "
            "are snapshotted first)")
    if not yes:
        die("re-run with --yes to do it (this writes into a mod folder your game loads)")
    if existing:
        from um import backup
        backup.create(str(target), name=f"nexus-{folder}"[:60].replace(" ", "-").lower(),
                      note=f"automatic, before installing {src.name}")
    extract(src, target)
    meta_mod_id = mod_id or guess.get("mod_id")
    if meta_mod_id:
        # The same keys MO2 writes, so `um nexus updates` tracks this mod from now on.
        lines = ["[General]", f"gameName={neighbour_game(dest_root)}", f"modid={meta_mod_id}",
                 f"version={version or guess.get('version') or ''}", "repository=Nexus",
                 f"installationFile={src.name}"]
        (target / "meta.ini").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    n = sum(1 for _ in target.rglob("*"))
    print(f"installed {n} files into {target}" + ("" if meta_mod_id else " (no Nexus mod id found; pass --modid so "
                                                   "`um nexus updates` can track it)"))
    return target


# --------------------------------------------------------------------------- CLI


def key_status() -> dict:
    data, limits = rest("/users/validate.json")
    return dict(user=data.get("name"), premium=bool(data.get("is_premium")),
                supporter=bool(data.get("is_supporter")), limits=limits)


def _fmt_count(n) -> str:
    return f"{n:,}" if isinstance(n, int) else "?"


def main(a):
    c = a.cmd
    if c == "games":
        hits = search_games(a.text, a.limit)
        if a.json:
            return emit(hits, True)
        if not hits:
            die(f"no Nexus game matches {a.text!r}")
        for g in hits:
            print(f"{g['domainName']:<28} id={str(g['id']):<6} {_fmt_count(g.get('modCount')):>8} mods  {g['name']}")
        return
    if c == "search":
        res = search(a.game, a.text, a.category, a.sort, a.limit, a.offset, a.adult)
        if a.json:
            return emit(res, True)
        g = res["game"]
        print(f"{res['total']:,} mods on {g['name']} ({g['domainName']})"
              + (f" matching {a.text!r}" if a.text else "") + (f" in {a.category}" if a.category else "")
              + f", by {a.sort}:")
        for m in res["mods"]:
            flag = " [ADULT]" if m.get("adultContent") else ""
            print(f"  [{m['modId']:>7}] {m['name'][:52]:<52} v{(m['version'] or '?')[:12]:<12} "
                  f"{_fmt_count(m['downloads']):>10} dl  {m['category'] or '-':<14} {m['updatedAt'][:10]}{flag}")
            print(f"            {mod_url(g['domainName'], m['modId'])}")
        return
    if c == "categories":
        res = search(a.game, sort="downloads", limit=80)
        seen: dict[str, int] = {}
        for m in res["mods"]:
            seen[m["category"] or "-"] = seen.get(m["category"] or "-", 0) + 1
        if a.json:
            return emit(seen, True)
        print(f"categories among the 80 most-downloaded {res['game']['name']} mods "
              "(Nexus has no per-game category list in its public API):")
        for name, n in sorted(seen.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>3}  {name}")
        return
    if c == "show":
        m = show(a.game, a.mod_id)
        if a.json:
            return emit(m, True)
        print(f"{m['name']}  v{m['version']}")
        print(f"  by:        {m.get('author') or (m.get('uploader') or {}).get('name') or '?'}")
        print(f"  game:      {m['game']['name']} ({m['game']['domainName']})")
        print(f"  category:  {m.get('category') or '-'}" + ("   [ADULT]" if m.get("adultContent") else ""))
        print(f"  downloads: {_fmt_count(m['downloads'])}   endorsements: {_fmt_count(m['endorsements'])}")
        print(f"  updated:   {m['updatedAt'][:10]}   created: {m['createdAt'][:10]}   status: {m.get('status')}")
        print(f"  url:       {m['url']}")
        print(f"  summary:   {(m.get('summary') or '').strip()[:400]}")
        return
    if c == "files":
        fs = files(a.game, a.mod_id, a.all)
        if a.json:
            return emit(fs, True)
        if not fs:
            die(f"no files listed for mod {a.mod_id} (try --all for archived and old versions)")
        for f in fs:
            print(f"  --file {f['fileId']:<9} {f['category']:<14} v{(f['version'] or '?')[:14]:<14} "
                  f"{file_bytes(f) / 2**20:>8.1f} MB  {f['name'][:44]}")
        return
    if c == "updates":
        res = updates(a.path, a.profile, a.enabled_only, progress=not a.json, game=a.game)
        if a.json:
            return emit(res, True)
        counts = res["counts"]
        print(f"\n{res['checked']} mods checked: {counts['newer']} with a newer version on Nexus, "
              f"{counts['differs']} where the versions just differ, {counts['same']} the same, "
              f"{counts['older']} ahead of Nexus, {counts['ignored']} ignored in MO2")
        shown = [r for r in res["mods"] if r["state"] in ("newer", "differs")][:a.limit]
        for r in shown:
            mark = "UPDATE" if r["state"] == "newer" else "differs"
            off = "" if r["enabled"] is not False else "  (disabled)"
            print(f"  {mark:<8} {r['local'] or '?':>12} -> {r['remote'] or '?':<12} {r['folder'][:44]}{off}")
            print(f"           {r['url']}")
        if len(shown) < counts["newer"] + counts["differs"]:
            print(f"  ... and {counts['newer'] + counts['differs'] - len(shown)} more (--limit to see them, --json for all)")
        if res["unresolved"]:
            print(f"\n{len(res['unresolved'])} not checked:")
            for u in res["unresolved"][:a.limit]:
                print(f"  {u['folder'][:48]:<48} {u['why']}")
        return
    if c == "key":
        st = key_status()
        print(f"key works: {st['user']}  premium={st['premium']}  supporter={st['supporter']}")
        for k, v in sorted(st["limits"].items()):
            print(f"  {k}: {v}")
        if not st["premium"]:
            print("Not Premium, so generated download links are refused (403). Use the mod page's 'Mod manager "
                  "download' button and pass the nxm:// URL to `um nexus download`.")
        return
    if c == "download":
        if a.target.startswith("nxm://"):
            download(a.out, nxm=a.target)
        else:
            if a.mod_id is None:
                die("give a mod id: `um nexus download <game> <mod id>`, or pass an nxm:// URL")
            download(a.out, a.target, a.mod_id, a.file)
        return
    if c == "install":
        install(a.archive, a.to, a.name, a.game, a.modid, a.version, a.yes, a.force)
        return


def register(sub):
    import argparse
    p = sub.add_parser("nexus", help="Nexus Mods: find gameplay mods, check a setup for updates, download, install",
                       description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    cs = p.add_subparsers(dest="cmd", metavar="<cmd>")

    q = cs.add_parser("games", help="find a game's Nexus domain and id")
    q.add_argument("text", nargs="?", help="part of a game's name (default: the biggest games)")
    q.add_argument("--limit", type=int, default=20)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=main)

    q = cs.add_parser("search", help="search a game's mods (no API key needed)")
    q.add_argument("text", nargs="?", help="words in the mod title; omit to browse a whole category")
    q.add_argument("--game", required=True, help="Nexus domain, a game name, or MO2's name for it")
    q.add_argument("--category", help="e.g. Gameplay, Overhauls, Patches (see `um nexus categories`)")
    q.add_argument("--sort", default="downloads", choices=sorted(SORTS), help="default: downloads")
    q.add_argument("--limit", type=int, default=20, help="up to 80 per request")
    q.add_argument("--offset", type=int, default=0)
    q.add_argument("--adult", action="store_true", help="include adult-only mods (excluded by default)")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=main)

    q = cs.add_parser("categories", help="categories the game's most-downloaded mods sit in")
    q.add_argument("game")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=main)

    for name, helptext in (("show", "one mod: version, author, category, summary"),
                           ("files", "a mod's downloadable files and their file ids")):
        q = cs.add_parser(name, help=helptext)
        q.add_argument("game")
        q.add_argument("mod_id", type=int)
        if name == "files":
            q.add_argument("--all", action="store_true", help="also old, archived and removed files")
        q.add_argument("--json", action="store_true")
        q.set_defaults(func=main)

    q = cs.add_parser("updates", help="an installed setup (Mod Organizer 2, or any folder of mod folders) vs Nexus")
    q.add_argument("path", help="the MO2 instance folder (holding ModOrganizer.ini), or a mods folder")
    q.add_argument("--profile", help="MO2 profile whose enabled list to read (default: the selected one)")
    q.add_argument("--enabled-only", action="store_true", help="skip mods the profile has switched off")
    q.add_argument("--game", help="check every mod against this game instead of each meta.ini's own; needed for "
                                  "a VR instance, since Nexus' public API lists no Skyrim VR / Fallout 4 VR")
    q.add_argument("--limit", type=int, default=40, help="rows to print (--json always has them all)")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=main)

    q = cs.add_parser("key", help="check NEXUS_API_KEY: which account, Premium or not, rate limits left")
    q.set_defaults(func=main)

    q = cs.add_parser("download", help="fetch a mod file (needs a key; Premium, or an nxm:// URL)")
    q.add_argument("target", help="a game (domain or name), or a whole nxm:// URL")
    q.add_argument("mod_id", nargs="?", type=int)
    q.add_argument("--file", type=int, help="file id (default: the newest MAIN file)")
    q.add_argument("--out", default="downloads", help="folder to save into (default: downloads)")
    q.set_defaults(func=main)

    q = cs.add_parser("install", help="unpack a downloaded archive into a mods folder")
    q.add_argument("archive")
    q.add_argument("--to", required=True, help="the mods folder (e.g. an MO2 instance's mods/)")
    q.add_argument("--name", help="mod folder name (default: from the archive name)")
    q.add_argument("--game", help="game, so the written meta.ini says which one")
    q.add_argument("--modid", type=int, help="Nexus mod id (default: read out of the archive's name)")
    q.add_argument("--version", help="version for the written meta.ini")
    q.add_argument("--force", action="store_true", help="unpack over an existing folder (snapshotted first)")
    q.add_argument("--yes", action="store_true")
    q.set_defaults(func=main)
