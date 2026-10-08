"""Offline tests for the pieces that don't need a game, a GPU or a fal key.

    uv run --with pytest pytest -q
"""
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from um import backup, fal, publish, scan, sprite, video  # noqa: E402


# --------------------------------------------------------------------------- scan

def make(root: Path, files: dict):
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())


def engine_of(tmp_path, files):
    make(tmp_path, files)
    hits, _ = scan.detect(scan.Index(tmp_path))
    return hits[0][0], hits[0][3]


def test_unity_mono_and_version(tmp_path):
    key, det = engine_of(tmp_path, {
        "UnityPlayer.dll": b"MZ", "Game_Data/Managed/Assembly-CSharp.dll": b"MZ",
        "Game_Data/globalgamemanagers": b"\0" * 20 + b"2022.3.21f1\0" + b"\0" * 100,
        "Game_Data/app.info": "Studio\nCoolGame",
    })
    assert key == "unity-mono"
    assert det["version"] == "2022.3.21f1" and det["product"] == "CoolGame"


def test_unity_il2cpp(tmp_path):
    key, _ = engine_of(tmp_path, {"UnityPlayer.dll": b"MZ", "GameAssembly.dll": b"MZ",
                                  "Game_Data/il2cpp_data/Metadata/global-metadata.dat": b"\xaf\x1b\xb1\xfa"})
    assert key == "unity-il2cpp"


def test_unreal_version_from_exe(tmp_path):
    exe = b"MZ" + b"\0" * 5000 + "++UE5+Release-5.3".encode("utf-16-le") + b"\0" * 100
    key, det = engine_of(tmp_path, {"Proj/Binaries/Win64/Proj-Win64-Shipping.exe": exe, "Proj/Content/Paks/Proj-Windows.pak": b"x",
                                    "Proj/Content/Paks/Proj-Windows.utoc": b"x"})
    assert key == "unreal" and det["engine_version"] == "UE5+Release-5.3" and det["iostore"]


def test_godot_pck(tmp_path):
    key, det = engine_of(tmp_path, {"game.exe": b"MZ", "game.pck": b"GDPC" + struct.pack("<4I", 2, 4, 2, 1)})
    assert key == "godot" and det["version"].startswith("4.2.1")


def test_gamemaker_and_rpgmaker(tmp_path):
    assert engine_of(tmp_path / "a", {"data.win": b"FORM\0\0\0\0GEN8\0\0\0\0\0\x11"})[0] == "gamemaker"
    assert engine_of(tmp_path / "b", {"www/js/rpg_core.js": "//", "Game.exe": b"MZ"})[0] == "rpgmaker-mvmz"


