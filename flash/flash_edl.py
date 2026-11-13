#!/usr/bin/env python3
"""EDL-only installer for the built image (no fastboot step).

Writes the image produced by this builder to an MSM8916 dongle through
Qualcomm EDL (Firehose), using bkerler's `edl` tool. Useful on boards where
lk2nd fastboot cannot be reached, and it is the path that was verified on the
JZ01-45-V33 board.

Three subcommands:

  prepare   build the EDL files from files/ (full-disk GPT, raw boot/rootfs)
  plan      print every edl command that `flash` would run, in order
  flash     run them: save the device-unique partitions, write, read back,
            compare, reset

Nothing is written to the device by `prepare` or `plan`.

The device-unique partitions (fsc fsg modem modemst1 modemst2 persist sec) are
read from the device right before flashing and written back at their new
offsets, because modemst1/modemst2/persist change while the stock firmware
runs.

Example (Windows, PowerShell):

  python flash/flash_edl.py prepare --sectors 7733248
  python flash/flash_edl.py plan
  python flash/flash_edl.py flash --edl "C:/edl/.venv/Scripts/python.exe C:/edl/edl.py" ^
      --loader C:/edl/Loaders/custom/prog_emmc_firehose_8916.mbn --yes-erase-device
"""
import argparse
import hashlib
import json
import shlex
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

SECTOR = 512
GPT_ENTRY_SECTORS = 32
ROOT = Path(__file__).resolve().parent.parent
FILES = ROOT / "files"
OUT = FILES / "edl"

# Partitions that only exist on this one device. Read first, written back last.
DEVICE_UNIQUE = ["fsc", "fsg", "modem", "modemst1", "modemst2", "persist", "sec"]
# name -> file in files/ (bootloader chain)
FIRMWARE = {"hyp": "hyp.mbn", "rpm": "rpm.mbn", "sbl1": "sbl1.mbn", "tz": "tz.mbn", "aboot": "aboot.mbn"}
# name -> sparse image in files/, written un-sparsed
IMAGES = {"boot": "boot.bin", "rootfs": "rootfs.bin"}


# ---------------------------------------------------------------- GPT

def parse_gpt(blob):
    """Return (header bytes, entries bytes, [(name, first, last)])."""
    hdr = bytearray(blob[SECTOR:SECTOR + 92])
    if hdr[:8] != b"EFI PART":
        sys.exit("gpt_both0.bin: no GPT header")
    ent_lba, n, esz = struct.unpack_from("<QII", hdr, 72)
    entries = bytearray(blob[ent_lba * SECTOR: ent_lba * SECTOR + n * esz])
    parts = []
    for i in range(n):
        e = entries[i * esz:(i + 1) * esz]
        first, last = struct.unpack_from("<QQ", e, 32)
        name = e[56:].decode("utf-16-le").rstrip("\0")
        if name:
            parts.append((name, first, last))
    return hdr, entries, parts, n, esz


def header_with_crc(hdr):
    hdr = bytearray(hdr)
    struct.pack_into("<I", hdr, 16, 0)
    struct.pack_into("<I", hdr, 16, zlib.crc32(bytes(hdr[:92])) & 0xFFFFFFFF)
    return hdr


def build_full_disk_gpt(src, total_sectors):
    """Stretch the 67-sector fastboot GPT to a real disk of total_sectors.

    lk2nd's fastboot grows the last partition and relocates the backup table
    when it flashes gpt_both0.bin. EDL does neither, so do it here.
    """
    blob = src.read_bytes()
    hdr, entries, parts, n, esz = parse_gpt(blob)
    last_usable = total_sectors - GPT_ENTRY_SECTORS - 2
    backup_entries_lba = total_sectors - GPT_ENTRY_SECTORS - 1
    backup_header_lba = total_sectors - 1

    # last partition (rootfs) runs to the end of the usable area
    idx = max(i for i in range(n) if entries[i * esz + 56: i * esz + 58] != b"\0\0")
    struct.pack_into("<Q", entries, idx * esz + 40, last_usable)
    entries_crc = zlib.crc32(bytes(entries)) & 0xFFFFFFFF

    primary = bytearray(hdr)
    struct.pack_into("<Q", primary, 24, 1)                    # my LBA
    struct.pack_into("<Q", primary, 32, backup_header_lba)    # alternate LBA
    struct.pack_into("<Q", primary, 48, last_usable)
    struct.pack_into("<Q", primary, 72, 2)                    # entries LBA
    struct.pack_into("<I", primary, 88, entries_crc)
    primary = header_with_crc(primary)

    backup = bytearray(primary)
    struct.pack_into("<Q", backup, 24, backup_header_lba)
    struct.pack_into("<Q", backup, 32, 1)
    struct.pack_into("<Q", backup, 72, backup_entries_lba)
    backup = header_with_crc(backup)

    pmbr = bytearray(blob[:SECTOR])
    struct.pack_into("<I", pmbr, 446 + 12, min(total_sectors - 1, 0xFFFFFFFF))

    pad = lambda b: bytes(b) + bytes(SECTOR - len(b))
    ent = bytes(entries) + bytes(GPT_ENTRY_SECTORS * SECTOR - len(entries))
    gpt_main = bytes(pmbr) + pad(primary) + ent           # sectors 0..33
    gpt_backup = ent + pad(backup)                        # last 33 sectors

    _, _, new_parts, _, _ = parse_gpt(gpt_main)
    return gpt_main, gpt_backup, new_parts, backup_entries_lba