def test_managed_pe(tmp_path):
    # minimal PE32 with a CLR header directory entry
    pe = bytearray(1024)
    pe[0:2] = b"MZ"
    struct.pack_into("<I", pe, 0x3C, 0x80)
    pe[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", pe, 0x84, 0x14C)
    opt = 0x80 + 24
    struct.pack_into("<H", pe, opt, 0x10B)
    struct.pack_into("<I", pe, opt + 96 + 14 * 8, 0x2000)
    p = tmp_path / "Game.exe"
    p.write_bytes(bytes(pe))
    assert scan.pe_info(p) == {"arch": "x86", "managed": True}


def test_vdf():
    d = scan._vdf('"AppState" { "appid" "105600" "name" "Terraria" "installdir" "Terraria" }')
    assert d["AppState"]["installdir"] == "Terraria"


@pytest.mark.parametrize("library_name,install_name,game_name", [
    ("SteamLibrary", "ExampleGame", "Example Game"),
    ("SteamLibrary", "ExampleGame", "Example Game\u2122"),
    ("SteamLibrary", "Jeu\u00e9", "Example Game"),
    ("Biblioth\u00e8que", "ExampleGame", "Example Game"),
])
def test_steam_games_utf8(tmp_path, monkeypatch, library_name, install_name, game_name):
    root = tmp_path / "Steam"
    library = tmp_path / library_name
    apps = library / "steamapps"
    game_path = apps / "common" / install_name
    game_path.mkdir(parents=True)
    (root / "steamapps").mkdir(parents=True)
    (root / "steamapps/libraryfolders.vdf").write_text(
        '"libraryfolders" { "0" { "path" "' + library.as_posix() + '" } }', encoding="utf-8",
    )
    (apps / "appmanifest_123.acf").write_text(
        f'"AppState" {{ "appid" "123" "name" "{game_name}" "installdir" "{install_name}" }}',
        encoding="utf-8",
    )
    monkeypatch.setattr(scan, "steam_roots", lambda: [root])

    # Emulate a non-UTF-8 Windows default on every test platform, using real files.
    original_read_text = Path.read_text

    def read_text(path, encoding=None, errors=None, **kwargs):
        return original_read_text(path, encoding=encoding or "cp1252", errors=errors, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    assert scan.steam_games() == [{
        "store": "steam", "appid": "123", "name": game_name,
        "path": str(game_path), "workshop": None,
    }]


def test_steam_root_from_registry(tmp_path, monkeypatch):
    # Steam installed outside Program Files (e.g. C:\Steam): its libraryfolders.vdf, and every library in it, was never read
    root = tmp_path / "Steam"
    (root / "steamapps").mkdir(parents=True)
    monkeypatch.setattr(scan, "steam_registry_root", lambda: root)
    assert root.resolve() in scan.steam_roots()

def test_known_game_longest_key_wins(tmp_path):
    # "grand theft auto v" is a substring of "grand theft auto v enhanced";
    # the more specific entry must win, not whichever lands first in the dict
    d = tmp_path / "Grand Theft Auto V Enhanced"
    d.mkdir()
    for i in range(6):
        (d / f"f{i}.txt").write_text("x")
    r = scan.scan(str(d))
    assert r["routes"][0]["route"] == scan.KNOWN["grand theft auto v enhanced"][0]


def test_record_encodes_and_tags_bt709(tmp_path, monkeypatch):
    # RGB frames -> yuv420p used the BT.601 matrix untagged; browsers read HD video as BT.709 and shift colours
    from um import win
    seen = {}
    monkeypatch.setattr(win, "is_wsl", lambda: False)   # under WSL, Recorder calls wslpath through the patched Popen

    class FakePopen:
        def __init__(self, cmd, **kw):
            seen["cmd"] = cmd
    monkeypatch.setattr(win, "ffmpeg_win", lambda *a, **k: "ffmpeg")
    monkeypatch.setattr(win, "encoder", lambda: "libx264")
    monkeypatch.setattr(win.subprocess, "Popen", FakePopen)
    win.Recorder(exe="Game.exe", out=str(tmp_path / "take"), audio=False).start()
    cmd = seen["cmd"]
    assert "out_color_matrix=bt709" in cmd[cmd.index("-vf") + 1]
    assert cmd[cmd.index("-colorspace") + 1] == "bt709" and cmd[cmd.index("-color_range") + 1] == "tv"


def test_auto_hdr_detection(monkeypatch):
    # Auto HDR on an HDR display washes out captures of SDR games; um warns from the registry setting
    from um import win
    prefs = {"DirectXUserGlobalSettings": "AutoHDREnable=0;SwapEffectUpgradeEnable=1;",
             r"E:\Games\Foo\Foo.exe": "AppStatus=1;AutoHDREnable=2097;",
             r"E:\Games\Bar\Bar.exe": "AppStatus=1;AutoHDREnable=2096;"}
    monkeypatch.setattr(win, "_gpu_prefs", lambda: prefs)
    assert win.auto_hdr_on("Foo.exe") and win.auto_hdr_on("foo")
    assert not win.auto_hdr_on("Bar.exe") and not win.auto_hdr_on("Other.exe") and not win.auto_hdr_on()
    prefs["DirectXUserGlobalSettings"] = "AutoHDREnable=1;"
    assert win.auto_hdr_on("Other.exe") and not win.auto_hdr_on("Bar.exe")


def test_slay_the_spire_2_is_not_sts1(tmp_path):
    # StS2 is Godot + C#; the StS1 entry (ModTheSpire, Java) must not match it
    d = tmp_path / "Slay the Spire 2"
    d.mkdir()
    for i in range(6):
        (d / f"f{i}.txt").write_text("x")
    route = scan.scan(str(d))["routes"][0]["route"]
    assert route == scan.KNOWN["slay the spire 2"][0] and "ModTheSpire" not in route


def test_online_only_matches_whole_names():
    # a substring test told agents to stop on single-player games: Rusty Lake ("rust"), Battlefield 1942, MW2 (2009)
    for offline in ["Rusty Lake Paradise", "Rusted Warfare", "Battlefield 1942", "Call of Duty: Modern Warfare 2 (2009)",
                    "Deadlock: Planetary Conquest", "The Final Station", "Trusty Rusty"]:
        assert scan.online_only(offline) is None, offline
    for online in ["Rust", "Counter-Strike 2", "Call of Duty®", "Tom Clancy's Rainbow Six® Siege", "PUBG: BATTLEGROUNDS",
                   "Overwatch® 2", "Deadlock", "NARAKA: BLADEPOINT"]:
        assert scan.online_only(online), online


def test_scan_warns_only_for_online_games(tmp_path):
    for name, warned in [("Rust", True), ("Rusty Lake Paradise", False)]:
        d = tmp_path / name
        d.mkdir()
        for i in range(6):
            (d / f"f{i}.txt").write_text("x")
        assert any("online competitive" in w for w in scan.scan(str(d))["warnings"]) == warned, name


def test_ffmpeg_download_is_checksummed(tmp_path, monkeypatch):
    import hashlib, io
    from um import win
    payload = b"PK fake ffmpeg zip"
    good = hashlib.sha256(payload).hexdigest()
    sums = f"{'0' * 64}  ffmpeg-other.zip\n{good}  ffmpeg-master-latest-win64-gpl.zip\n".encode()
    monkeypatch.delenv("UM_FFMPEG_SHA256", raising=False)
    monkeypatch.setattr(win.urllib.request, "urlopen", lambda url, timeout=None: io.BytesIO(sums))
    assert win.ffmpeg_sha256() == good
    monkeypatch.setattr(win.urllib.request, "urlretrieve", lambda url, dst: Path(dst).write_bytes(payload))
    z = tmp_path / "ffmpeg.zip"
    win.download_ffmpeg(z)
    assert z.read_bytes() == payload
    monkeypatch.setenv("UM_FFMPEG_SHA256", "ab" * 32)        # a pin wins over the published sum
    with pytest.raises(SystemExit):
        win.download_ffmpeg(z)
    assert not z.exists()                                      # a bad download is deleted, never extracted


# --------------------------------------------------------------------------- sprite

def sprite_on_white(w=64, h=48):
    im = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    for x in range(20, 40):
        for y in range(10, 30):
            im.putpixel((x, y), (200, 30, 30, 255))
    im.putpixel((30, 20), (255, 255, 255, 255))   # an interior white "eye" must survive
    return im


def test_cutout_keeps_interior_white():
    out = sprite.cutout(sprite_on_white())
    assert out.size == (20, 20)
    assert out.getpixel((10, 10))[3] == 255          # the interior white pixel is still opaque
    assert out.getpixel((0, 0))[:3] == (200, 30, 30)


def test_fit_and_hard_alpha():
    f = sprite.fit(sprite.cutout(sprite_on_white()), 10, 10, anchor="bottom")
    assert f.size == (10, 10) and f.getbbox()[3] == 10
    assert set(sprite.hard_alpha(f).getchannel("A").getdata()) <= {0, 255}


def test_sheet_slice_roundtrip():
    frames = [Image.new("RGBA", (8, 8), (i * 40, 0, 0, 255)) for i in range(5)]
    sh = sprite.sheet(frames, cols=3)
    assert sh.size == (24, 16)
    assert len(sprite.slice_sheet(sh, 8, 8)) == 5


@pytest.mark.parametrize("alpha", [1, 64, 128, 192, 254, 255])
@pytest.mark.parametrize("operation", ["fit", "sheet", "squash"])
def test_sprite_placement_preserves_rgba(alpha, operation):
    # Placing a frame on a transparent canvas must not apply its alpha twice.
    im = Image.new("RGBA", (8, 8), (200, 100, 50, alpha))
    if operation == "fit":
        out = sprite.fit(im, 8, 8)
    elif operation == "sheet":
        out = sprite.slice_sheet(sprite.sheet([im]), 8, 8)[0]
    else:
        out = sprite.simple_frames(im, n=1, kind="squash")[0]
    assert out.tobytes() == im.tobytes()


def test_team_mask():
    im = Image.new("RGBA", (4, 1), (0, 0, 0, 255))
    im.putpixel((0, 0), (20, 60, 240, 255))            # saturated blue -> player colour
    im.putpixel((1, 0), (200, 200, 200, 255))          # grey stays
    rgb, mask = sprite.team_mask(im)
    assert mask.getpixel((0, 0)) > 200 and mask.getpixel((1, 0)) == 0


def test_seamless_edges_match():
    import numpy as np
    ramp = np.tile(np.linspace(0, 255, 64)[None, :, None], (64, 1, 4)).astype(np.uint8)   # huge seam at the wrap
    ramp[..., 3] = 255
    out = np.asarray(sprite.seamless(Image.fromarray(ramp))).astype(int)
    before = np.abs(ramp[:, 0, :3].astype(int) - ramp[:, -1, :3].astype(int)).mean()
    after = np.abs(out[:, 0, :3] - out[:, -1, :3]).mean()
    assert before > 200 and after < 12


# --------------------------------------------------------------------------- fal (offline parts)

def test_kv_and_urls(tmp_path):
    assert fal._kv(["prompt=a cat", "num_images:=2", "flag:=true"]) == {"prompt": "a cat", "num_images": 2, "flag": True}
    res = {"images": [{"url": "https://v3.fal.media/a.png", "content_type": "image/png"}, {"url": "https://v3.fal.media/b.png"}],
           "mask_image": {"url": "https://v3.fal.media/m.png"}}
    assert [u for _, u, _ in fal._urls_in(res)] == ["https://v3.fal.media/a.png", "https://v3.fal.media/b.png", "https://v3.fal.media/m.png"]


def test_upload_uses_cdn_token_and_explains_big_failures(tmp_path, monkeypatch, capsys):
    # storage/upload/initiate?storage_type=gcs now answers 400 "Invalid storage type"; files over 8 MiB then failed silently
    calls = []
    monkeypatch.setattr(fal, "_req", lambda method, url, body=None, **k: calls.append(url) or {"token": "t", "token_type": "Bearer"})

    class Resp:
        def __init__(self, req):
            self.req = req

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"access_url": "https://v3.fal.media/files/x/a.png"}).encode()
    sent = []
    monkeypatch.setattr(fal.urllib.request, "urlopen", lambda req, timeout=None: sent.append(req) or Resp(req))
    f = tmp_path / "a.png"
    f.write_bytes(b"\x89PNG")
    assert fal.upload(f) == "https://v3.fal.media/files/x/a.png"
    assert "storage_type=fal-cdn-v3" in calls[0] and sent[0].full_url == fal.CDN + "/files/upload"
    assert sent[0].get_header("Authorization") == "Bearer t" and sent[0].get_header("X-fal-file-name") == "a.png"

    def fail(req, timeout=None):
        raise fal.urllib.error.URLError("boom")
    monkeypatch.setattr(fal.urllib.request, "urlopen", fail)
    assert fal.upload(f).startswith("data:image/png;base64,")              # small: inline fallback
    big = tmp_path / "big.mp4"
    big.write_bytes(b"\0" * ((8 << 20) + 1))
    with pytest.raises(SystemExit):
        fal.upload(big)
    assert "only covers files under 8 MiB" in capsys.readouterr().err  # big: says why instead of a bare exit 1


def test_failed_download_keeps_the_request_id(tmp_path, monkeypatch, capsys):
    # a finished (paid) job whose output URL 404s must not vanish: say which request to fetch again
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"ok"

    def urlopen(req, timeout=None):
        if req.full_url.endswith("big.mov"):
            raise fal.urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        return Resp()
    monkeypatch.setattr(fal.urllib.request, "urlopen", urlopen)
    res = {"video": {"url": "https://v3b.fal.media/files/x/big.mov"}, "thumb": {"url": "https://v3b.fal.media/files/x/t.png"},
           "_request_id": "req-123", "_endpoint": "fal-ai/some-model"}
    with pytest.raises(SystemExit):
        fal.download_outputs(res, tmp_path, "clip")
    err = capsys.readouterr().err
    assert "big.mov" in err and "um fal result fal-ai/some-model req-123" in err
    assert (tmp_path / "clip_thumb.png").read_bytes() == b"ok"   # the other outputs still saved


# --------------------------------------------------------------------------- publish

def test_publish_check(tmp_path, capsys):
    # fixtures assembled at runtime so this file doesn't trip the toolkit's own publish check
    fake_key = "FAL" + "_KEY=" + "abcdefghijklmnopqrstuvwxyz0123"
    ghidra_name = "FUN" + "_00401000"
    make(tmp_path / "mod", {"src/Mod.cs": f"int {ghidra_name}();\n// " + "Decompiled with ILSpy", "README.md": "My mod, built with dnSpy notes",
                            "config.txt": fake_key})
    make(tmp_path / "game", {"data/big.bin": b"x" * 4096})
    (tmp_path / "mod" / "copied.bin").write_bytes(b"x" * 4096)
    assert publish.check(str(tmp_path / "mod"), str(tmp_path / "game")) == 1
    out = capsys.readouterr().out
    assert "game file copied verbatim" in out and "FAL_KEY assignment" in out and "Ghidra auto-name" in out
    normalized = out.replace("\\", "/")
    assert "decompiler header x1 in src/Mod.cs" in normalized and "README.md" not in normalized.split("decompiler header")[-1].split("\n")[0]


@pytest.mark.parametrize("label,key", [
    # assembled at runtime so this file doesn't trip the toolkit's own publish check
    ("OpenAI key", "sk-" + "proj-" + "Ab3_dE-f" + "Gh1jK2lM3nO4pQ5rS6tU7vW8xY9z0" * 4),
    ("OpenAI key", "sk-" + "svcacct-" + "Ab3_dE-f" + "Gh1jK2lM3nO4pQ5rS6tU7vW8xY9z0" * 4),
    ("OpenAI key", "sk-" + "Gh1jK2lM3nO4pQ5rS6tU7vW8xY9z0" * 2),
    ("GitHub token", "github" + "_pat_" + "11ABCDEFG0123456789abc" + "_" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5bC7dE9fG1hJ3kL5mN7pQ9rS1t"),
    ("GitHub token", "gh" + "p_" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3z"),
])
def test_secret_patterns_catch_current_key_formats(label, key):
    rx = dict(publish.SECRET_PATTERNS)[label]
    assert rx.search(f"key = {key}\n"), key


def test_secret_patterns_ignore_ordinary_text():
    text = "sk-learn-style-kebab-case-identifiers-are-not-keys and github_pat_ alone"
    assert not [label for label, rx in publish.SECRET_PATTERNS if rx.search(text)]


# --------------------------------------------------------------------------- video

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_compile_small_edl(tmp_path):
    for i, color in enumerate(["red", "blue"]):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s=640x360:d=3:r=30", "-f", "lavfi", "-i", "sine=f=440:d=3",
                        "-shortest", str(tmp_path / f"c{i}.mp4")], check=True)
    edl = {"size": [640, 360], "fps": 30, "bpm": 120, "beat_lock": True, "transition": {"type": "cut"},
           "segments": [{"clip": "c0.mp4", "in": 0, "beats": 4, "hook": "Hello"},
                        {"clip": "c1.mp4", "in": 0.5, "beats": 4, "title": "A title", "credit": "@someone", "transition": {"type": "fade", "duration": 0.3}},
                        {"card": {"title": "The end"}, "dur": 1.5}]}
    (tmp_path / "edl.json").write_text(json.dumps(edl))
    video.compile_edl(tmp_path / "edl.json", str(tmp_path / "out.mp4"))
    info = video.probe(tmp_path / "out.mp4")
    assert abs(info["duration"] - (2 + 2 + 1.5)) < 0.15 and info["audio"]


# --------------------------------------------------------------------------- knowledge base

from um import kb  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def test_repo_knowledge_is_valid():
    root = REPO / "knowledge"
    for p, _, _ in kb.notes(root):
        fails, _ = kb.check_note(p, root)
        assert not fails, (p, fails)
    idx, rows = kb.build_index(root)
    assert (root / "INDEX.md").read_text(encoding="utf-8") == idx, "run `um kb index`"
    assert len(rows) >= 7


def test_kb_new_check_search(tmp_path):
    import shutil
    root = tmp_path / "knowledge"
    root.mkdir()
    shutil.copy(REPO / "knowledge" / "TEMPLATE.md", root / "TEMPLATE.md")
    p = kb.new_note(root, "Hades II", "A new boon god", agent="Codex (gpt-6)", route="loader-api")
    fails, _ = kb.check_note(p, root)
    assert any("unfilled template text" in f for f in fails)          # a fresh scaffold must not pass
    good = p.read_text(encoding="utf-8")
    good = good.replace("FILL IN: exact build", "1.0.1 (Steam)").replace("anti_cheat: FILL IN", "anti_cheat: none")
    good = good.replace("> Two to four sentences: what you built", "> Added a boon god via a Lua mod loader")
    good = good.replace("The most valuable section. Numbered; each one symptom → cause → fix.", "")
    good = good.replace("1. **Symptom.** What you saw. **Cause:** what it really was. **Fix:** what worked.",
                        "1. **Boons never offered.** **Cause:** pool cached at load. **Fix:** register before the run starts.")
    p.write_text(good, encoding="utf-8")
    fails, _ = kb.check_note(p, root)
    assert not fails, fails
    res = kb.search(root, ["boon"])
    assert res and res[0]["path"].endswith("a-new-boon-god.md")
    assert kb.search(root, ["boon"], route="native-hook") == []


def test_kb_search_matches_word_starts(tmp_path):
    root = tmp_path / "knowledge" / "games" / "x"
    root.mkdir(parents=True)
    for name, title in [("a.md", "Trust and frustum culling"), ("b.md", "A Rust server plugin"), ("c.md", "Rusty Lake puzzles"),
                        ("d.md", "Patching plugin.esp")]:
        (root / name).write_text(f"---\nkind: game\ntitle: {title}\ngame: X\n---\n# {title}\n", encoding="utf-8")
    found = {r["title"] for r in kb.search(tmp_path / "knowledge", ["rust"])}
    assert found == {"A Rust server plugin", "Rusty Lake puzzles"}
    assert [r["title"] for r in kb.search(tmp_path / "knowledge", [".esp"])] == ["Patching plugin.esp"]   # punctuation-led terms match anywhere


def test_kb_check_rejects_secrets_and_dumps(tmp_path):
    note = tmp_path / "n.md"
    code = "\n".join(f"int x{i} = {i};" for i in range(160))
    note.write_text("---\nkind: technique\ntitle: t\ntags: [x]\ndate: 2026-09-30\nagents: [a]\n---\n# t\n"
                    f"```c\n{code}\n```\n" + "FAL" + "_KEY=abcdefghijklmnopqrstuvwxyz0123\n")
    fails, _ = kb.check_note(note)
    assert any("code block" in f for f in fails) and any("FAL_KEY" in f for f in fails)


def test_kb_impossible_date_is_reported_not_raised(tmp_path):
    # YAML turns an unquoted YYYY-MM-DD into a date; a day that doesn't exist raises ValueError, not YAMLError
    root = tmp_path / "knowledge"
    (root / "techniques").mkdir(parents=True)
    note = root / "techniques" / "t.md"
    note.write_text("---\nkind: technique\ntitle: t\ntags: [x]\ndate: 2026-09-31\nagents: [a]\n---\n# t\n", encoding="utf-8")
    fails, _ = kb.check_note(note, root)
    assert any("front matter is not valid YAML" in f for f in fails), fails   # the date error's wording varies by Python
    kb.search(root, ["t"])                                             # one bad note must not break search or index
    kb.build_index(root)