# ---------------------------------------------------------------- sparse

def unsparse(src, dst):
    """Android sparse image -> raw image. Returns raw size."""
    with open(src, "rb") as f, open(dst, "wb") as o:
        magic, major, _minor, fhs, chs, blk, total_blks, chunks, _ = struct.unpack("<IHHHHIIII", f.read(28))
        if magic != 0xED26FF3A or major != 1:
            sys.exit(f"{src.name}: not an Android sparse image")
        f.seek(fhs)
        written = 0
        for _ in range(chunks):
            ctype, _, csz, total = struct.unpack("<HHII", f.read(12))
            f.seek(chs - 12, 1)
            size = csz * blk
            if ctype == 0xCAC1:      # raw
                left = size
                while left:
                    buf = f.read(min(left, 4 << 20))
                    o.write(buf)
                    left -= len(buf)
            elif ctype == 0xCAC2:    # fill
                fill = f.read(4)
                o.write(fill * (size // 4))
            elif ctype == 0xCAC3:    # don't care
                o.seek(size, 1)
            elif ctype == 0xCAC4:    # crc32
                f.read(4)
                size = 0
            else:
                sys.exit(f"{src.name}: unknown chunk type {ctype:#x}")
            written += size
        o.truncate(written)
    if written != blk * total_blks:
        sys.exit(f"{src.name}: size mismatch after un-sparsing")
    return written


def sha256(path, limit=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        left = limit
        while True:
            n = 4 << 20 if left is None else min(4 << 20, left)
            if n == 0:
                break
            buf = f.read(n)
            if not buf:
                break
            h.update(buf)
            if left is not None:
                left -= len(buf)
    return h.hexdigest()


# ---------------------------------------------------------------- steps

def cmd_prepare(args):
    OUT.mkdir(parents=True, exist_ok=True)
    gpt_main, gpt_backup, parts, backup_lba = build_full_disk_gpt(FILES / "gpt_both0.bin", args.sectors)
    (OUT / "gpt_main.bin").write_bytes(gpt_main)
    (OUT / "gpt_backup.bin").write_bytes(gpt_backup)
    table = {name: {"start": first, "sectors": last - first + 1} for name, first, last in parts}

    for name, src in IMAGES.items():
        raw = OUT / f"{name}.raw"
        size = unsparse(FILES / src, raw)
        if size > table[name]["sectors"] * SECTOR:
            sys.exit(f"{name}: image ({size}) larger than partition")
        print(f"{name}.raw  {size:,} bytes")

    for name, src in FIRMWARE.items():
        size = (FILES / src).stat().st_size
        if size > table[name]["sectors"] * SECTOR:
            sys.exit(f"{name}: {src} ({size}) larger than partition")

    layout = {"total_sectors": args.sectors, "backup_gpt_lba": backup_lba, "partitions": table}
    (OUT / "layout.json").write_text(json.dumps(layout, indent=2))
    print(f"\nFull-disk GPT for {args.sectors:,} sectors:")
    for name, first, last in parts:
        print(f"  {name:<9} start {first:>8}  sectors {last - first + 1:>8}")
    print(f"  backup GPT at sector {backup_lba}")
    print(f"\nFiles are in {OUT}")


def load_layout():
    p = OUT / "layout.json"
    if not p.exists():
        sys.exit("Run `prepare` first.")
    return json.loads(p.read_text())


def build_steps(layout, save_dir):
    """Ordered list of (kind, description, edl args, check)."""
    parts = layout["partitions"]
    steps = []
    for name in DEVICE_UNIQUE:
        steps.append(("save", f"save {name} from the stock layout", ["r", name, str(save_dir / f"{name}.bin")], None))
    steps.append(("write", "primary GPT", ["ws", "0", str(OUT / "gpt_main.bin")], (0, OUT / "gpt_main.bin")))
    steps.append(("write", "backup GPT", ["ws", str(layout["backup_gpt_lba"]), str(OUT / "gpt_backup.bin")],
                  (layout["backup_gpt_lba"], OUT / "gpt_backup.bin")))
    for name, src in FIRMWARE.items():
        f = FILES / src
        steps.append(("write", name, ["ws", str(parts[name]["start"]), str(f)], (parts[name]["start"], f)))
    for name in IMAGES:
        f = OUT / f"{name}.raw"
        steps.append(("write", name, ["ws", str(parts[name]["start"]), str(f)], (parts[name]["start"], f)))
    for name in DEVICE_UNIQUE:
        f = save_dir / f"{name}.bin"
        steps.append(("write", f"restore {name}", ["ws", str(parts[name]["start"]), str(f)], (parts[name]["start"], f)))
    return steps


def cmd_plan(args):
    layout = load_layout()
    save_dir = Path(args.save_dir) if args.save_dir else OUT / "device-unique"
    print("# every command below is `edl --loader <loader> <args> --memory=eMMC`\n")
    for kind, desc, a, check in build_steps(layout, save_dir):
        line = "edl " + " ".join(a)
        print(f"{kind:<5} {desc:<34} {line}")
        if check:
            start, f = check
            n = -(-Path(f).stat().st_size // SECTOR) if Path(f).exists() else "?"
            print(f"      {'read back and compare':<34} edl rs {start} {n} <tmp>")
    print(f"{'reset':<5} {'reboot into the new system':<34} edl reset")


def cmd_flash(args):
    if not args.yes_erase_device:
        sys.exit("This erases the stock firmware. Re-run with --yes-erase-device.")
    layout = load_layout()
    save_dir = Path(args.save_dir) if args.save_dir else OUT / "device-unique"
    save_dir.mkdir(parents=True, exist_ok=True)
    tmp = OUT / "readback"
    tmp.mkdir(exist_ok=True)
    # posix=False keeps Windows backslashes but also keeps the quotes: strip them
    base = [t.strip('"') for t in shlex.split(args.edl, posix=False)] + ["--loader", args.loader]
    for exe in base[:2]:
        if not Path(exe).exists():
            sys.exit(f"--edl: {exe} does not exist")

    def edl(a, timeout):
        t = time.time()
        r = subprocess.run(base + a + ["--memory=eMMC"], capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return r.returncode, r.stdout[-600:], time.time() - t

    for kind, desc, a, check in build_steps(layout, save_dir):
        if kind == "write" and not Path(a[2]).exists():
            sys.exit(f"missing file for '{desc}': {a[2]}")
        rc, tail, dt = edl(a, args.timeout)
        if rc != 0:
            sys.exit(f"FAILED at '{desc}' (exit {rc}). Nothing after this step was written.\n{tail}")
        if kind == "save":
            f = Path(a[2])
            if not f.exists() or f.stat().st_size == 0:
                sys.exit(f"FAILED: {f} is empty after reading")
            print(f"saved   {desc:<30} {f.stat().st_size:>12,} bytes  {dt:5.1f}s")
            continue
        start, f = check
        size = Path(f).stat().st_size
        sectors = -(-size // SECTOR)
        back = tmp / "readback.bin"
        rc, tail, dt2 = edl(["rs", str(start), str(sectors), str(back)], args.timeout)
        if rc != 0 or sha256(back, size) != sha256(f):
            sys.exit(f"VERIFY FAILED at '{desc}'. Do not unplug; re-run this step.\n{tail}")
        print(f"written {desc:<30} {size:>12,} bytes  {dt:5.1f}s  verified")
    rc, tail, _ = edl(["reset"], 60)
    print("reset sent" if rc == 0 else f"reset failed (exit {rc}); replug the dongle")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("prepare")
    a.add_argument("--sectors", type=int, required=True, help="eMMC size in 512-byte sectors (edl printgpt shows it)")
    a.set_defaults(func=cmd_prepare)
    for name, func in (("plan", cmd_plan), ("flash", cmd_flash)):
        a = sub.add_parser(name)
        a.add_argument("--save-dir", help="where the device-unique partitions are saved")
        if name == "flash":
            a.add_argument("--edl", required=True, help='how to start edl, e.g. "python C:/edl/edl.py"')
            a.add_argument("--loader", required=True, help="Firehose loader (.mbn)")
            a.add_argument("--timeout", type=int, default=900)
            a.add_argument("--yes-erase-device", action="store_true")
        a.set_defaults(func=func)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