@pytest.mark.parametrize("url", ["https://github.com/alice/universal-modder.git", "https://github.com/alice/universal-modder",
                                 "git@github.com:alice/universal-modder.git", "ssh://git@github.com/alice/universal-modder.git"])
def test_pr_head_from_fork(url):
    # gh looks a bare --head branch up in the base repo; a PR from a fork needs "<owner>:<branch>"
    assert kb.pr_head("kb/a-b", url) == "alice:kb/a-b"


def test_pr_head_same_repo():
    assert kb.pr_head("kb/a-b", None) == "kb/a-b"


# --------------------------------------------------------------------------- powershell

def test_ps_exe_falls_back_to_full_path(tmp_path, monkeypatch):
    # an agent's PATH often lacks System32\WindowsPowerShell\v1.0; bare "powershell" then raises WinError 2
    from um import common
    exe = tmp_path / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(common, "is_wsl", lambda: False)
    monkeypatch.setattr(common.shutil, "which", lambda name: None)
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    assert common.ps_exe() == str(exe)
    monkeypatch.setattr(common.shutil, "which", lambda name: "/on/path/" + name)
    assert common.ps_exe() == "/on/path/powershell"


# --------------------------------------------------------------------------- backup

@pytest.fixture
def backup_same_second(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(backup.time, "strftime", lambda *args: "20261006-120000")


def test_backup_same_second_keeps_every_snapshot(tmp_path, backup_same_second):
    src = tmp_path / "src"
    paths = []
    for i in range(12):
        make(src, {"save.dat": f"version {i}"})
        paths.append(backup.create(str(src), name="t", note=f"take {i}"))

    assert len(set(paths)) == 12
    assert paths[0].name == "20261006-120000.zip"  # keep the existing filename format when available
    assert backup.snapshots("t") == paths         # latest selection still works after ten collisions
    for i, path in enumerate(paths):
        with backup.zipfile.ZipFile(path) as z:
            assert z.read("save.dat") == f"version {i}".encode()
        assert backup._manifest(path)["note"] == f"take {i}"
    assert backup.diff("t")["changed"] == []
    make(src, {"save.dat": "modified"})
    backup.restore("t", yes=True)
    assert (src / "save.dat").read_text() == "version 11"


@pytest.mark.parametrize("removed", [0, 1])
def test_backup_after_deleted_snapshot_is_still_latest(tmp_path, backup_same_second, removed):
    src = tmp_path / "src"
    make(src, {"save.dat": "old"})
    paths = [backup.create(str(src), name="t") for _ in range(3)]
    paths[removed].unlink()
    make(src, {"save.dat": "new"})
    newest = backup.create(str(src), name="t")

    assert newest > paths[-1]
    assert backup.snapshots("t")[-1] == newest
    assert backup.diff("t")["changed"] == []


def test_backup_concurrent_creates_keep_every_snapshot(tmp_path, backup_same_second):
    from concurrent.futures import ThreadPoolExecutor

    src = tmp_path / "src"
    make(src, {"save.dat": "world"})
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda i: backup.create(str(src), name="t", note=str(i)), range(8)))

    assert len(set(paths)) == 8
    assert backup.snapshots("t") == sorted(paths)
    for i, path in enumerate(paths):
        with backup.zipfile.ZipFile(path) as z:
            assert z.read("save.dat") == b"world"
        assert backup._manifest(path)["note"] == str(i)


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_backup_failed_create_keeps_previous_snapshot(tmp_path, backup_same_second, monkeypatch, error):
    src = tmp_path / "src"
    make(src, {"save.dat": "pristine"})
    first = backup.create(str(src), name="t")
    original = first.read_bytes()

    def fail_write(*args, **kwargs):
        raise error("interrupted backup")

    monkeypatch.setattr(backup.zipfile.ZipFile, "write", fail_write)
    with pytest.raises(error, match="interrupted backup"):
        backup.create(str(src), name="t")
    assert backup.snapshots("t") == [first]
    assert first.read_bytes() == original


def test_backup_repeated_restores_keep_undo_snapshots(tmp_path, backup_same_second):
    src = tmp_path / "src"
    make(src, {"save.dat": "pristine"})
    backup.create(str(src), name="t")
    for state in ("first take", "second take"):
        make(src, {"save.dat": state})
        backup.restore("t", yes=True)
        assert (src / "save.dat").read_text() == "pristine"

    undo = backup.snapshots("t-pre-restore")
    assert len(undo) == 2
    for path, state in zip(undo, ("first take", "second take")):
        with backup.zipfile.ZipFile(path) as z:
            assert z.read("save.dat") == state.encode()


def test_backup_handles_pre_1980_timestamps(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(backup, "_root", lambda name: (tmp_path / "snaps" / name).mkdir(parents=True, exist_ok=True)
                        or tmp_path / "snaps" / name)
    src = tmp_path / "src"
    make(src, {"old.txt": "from 1970", "new.txt": "fresh"})
    os.utime(src / "old.txt", (0, 0))
    zp = backup.create(str(src), name="t")
    assert set(backup._manifest(zp)["files"]) == {"old.txt", "new.txt"}


def test_backup_diff_and_restore_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "_root", lambda name: (tmp_path / "snaps" / name).mkdir(parents=True, exist_ok=True)
                        or tmp_path / "snaps" / name)
    src = tmp_path / "src"
    make(src, {"save.dat": "v1", "sub/cfg.ini": "a=1"})
    backup.create(str(src), name="t")
    (src / "save.dat").write_text("v2")
    (src / "sub" / "cfg.ini").unlink()
    make(src, {"extra.log": "new"})
    d = backup.diff("t", str(src))
    assert (d["changed"], d["removed"], d["added"]) == (["save.dat"], ["sub/cfg.ini"], ["extra.log"])
    with pytest.raises(SystemExit):  # no --yes: report only, touch nothing
        backup.restore("t", str(src))
    assert (src / "save.dat").read_text() == "v2"
    backup.restore("t", str(src), clean=True, yes=True)
    assert (src / "save.dat").read_text() == "v1"
    assert (src / "sub" / "cfg.ini").read_text() == "a=1"
    assert not (src / "extra.log").exists()
    assert backup.snapshots("t-pre-restore")  # the state before the restore was kept


# --------------------------------------------------------------------------- comfy

from um import comfy  # noqa: E402


@pytest.fixture
def fake_comfy():
    """A stand-in for ComfyUI's HTTP API: /system_stats, /models, /object_info, /prompt, /history, /view."""
    import io
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    state = {"prompts": [], "polls": 0, "models_route": True}
    png = io.BytesIO()
    im = Image.new("RGBA", (16, 16), (255, 255, 255, 255))      # a red square on a white background
    im.paste((200, 30, 30, 255), (4, 4, 12, 12))
    im.save(png, "PNG")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body: bytes, code=200, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/system_stats":
                self._send(json.dumps({"system": {"comfyui_version": "0.9.0", "pytorch_version": "2.9.0"},
                                       "devices": [{"name": "fake", "type": "cpu"}]}).encode())
            elif u.path == "/models/checkpoints" and state["models_route"]:
                self._send(json.dumps(["sd15.safetensors", "sdxl_base.safetensors"]).encode())
            elif u.path == "/object_info/CheckpointLoaderSimple":
                spec = ["COMBO", {"options": ["v3.safetensors"]}]
                self._send(json.dumps({"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": spec}}}}).encode())
            elif u.path == "/history/p1":
                state["polls"] += 1                                  # the first poll finds it still running
                done = {"p1": {"status": {"status_str": "success", "completed": True},
                               "outputs": {"9": {"images": [{"filename": "um_00001_.png", "subfolder": "", "type": "output"}]}}}}
                self._send(json.dumps(done if state["polls"] > 1 else {}).encode())
            elif u.path == "/view" and parse_qs(u.query).get("filename") == ["um_00001_.png"]:
                self._send(png.getvalue(), ctype="image/png")
            else:
                self._send(b"404: Not Found", 404, "text/plain")

        def do_POST(self):
            wf = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["prompt"]
            state["prompts"].append(wf)
            if wf.get("4", {}).get("inputs", {}).get("ckpt_name") == "missing.safetensors":
                err = {"error": {"message": "Prompt outputs failed validation", "details": ""},
                       "node_errors": {"4": {"class_type": "CheckpointLoaderSimple", "errors": [
                           {"message": "Value not in list", "details": "ckpt_name: 'missing.safetensors' not in [...]"}]}}}
                self._send(json.dumps(err).encode(), 400)
            else:
                self._send(json.dumps({"prompt_id": "p1", "number": 0, "node_errors": {}}).encode())

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{srv.server_address[1]}"
    yield state
    srv.shutdown()


def test_comfy_image_sprite(fake_comfy, tmp_path, monkeypatch):
    from um import cli
    monkeypatch.setattr(comfy.time, "sleep", lambda s: None)
    out = tmp_path / "gen"
    cli.main(["comfy", "image", "a red potion", "--url", fake_comfy["url"], "--out", str(out), "--seed", "7", "--sprite"])
    wf = fake_comfy["prompts"][-1]
    assert wf["4"]["inputs"]["ckpt_name"] == "sd15.safetensors"               # the first checkpoint listed
    assert (wf["5"]["inputs"]["width"], wf["3"]["inputs"]["seed"]) == (512, 7)  # SD 1.5 size, the given seed
    assert "plain flat white background" in wf["6"]["inputs"]["text"]
    assert Image.open(out / "a_red_potion.png").size == (16, 16)
    cut = Image.open(out / "a_red_potion_cut.png")                           # cut out and trimmed locally
    assert cut.size == (8, 8) and cut.getpixel((0, 0)) == (200, 30, 30, 255)
    rec = json.loads((out / "comfy_manifest.jsonl").read_text().splitlines()[-1])
    assert rec["prompt_id"] == "p1" and rec["seed"] == 7 and rec["workflow"]["9"]["class_type"] == "SaveImage"


def test_comfy_run_set_and_errors(fake_comfy, tmp_path, monkeypatch):
    monkeypatch.setattr(comfy.time, "sleep", lambda s: None)
    wf = comfy.txt2img("x", "sd15.safetensors")
    wf["6"]["_meta"] = {"title": "Positive"}
    (tmp_path / "wf.json").write_text(json.dumps(wf))
    wf = comfy.apply_set(comfy.load_workflow(tmp_path / "wf.json"), ["Positive.text=a v1.5 sword=sharp", "3.seed:=42"])
    assert (wf["6"]["inputs"]["text"], wf["3"]["inputs"]["seed"]) == ("a v1.5 sword=sharp", 42)
    assert [Path(f).name for f in comfy.generate(fake_comfy["url"], wf, tmp_path / "o", "sword")] == ["sword.png"]
    (tmp_path / "ui.json").write_text(json.dumps({"nodes": [], "links": []}))
    with pytest.raises(SystemExit):
        comfy.load_workflow(tmp_path / "ui.json")                            # UI format: needs Export (API)
    with pytest.raises(SystemExit):
        comfy.queue(fake_comfy["url"], comfy.txt2img("x", "missing.safetensors"))   # validation error reported
    fake_comfy["models_route"] = False
    assert comfy.checkpoints(fake_comfy["url"]) == ["v3.safetensors"]        # older servers: /object_info
    assert comfy.status(fake_comfy["url"])["version"] == "0.9.0"


def test_skill_copies_match():
    # .agents/skills and .claude/skills are real copies of skills/ (Windows clones turn symlinks into text files)
    root = Path(__file__).resolve().parents[1]
    def tree(d):
        return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}
    src = tree(root / "skills")
    for copy in (".agents/skills", ".claude/skills"):
        assert not (root / copy).is_symlink(), f"{copy} must be a folder, not a symlink"
        assert tree(root / copy) == src, (f"{copy} differs from skills/: rm -rf .agents/skills .claude/skills && "
                                          "cp -r skills .agents/skills && cp -r skills .claude/skills")
    assert not any((root / d).exists() for d in (".gemini/skills", ".github/skills")), "agents read .agents/skills"


# --------------------------------------------------------------------------- hooks

@pytest.mark.skipif(not shutil.which("cygpath"), reason="Git Bash / MSYS only")
def test_path_hook_writes_a_posix_root(tmp_path):
    # Claude Code passes ${CLAUDE_PLUGIN_ROOT} as C:/...; written as is, bash splits PATH at the drive colon
    import os
    root = tmp_path / "um root"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "um").write_text("#!/bin/sh\n")
    env_file = tmp_path / "env.sh"
    bash = str(Path(shutil.which("cygpath")).with_name("bash.exe"))
    hook = Path(__file__).resolve().parents[1] / "hooks" / "add-to-path.sh"
    subprocess.run([bash, str(hook), root.as_posix()], env={**os.environ, "CLAUDE_ENV_FILE": str(env_file)}, check=True)
    value = env_file.read_text().split('"')[1]
    prefix = value[:value.index("/bin:$PATH")]
    assert prefix.startswith("/") and ":" not in prefix, value


# --------------------------------------------------------------------------- injection and path containment

PAYLOAD = r"x'; Set-Content -Path C:\pwned.txt -Value hi; Get-Process -Name 'nope"


def ps_literal(script: str, after: str = "-Name ", until: str = " -ErrorAction") -> str:
    """The single-quoted literal a script passes to -Name, decoded by PowerShell's rules ('' is one quote).
    Raises if the value is not wholly inside one literal - which is exactly what injection looks like."""
    literal = script.split(after, 1)[1].split(until, 1)[0].strip()
    assert literal[0] == "'" and literal[-1] == "'", literal
    body = literal[1:-1]
    assert "'" not in body.replace("''", ""), f"an unescaped quote ends the literal early: {literal}"
    return body.replace("''", "'")


def test_process_name_cannot_escape_the_powershell_literal(monkeypatch):
    # `um win ps "x'; <command>; '"` closed the -Name literal and ran the rest as PowerShell
    from um import common, win
    assert common.ps_quote("a'b") == "'a''b'" and common.ps_quote("plain") == "'plain'"
    seen = {}
    monkeypatch.setattr(win, "powershell", lambda script, **k: seen.setdefault("script", script) and "")

    win.processes(PAYLOAD)
    assert ps_literal(seen["script"]) == PAYLOAD + "*"      # the whole payload is one value, not a statement

    seen.clear()
    win.pid_of(PAYLOAD + ".exe")
    assert ps_literal(seen["script"]) == PAYLOAD


@pytest.mark.skipif(not (sys.platform == "win32" or shutil.which("powershell.exe")), reason="needs Windows PowerShell")
def test_process_name_injection_does_not_run(tmp_path):
    # the same payload against the real shell: the marker file must not appear
    from um import win
    marker = tmp_path / "pwned.txt"
    win.processes(f"x'; Set-Content -Path '{marker}' -Value hi; Get-Process -Name 'nope")
    assert not marker.exists()


def test_launch_never_hands_metacharacters_to_cmd(monkeypatch, tmp_path):
    # cmd.exe re-parses its command line, so `--steam "480&calc"` through `cmd /c start` also started calc
    from um import win
    monkeypatch.setattr(win, "is_wsl", lambda: False)
    monkeypatch.setattr(win, "is_windows", lambda: True)
    started, spawned = [], []
    monkeypatch.setattr(win.os, "startfile", lambda url: started.append(url), raising=False)
    monkeypatch.setattr(win.subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd))
    monkeypatch.setattr(win.subprocess, "run", lambda *a, **k: pytest.fail(f"spawned a shell: {a}"))

    with pytest.raises(SystemExit):
        win.launch("480&calc", [], steam=True)
    assert not started

    win.launch("105600", ["-windowed", "a b&c"], steam=True)
    assert started == ["steam://run/105600//-windowed%20a%20b%26c/"]   # & percent-encoded, not a second command

    exe = tmp_path / "game.exe"
    exe.write_bytes(b"MZ")
    win.launch(str(exe), ["-x&whoami"])
    assert len(spawned) == 1 and spawned[0][1:] == ["-x&whoami"]       # argv, so cmd never sees the &
    assert Path(spawned[0][0]).name == "game.exe"


@pytest.mark.parametrize("member", ["../escaped.txt", "ffmpeg/../../escaped.txt", "/abs/escaped.txt", "C:/abs/escaped.txt"])
def test_ffmpeg_zip_members_must_stay_inside(tmp_path, member):
    from um import win
    z = tmp_path / "ffmpeg.zip"
    with backup.zipfile.ZipFile(z, "w") as zf:
        zf.writestr("ffmpeg-build/bin/ffmpeg.exe", b"x")
        zf.writestr(member, b"pwned")
    with pytest.raises(SystemExit):
        win._extract_zip(z, tmp_path / "dest")
    assert not list((tmp_path / "dest").rglob("escaped.txt")) if (tmp_path / "dest").exists() else True

    with backup.zipfile.ZipFile(z, "w") as zf:               # the real shape still extracts
        zf.writestr("ffmpeg-build/bin/ffmpeg.exe", b"x")
    assert win._extract_zip(z, tmp_path / "ok") == "ffmpeg-build"
    assert (tmp_path / "ok" / "ffmpeg-build" / "bin" / "ffmpeg.exe").exists()


@pytest.mark.parametrize("rel", ["../escaped.txt", "a/../../escaped.txt", "/etc/escaped.txt", "C:/Windows/escaped.txt",
                                 r"..\escaped.txt"])
def test_restore_refuses_a_snapshot_that_writes_outside_the_target(tmp_path, backup_same_second, rel):
    # a snapshot's file list comes out of the zip, and snapshots travel between machines
    root = tmp_path / "data" / "backups" / "evil"
    root.mkdir(parents=True)
    target = tmp_path / "target"
    manifest = {"source": str(target), "created": "20261006-120000", "note": "", "files": {rel: {"size": 5, "sha1": "x"}}}
    with backup.zipfile.ZipFile(root / "20261006-120000.zip", "w") as z:
        z.writestr(rel, b"pwned")
        z.writestr("_um_manifest.json", json.dumps(manifest))

    with pytest.raises(SystemExit):
        backup.restore("evil", to=str(target), yes=True)
    assert not list(tmp_path.rglob("escaped.txt"))


def test_restore_still_puts_ordinary_paths_back(tmp_path, backup_same_second):
    src = tmp_path / "src"
    make(src, {"save.dat": "keep", "deep/nested/world.wld": "also keep"})
    backup.create(str(src), name="ok")
    shutil.rmtree(src)
    backup.restore("ok", yes=True)
    assert (src / "save.dat").read_text() == "keep"
    assert (src / "deep" / "nested" / "world.wld").read_text() == "also keep"


def test_edl_numbers_cannot_add_filters():
    # crop/zoom come from JSON; as strings they used to land in -filter_complex verbatim
    assert video._fill_filter("crop", 1920, 1080, crop=[1, 2, 3, 4]).startswith("crop=3:4:1:2,")
    with pytest.raises(ValueError):
        video._fill_filter("crop", 1920, 1080, crop=[0, 0, "1920,movie=/etc/passwd", 1080])
    with pytest.raises(ValueError):
        video._fill_filter("crop", 1920, 1080, zoom="1.5,movie=/etc/passwd")
    assert "movie=" not in video._fill_filter("crop", 1920, 1080, zoom=1.5)


def test_fal_key_is_not_replayed_on_a_redirect(monkeypatch):
    # urllib puts a request's headers back on a 3xx; the key must only ever go to the host it belongs to
    monkeypatch.setenv("FAL_KEY", "id:secret")
    captured = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return Resp()

    monkeypatch.setattr(fal.urllib.request, "urlopen", fake_urlopen)
    fal._req("GET", f"{fal.QUEUE}/fal-ai/x/requests/1/status")
    req = captured["req"]
    assert req.get_header("Authorization") == "Key id:secret"          # still sent to fal
    assert "authorization" in {k.lower() for k in req.unredirected_hdrs}
    assert "authorization" not in {k.lower() for k in req.headers}     # so a redirect cannot carry it


def test_fal_polls_only_fal_urls(capsys):
    assert fal._same_host(f"{fal.QUEUE}/fal-ai/x/requests/1/status", fal.QUEUE) is not None
    for bad in ("https://evil.example/steal", "http://queue.fal.run/x", "queue.fal.run/x", None, 42):
        assert fal._same_host(bad, fal.QUEUE) is None
    assert "ignoring" in capsys.readouterr().err


# --------------------------------------------------------------------------- nexus

def test_version_compare_ignores_mo2_padding():
    # MO2 stores every version padded to x.y.z.w; without stripping it, a whole modlist read "ahead of Nexus"
    from um import nexus
    for local, remote, want in [("1.1.0.0", "1.1", "same"), ("1.0.0.0", "1.0", "same"), ("1.0.0.0", "1", "same"),
                                ("4.2.6a", "4.2.6a", "same"), ("v6.9", "6.9", "same"),
                                ("1.0.0.0", "1.1", "newer"), ("5.9.2.0", "6.0.4", "newer"),
                                ("1.2.0.0", "1.1", "older"),
                                ("5.8.0.0a", "5.8", "differs"), ("1.0.0.0", "1.0b", "differs"),
                                ("", "1.0", "differs"), ("1.0", "", "differs")]:
        assert nexus.compare_versions(local, remote) == want, (local, remote)


def test_mod_uid_packs_game_and_mod():
    # the batch query takes uids, not (game, mod) pairs
    from um import nexus
    assert nexus.uid(1704, 272) == str((1704 << 32) | 272) == "7318624272656"
    assert nexus.file_bytes({"sizeInBytes": "2693003"}) == 2693003    # BigInt arrives as a string
    assert nexus.file_bytes({"size": 2630}) == 2630 * 1024            # size is in KB
    assert nexus.file_bytes({}) == 0


def mo2_instance(root, mods, profile="Default", enabled=None):
    """A miniature Mod Organizer 2 instance: ModOrganizer.ini, mods/<name>/meta.ini, a profile modlist."""
    (root / "profiles" / profile).mkdir(parents=True, exist_ok=True)
    root.joinpath("ModOrganizer.ini").write_text(
        "[General]\ngameName=Skyrim Special Edition\n"
        "selected_profile=@ByteArray(" + profile + ")\n", encoding="utf-8")
    for name, fields in mods.items():
        d = root / "mods" / name
        d.mkdir(parents=True, exist_ok=True)
        body = "[General]\n" + "".join(f"{k}={v}\n" for k, v in fields.items())
        # MO2 pastes the whole Nexus description in here; real files run to tens of KB
        body += 'nexusDescription="' + ("padding " * 4000) + '"\n'
        d.joinpath("meta.ini").write_text(body, encoding="utf-8")
    lines = [("+" if enabled is None or n in enabled else "-") + n for n in mods]
    (root / "profiles" / profile / "modlist.txt").write_text(
        "# generated by Mod Organizer\n" + "\n".join(lines) + "\n-Some Separator_separator\n", encoding="utf-8")


def test_installed_reads_mo2_metadata(tmp_path):
    from um import mo2, nexus
    inst = tmp_path / "LoreRim"
    mo2_instance(inst, {
        "Alternate Start": dict(gameName="SkyrimSE", modid="272", version="4.2.0.0",
                                installationFile="Alternate Start-272-4-2-123456789.7z", repository="Nexus"),
        "Switched Off": dict(gameName="SkyrimSE", modid="12604", version="6.0.0.0", repository="Nexus"),
        "Hand Built": dict(gameName="SkyrimSE", modid="0", version="1.0"),          # no Nexus id: skipped
    }, enabled={"Alternate Start"})

    everything = nexus.installed(str(inst))
    assert [m["folder"] for m in everything] == ["Alternate Start", "Switched Off"]
    assert everything[0]["mod_id"] == 272 and everything[0]["version"] == "4.2.0.0"
    assert everything[0]["enabled"] is True and everything[1]["enabled"] is False
    assert everything[0]["file"].endswith(".7z")                 # read past the padding, not truncated

    on = nexus.installed(str(inst), enabled_only=True)
    assert [m["folder"] for m in on] == ["Alternate Start"]
    assert mo2.resolve(str(inst))["selected_profile"] == "Default"      # @ByteArray unwrapped


def test_updates_reports_rather_than_skips(tmp_path, monkeypatch):
    from um import nexus
    inst = tmp_path / "inst"
    mo2_instance(inst, {
        "Needs Update": dict(gameName="SkyrimSE", modid="272", version="4.1.0.0", repository="Nexus"),
        "Up To Date": dict(gameName="SkyrimSE", modid="12604", version="6.11.0.0", repository="Nexus"),
        "Skipped In MO2": dict(gameName="SkyrimSE", modid="999", version="1.0", ignoredVersion="2.0",
                               repository="Nexus"),
        "Gone From Nexus": dict(gameName="SkyrimSE", modid="4242", version="1.0", repository="Nexus"),
        "Mystery Game": dict(gameName="NoSuchGame", modid="7", version="1.0", repository="Nexus"),
    })
    sse = {"id": 1704, "name": "Skyrim SE", "domainName": "skyrimspecialedition"}
    monkeypatch.setattr(nexus, "resolve_game",
                        lambda t: sse if str(t).lower() in ("skyrimse", "skyrimspecialedition")
                        else nexus.die("no game " + str(t)))
    remote = {272: dict(modId=272, name="Alternate Start", version="4.2.6a", category="Gameplay",
                        status="published", updatedAt="2026-03-08T00:00:00Z"),
              12604: dict(modId=12604, name="SkyUI", version="6.11", category="User Interface",
                          status="published", updatedAt="2026-05-01T00:00:00Z"),
              999: dict(modId=999, name="Skipped", version="2.0", category="Gameplay",
                        status="published", updatedAt="2026-01-01T00:00:00Z")}
    monkeypatch.setattr(nexus, "fetch_mods",
                        lambda game_id, ids, progress=False: {i: remote[i] for i in ids if i in remote})

    res = nexus.updates(str(inst), progress=False)
    state = {r["folder"]: r["state"] for r in res["mods"]}
    assert state == {"Needs Update": "newer", "Up To Date": "same", "Skipped In MO2": "ignored"}
    unresolved = {u["folder"]: u["why"] for u in res["unresolved"]}
    assert set(unresolved) == {"Gone From Nexus", "Mystery Game"}       # reported, never silently dropped
    assert "removed, hidden or wrong" in unresolved["Gone From Nexus"]
    assert "NoSuchGame" in unresolved["Mystery Game"]
    assert res["mods"][0]["folder"] == "Needs Update"                   # updates sort to the top


@pytest.mark.parametrize("url,want", [
    ("nxm://skyrimspecialedition/mods/272/files/749043?key=abc&expires=123",
     dict(domain="skyrimspecialedition", mod_id=272, file_id=749043, key="abc", expires="123")),
    ("nxm://fallout4/mods/1/files/2", dict(domain="fallout4", mod_id=1, file_id=2, key=None, expires=None)),
])
def test_nxm_url_parsing(url, want):
    from um import nexus
    assert nexus.nxm_parse(url) == want


@pytest.mark.parametrize("bad", ["https://nexusmods.com/skyrimspecialedition/mods/272",
                                 "nxm://skyrimspecialedition/collections/abc",
                                 "nxm://skyrimspecialedition/mods/272"])
def test_nxm_url_rejects_other_shapes(bad):
    from um import nexus
    with pytest.raises(SystemExit):
        nexus.nxm_parse(bad)


def test_nexus_filename_carries_the_mod_id():
    from um import nexus
    got = nexus.from_filename(Path("(2)Barbarian Bodypaints - CBBE-31826-1-0-1579138592.7z"))
    assert got["mod_id"] == 31826 and got["version"] == "1.0"
    assert got["name"] == "(2)Barbarian Bodypaints - CBBE"
    assert nexus.from_filename(Path("hand-made-thing.zip")) == {}


def test_api_key_comes_only_from_the_environment(tmp_path, monkeypatch):
    from um import nexus
    monkeypatch.delenv("NEXUS_API_KEY", raising=False)
    monkeypatch.delenv("NEXUS_API_KEY_FILE", raising=False)
    (tmp_path / ".env").write_text("NEXUS_API_KEY=should-not-be-read\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert nexus.api_key(required=False) is None          # a .env in the cwd is not a key you chose to use
    with pytest.raises(SystemExit):
        nexus.api_key()
    keyfile = tmp_path / "key.txt"
    keyfile.write_text("  from-a-file  \n", encoding="utf-8")
    monkeypatch.setenv("NEXUS_API_KEY_FILE", str(keyfile))
    assert nexus.api_key() == "from-a-file"
    monkeypatch.setenv("NEXUS_API_KEY", "from-the-env")
    assert nexus.api_key() == "from-the-env"              # the variable wins over the file


@pytest.mark.parametrize("member", ["../escaped.txt", "a/../../escaped.txt", "/abs/escaped.txt",
                                    "C:/abs/escaped.txt", "..\\escaped.txt"])
def test_install_refuses_an_archive_that_writes_outside(tmp_path, member):
    # a Nexus download is a stranger's archive, and MO2's own mods folder is the target
    from um import nexus
    archive = tmp_path / "Evil Mod-123-1-0-123456789.zip"
    with backup.zipfile.ZipFile(archive, "w") as z:
        z.writestr("meshes/fine.nif", b"ok")
        z.writestr(member, b"pwned")
    mods = tmp_path / "mods"
    with pytest.raises(SystemExit):
        nexus.install(str(archive), str(mods), yes=True)
    assert not list(tmp_path.rglob("escaped.txt"))


def test_install_unpacks_and_leaves_meta_for_update_checking(tmp_path, monkeypatch):
    from um import mo2, nexus
    archive = tmp_path / "Alternate Start-272-4-2-6-1579138592.zip"
    with backup.zipfile.ZipFile(archive, "w") as z:
        z.writestr("Alternate Start.esp", b"TES4")
        z.writestr("scripts/thing.pex", b"pex")
    mods = tmp_path / "mods"
    monkeypatch.setattr(nexus, "resolve_game", lambda t: {"id": 1704, "name": "Skyrim Special Edition",
                                                          "domainName": "skyrimspecialedition"})

    with pytest.raises(SystemExit):                         # writing into a loaded mods folder is confirmed
        nexus.install(str(archive), str(mods))

    target = nexus.install(str(archive), str(mods), game="skyrimspecialedition", yes=True)
    assert (target / "Alternate Start.esp").read_bytes() == b"TES4"
    assert (target / "scripts" / "thing.pex").exists()
    meta = mo2.read_meta(target / "meta.ini")
    assert meta["modid"] == "272" and meta["version"] == "4.2.6" and meta["repository"] == "Nexus"
    assert nexus.installed(str(mods))[0]["mod_id"] == 272    # the loop closes: updates now tracks it


# --------------------------------------------------------------------------- mo2

def test_mo2_ini_values_survive_qt_and_odd_keys(tmp_path):
    # ModOrganizer.ini holds `1\title=` keys and bare % signs, which configparser refuses outright
    from um import mo2
    ini = tmp_path / "ModOrganizer.ini"
    ini.write_text("[General]\ngameName=Skyrim Special Edition\n"
                   "gamePath=@ByteArray(D:\\\\LoreRim\\\\Stock Game)\n"
                   "selected_profile=@ByteArray(Community Shaders + Ciri Player)\n"
                   "[customExecutables]\n1\\title=xEdit64\n1\\binary=D:/x/xEdit.exe\n"
                   "2\\title=Synthesis\n%00orkingDirectory%00%00size=3\n", encoding="utf-8")
    v = mo2.ini_values(ini)
    assert mo2._unqt(v["gamePath"]) == r"D:\LoreRim\Stock Game"          # @ByteArray + doubled backslashes
    assert mo2._unqt(v["selected_profile"]) == "Community Shaders + Ciri Player"
    assert mo2._unqt(v["gameName"]) == "Skyrim Special Edition"
    inst = dict(ini=str(ini))
    assert mo2.tools(inst) == ["xEdit64", "Synthesis"]


def mo2_full(root, mods, profile="Default", base=None, order=None):
    """A miniature instance. `order` is written to modlist.txt as given - i.e. highest priority first."""
    data = Path(base) if base else root
    (data / "profiles" / profile).mkdir(parents=True, exist_ok=True)
    (data / "overwrite").mkdir(parents=True, exist_ok=True)
    (data / "downloads").mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    ini = ["[General]", "gameName=Skyrim Special Edition", f"selected_profile=@ByteArray({profile})"]
    if base:
        ini.append(f"base_directory=@ByteArray({str(data).replace(chr(92), chr(92) * 2)})")
    (root / "ModOrganizer.ini").write_text("\n".join(ini) + "\n", encoding="utf-8")
    for name, files in mods.items():
        d = data / "mods" / name
        d.mkdir(parents=True, exist_ok=True)
        for rel, body in (files or {}).items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
    lines = order if order is not None else ["+" + n for n in mods]
    (data / "profiles" / profile / "modlist.txt").write_text(
        "# This file was automatically generated by Mod Organizer.\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return root


def test_mo2_modlist_priority_is_reverse_of_file_order(tmp_path):
    # modlist.txt is written winners-first; reading it top-down inverts every conflict answer
    from um import mo2
    root = mo2_full(tmp_path / "List", {"Wins": None, "Middle": None, "Base": None},
                    order=["+Wins", "-Off", "+Middle", "*DLC: Dawnguard", "+Base", "-Some Group_separator"])
    inst = mo2.resolve(str(root))
    rows = mo2.mod_order(inst)
    assert [r["name"] for r in rows] == ["Wins", "Off", "Middle", "DLC: Dawnguard", "Base", "Some Group_separator"]
    by = {r["name"]: r for r in rows}
    assert by["Wins"]["priority"] > by["Middle"]["priority"] > by["Base"]["priority"]
    assert by["Off"]["enabled"] is False and by["Wins"]["enabled"] is True
    assert by["DLC: Dawnguard"]["unmanaged"] is True            # * here means base-game, not enabled
    assert by["Some Group_separator"]["separator"] is True
    assert mo2.enabled_mods(inst) == {"Wins", "Middle", "Base"}  # separators and * entries are not mods


def test_mo2_resolves_a_managed_instance_base_directory(tmp_path):
    # a managed instance keeps only the ini under %LOCALAPPDATA%; the mods are at base_directory
    from um import mo2
    data = tmp_path / "Elsewhere"
    root = mo2_full(tmp_path / "IniOnly", {"A Mod": None}, base=data)
    inst = mo2.resolve(str(root))
    assert Path(inst["mods_dir"]) == data / "mods"
    assert Path(inst["profiles_dir"]) == data / "profiles"
    assert inst["portable"] is False                            # no ModOrganizer.exe beside the ini
    assert [m["folder"] for m in mo2.installed_mods(inst)] == []  # no meta.ini, so nothing to report


def test_mo2_conflict_winner_is_the_highest_priority_provider(tmp_path):
    from um import mo2
    rel = "meshes/actors/character/skeleton.nif"
    root = mo2_full(tmp_path / "List", {
        "Skeleton Fix": {rel: "fix"},
        "XP32": {rel: "xp32"},
        "Unrelated": {"textures/x.dds": "tex"},
        "Disabled Fix": {rel: "never"},
    }, order=["+Skeleton Fix", "+XP32", "-Disabled Fix", "+Unrelated"])
    inst = mo2.resolve(str(root))

    res = mo2.winner(inst, rel)
    assert res["winner"] == "Skeleton Fix"                      # nearer the top of modlist.txt = wins
    assert [p["mod"] for p in res["providers"]] == ["Skeleton Fix", "XP32"]   # the disabled one is absent

    # overwrite/ sits above every mod, which is why leftovers there silently win
    over = Path(inst["overwrite_dir"]) / rel
    over.parent.mkdir(parents=True, exist_ok=True)
    over.write_text("stale", encoding="utf-8")
    assert mo2.winner(inst, rel)["winner"] == "<overwrite>"

    assert mo2.winner(inst, "nothing/here.nif")["providers"] == []
    with pytest.raises(SystemExit):
        mo2.winner(inst, "../escape.nif")


def test_mo2_mod_conflicts_reports_both_directions(tmp_path):
    from um import mo2
    root = mo2_full(tmp_path / "List", {
        "Top": {"a.nif": "top"},
        "Middle": {"a.nif": "mid", "b.nif": "mid"},
        "Bottom": {"b.nif": "bottom"},
    }, order=["+Top", "+Middle", "+Bottom"])
    inst = mo2.resolve(str(root))
    res = mo2.mod_conflicts(inst, "Middle")
    assert [r["file"] for r in res["loses"]] == ["a.nif"] and res["loses"][0]["against"] == "Top"
    assert [r["file"] for r in res["wins"]] == ["b.nif"]
    assert mo2.mod_conflicts(inst, "Middle", limit=1)["truncated"] is True


def test_mo2_check_counts_separators_as_present(tmp_path):
    # MO2 creates mods/<name>_separator/, so excluding separators made every one look like an orphan
    from um import mo2
    root = mo2_full(tmp_path / "List", {"Real Mod": None, "A Group_separator": None, "Left Over": None},
                    order=["+Real Mod", "-A Group_separator", "+Gone From Disk", "*DLC: Dawnguard"])
    inst = mo2.resolve(str(root))
    res = mo2.check(inst)
    assert "A Group_separator" not in " ".join(res["notes"])     # the separator is not an orphan
    assert "DLC: Dawnguard" not in " ".join(res["problems"])     # nor is an unmanaged entry missing
    assert any("Gone From Disk" in p for p in res["problems"])   # listed with no folder: a real problem
    assert any("Left Over" in n for n in res["notes"])           # on disk, never listed: a note


def test_mo2_plugins_reads_order_and_enabled_separately(tmp_path):
    # plugins.txt's `*` means enabled - the opposite kind of marker from modlist.txt's `*`
    from um import mo2
    root = mo2_full(tmp_path / "List", {"A Mod": None})
    prof = Path(mo2.resolve(str(root))["profiles_dir"]) / "Default"
    (prof / "plugins.txt").write_text("# generated\n*Skyrim.esm\nDisabled.esp\n*Mine.esp\n", encoding="utf-8")
    (prof / "loadorder.txt").write_text("Skyrim.esm\nDisabled.esp\nMine.esp\n", encoding="utf-8")
    rows = mo2.plugins(mo2.resolve(str(root)))
    assert [r["name"] for r in rows] == ["Skyrim.esm", "Disabled.esp", "Mine.esp"]
    assert [r["enabled"] for r in rows] == [True, False, True]


def test_mo2_run_refuses_a_tool_the_instance_does_not_have(tmp_path):
    from um import mo2
    root = mo2_full(tmp_path / "List", {"A Mod": None})
    (root / "ModOrganizer.exe").write_bytes(b"MZ")
    inst = mo2.resolve(str(root))
    with pytest.raises(SystemExit):
        mo2.run(inst, "NotConfigured")


def test_vortex_staging_folder_names_carry_the_mod_id(tmp_path):
    # Vortex keeps no meta.ini, but names each staging folder after the Nexus archive
    from um import nexus
    staging = tmp_path / "Vortex Mods" / "skyrimse"
    staging.mkdir(parents=True)
    (staging / "__vortex_staging_folder").write_text('{"instance":"x","game":"skyrimse"}', encoding="utf-8")
    for name in ["'Pumping Iron' and 'Hand to Hand (Adamant)' Patch-97490-1-0-1724779805",
                 "(beta) Wade In Water Animations-34006-1-42-1649385478",
                 "Something Renamed By Hand"]:
        (staging / name).mkdir()
    rows = nexus.installed(str(staging))
    assert {r["mod_id"] for r in rows} == {97490, 34006}       # the renamed folder drops out, not guessed at
    assert {r["version"] for r in rows} == {"1.0", "1.42"}
    assert nexus.from_name("Thing-272-4-2-6-1579138592")["mod_id"] == 272
