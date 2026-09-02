#!/usr/bin/env python3
"""
flac_downconvert.py - Down-convert FLAC files to 16 bit with minimal loss.

Target format:
  * 16 bit, always
  * Sample rate: the rate family is preserved
        44100  -> 44100      88200  -> 44100      176400 -> 44100
        48000  -> 48000      96000  -> 48000      192000 -> 48000
    Anything at or below 48 kHz is left alone. Never upsamples.

Per file:
  1. Read STREAMINFO straight from the file (no ffprobe needed)
  2. If resampling: measure the peak of the resampled signal and, if it
     exceeds 0 dBFS, attenuate by exactly enough to avoid clipping
  3. ffmpeg (soxr, else swr) -> 16-bit WAV, RF64 beyond 4 GB, with dither
  4. Local flac binary, highest compression (-8 -e -p), with --verify
  5. Carry over VORBIS_COMMENT, PICTURE, APPLICATION and (only if the
     sample rate is unchanged) CUESHEET byte for byte from the original
  6. flac -t as an integrity check
  7. Move the original to *.flac.bak, put the new file in its place,
     restore mtime and permissions

Requirements: ffmpeg and flac on PATH. Otherwise just Python >= 3.8.
Runs on Linux, macOS and Windows.

License: MIT
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

__version__ = "1.1.0"

# --------------------------------------------------------------------------
# FLAC metadata blocks
# --------------------------------------------------------------------------

BLOCK_STREAMINFO = 0
BLOCK_PADDING = 1
BLOCK_APPLICATION = 2
BLOCK_SEEKTABLE = 3
BLOCK_VORBIS_COMMENT = 4
BLOCK_CUESHEET = 5
BLOCK_PICTURE = 6

MAX_BLOCK_LEN = 0xFFFFFF
PADDING_BYTES = 8192

TARGET_BITS = 16


class FlacError(Exception):
    pass


@dataclass
class MetaBlock:
    btype: int
    data: bytes


@dataclass
class StreamInfo:
    sample_rate: int
    channels: int
    bits: int
    total_samples: int


@dataclass
class FlacFile:
    blocks: list
    audio_offset: int
    streaminfo: StreamInfo

    def first(self, btype):
        for b in self.blocks:
            if b.btype == btype:
                return b
        return None

    def all_of(self, btype):
        return [b for b in self.blocks if b.btype == btype]


def parse_streaminfo(data: bytes) -> StreamInfo:
    """STREAMINFO is a packed bit field; see the FLAC specification."""
    if len(data) < 34:
        raise FlacError("STREAMINFO too short")
    sample_rate = (data[10] << 12) | (data[11] << 4) | (data[12] >> 4)
    channels = ((data[12] >> 1) & 0x07) + 1
    bits = (((data[12] & 0x01) << 4) | (data[13] >> 4)) + 1
    total = ((data[13] & 0x0F) << 32) | int.from_bytes(data[14:18], "big")
    if sample_rate == 0:
        raise FlacError("sample rate is 0 in STREAMINFO")
    return StreamInfo(sample_rate, channels, bits, total)


def read_flac(path: Path) -> FlacFile:
    """Read the header and metadata blocks without touching the audio."""
    with open(path, "rb") as f:
        if f.read(4) != b"fLaC":
            raise FlacError("no fLaC marker - not a FLAC file")
        blocks = []
        while True:
            hdr = f.read(4)
            if len(hdr) < 4:
                raise FlacError("metadata section truncated")
            last = bool(hdr[0] & 0x80)
            btype = hdr[0] & 0x7F
            length = int.from_bytes(hdr[1:4], "big")
            data = f.read(length)
            if len(data) != length:
                raise FlacError("incomplete metadata block")
            blocks.append(MetaBlock(btype, data))
            if last:
                audio_offset = f.tell()
                break
    if not blocks or blocks[0].btype != BLOCK_STREAMINFO:
        raise FlacError("first block is not STREAMINFO")
    return FlacFile(blocks, audio_offset, parse_streaminfo(blocks[0].data))


def parse_vorbis_comment(data: bytes):
    """-> (vendor: bytes, items: list[bytes]). Items stay raw (UTF-8) so
    multi-line values and special characters pass through untouched."""
    try:
        off = 0
        (vlen,) = struct.unpack_from("<I", data, off)
        off += 4
        vendor = data[off : off + vlen]
        off += vlen
        (count,) = struct.unpack_from("<I", data, off)
        off += 4
        items = []
        for _ in range(count):
            (ln,) = struct.unpack_from("<I", data, off)
            off += 4
            items.append(data[off : off + ln])
            off += ln
    except struct.error as e:
        raise FlacError(f"malformed VORBIS_COMMENT: {e}")
    return vendor, items


def build_vorbis_comment(vendor: bytes, items) -> bytes:
    out = bytearray()
    out += struct.pack("<I", len(vendor)) + vendor
    out += struct.pack("<I", len(items))
    for it in items:
        out += struct.pack("<I", len(it)) + it
    return bytes(out)


def write_flac_with_blocks(encoded: Path, dest: Path, blocks, audio_offset: int):
    """Write dest = fLaC + blocks + audio frames taken from encoded."""
    with open(encoded, "rb") as fin, open(dest, "wb") as fout:
        fout.write(b"fLaC")
        for i, b in enumerate(blocks):
            if len(b.data) > MAX_BLOCK_LEN:
                raise FlacError(f"metadata block type {b.btype} too large")
            last = 0x80 if i == len(blocks) - 1 else 0x00
            fout.write(bytes([last | (b.btype & 0x7F)]))
            fout.write(len(b.data).to_bytes(3, "big"))
            fout.write(b.data)
        fin.seek(audio_offset)
        shutil.copyfileobj(fin, fout, 1024 * 1024)
        fout.flush()
        os.fsync(fout.fileno())


def is_replaygain(item: bytes) -> bool:
    return item.upper().startswith(b"REPLAYGAIN_")


def assemble_blocks(enc: FlacFile, src: FlacFile, keep_cuesheet: bool,
                    drop_rg: bool):
    """Blocks for the new file: structure from the encoder, content from
    the original.

    STREAMINFO and SEEKTABLE have to come from the encoder because they
    describe the new audio data. Everything else is copied byte for byte
    so picture types, descriptions and multi-line tags survive exactly.
    """
    out = [enc.blocks[0]]

    seek = enc.first(BLOCK_SEEKTABLE)
    if seek is not None:
        out.append(seek)

    enc_vc = enc.first(BLOCK_VORBIS_COMMENT)
    vendor = parse_vorbis_comment(enc_vc.data)[0] if enc_vc else b"reference libFLAC"

    src_vc = src.first(BLOCK_VORBIS_COMMENT)
    items = list(parse_vorbis_comment(src_vc.data)[1]) if src_vc else []
    dropped_rg = 0
    if drop_rg:
        before = len(items)
        items = [i for i in items if not is_replaygain(i)]
        dropped_rg = before - len(items)
    out.append(MetaBlock(BLOCK_VORBIS_COMMENT, build_vorbis_comment(vendor, items)))

    out.extend(src.all_of(BLOCK_APPLICATION))
    if keep_cuesheet:
        # CUESHEET offsets are sample positions and are only valid while
        # the sample rate stays the same.
        out.extend(src.all_of(BLOCK_CUESHEET))
    out.extend(src.all_of(BLOCK_PICTURE))
    out.append(MetaBlock(BLOCK_PADDING, b"\x00" * PADDING_BYTES))
    return out, dropped_rg


# --------------------------------------------------------------------------
# Sample rate policy
# --------------------------------------------------------------------------

def target_rate(sr: int) -> int:
    if sr <= 48000:
        return sr
    if sr % 44100 == 0:
        return 44100
    if sr % 48000 == 0:
        return 48000
    if sr % 11025 == 0:
        return 44100
    return 48000


# --------------------------------------------------------------------------
# External tools
# --------------------------------------------------------------------------

@dataclass
class Tools:
    ffmpeg: str
    flac: str
    resampler: str = "soxr"    # soxr | swr


def run(cmd, **kw):
    return subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kw,
    )


def locate_tools(args) -> Tools:
    ffmpeg = args.ffmpeg or os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    flac = args.flac or os.environ.get("FLAC") or shutil.which("flac")
    missing = []
    if not ffmpeg:
        missing.append("ffmpeg")
    if not flac:
        missing.append("flac")
    if missing:
        sys.exit(
            "ERROR: not found: " + ", ".join(missing) + "\n"
            "  Debian/Ubuntu : sudo apt install ffmpeg flac\n"
            "  Fedora        : sudo dnf install ffmpeg flac\n"
            "  Arch          : sudo pacman -S ffmpeg flac\n"
            "  macOS         : brew install ffmpeg flac\n"
            "  Windows       : winget install Gyan.FFmpeg  /  choco install flac\n"
            "  or pass explicit paths via --ffmpeg / --flac."
        )
    r = run([flac, "--version"])
    ver = (r.stdout or r.stderr).decode("utf-8", "replace").strip()
    m = re.search(r"(\d+)\.(\d+)", ver)
    if m and (int(m.group(1)), int(m.group(2))) < (1, 3):
        print(f"WARNING: {ver} is old; it may not read RF64 WAV.",
              file=sys.stderr)
    tools = Tools(ffmpeg, flac)
    tools.resampler = pick_resampler(tools, args.resampler)
    return tools


def has_soxr(tools: Tools) -> bool:
    """Empirically test whether this ffmpeg build includes libsoxr.
    Parsing the version string would be unreliable, so push 0.05 s of
    silence through the filter instead."""
    r = run([
        tools.ffmpeg, "-v", "error", "-hide_banner", "-nostdin",
        "-f", "lavfi", "-i", "anullsrc=r=96000:cl=stereo", "-t", "0.05",
        "-af", "aresample=resampler=soxr:precision=28:out_sample_rate=48000",
        "-f", "null", "-",
    ])
    return r.returncode == 0


def pick_resampler(tools: Tools, preference: str) -> str:
    if preference == "swr":
        return "swr"
    available = has_soxr(tools)
    if preference == "soxr":
        if not available:
            sys.exit(
                "ERROR: --resampler soxr requested, but this ffmpeg build "
                "has no libsoxr.\n"
                "  Check with: ffmpeg -buildconf | grep soxr\n"
                "  Or use --resampler swr instead."
            )
        return "soxr"
    return "soxr" if available else "swr"


def aresample(engine: str, rate: int, osf: str, dither: str = None) -> str:
    """Build the aresample filter chain for the selected engine."""
    if engine == "soxr":
        opts = ["resampler=soxr", "precision=28"]
    else:
        # swr tuned for quality: long Kaiser filter, high stopband
        # attenuation. The default would be filter_size=32.
        opts = ["resampler=swr", "filter_size=256", "phase_shift=10",
                "filter_type=kaiser", "kaiser_beta=12", "cutoff=0.95"]
    opts.append(f"osf={osf}")
    if dither:
        opts.append(f"dither_method={dither}")
    opts.append(f"out_sample_rate={rate}")
    return "aresample=" + ":".join(opts)


PEAK_RE = re.compile(r"Peak level dB:\s*(-?inf|-?\d+(?:\.\d+)?)", re.IGNORECASE)


def measure_peak_db(tools: Tools, src: Path, rate: int):
    """Peak of the resampled float signal in dBFS, or None on failure.

    Resampling produces intersample overshoot; a signal peaking at
    0 dBFS can land well above that afterwards and clip on quantisation.
    """
    chain = (
        aresample(tools.resampler, rate, "fltp")
        + ",astats=measure_perchannel=none:measure_overall=Peak_level"
    )
    r = run([
        tools.ffmpeg, "-v", "info", "-hide_banner", "-nostdin",
        "-i", str(src), "-map", "0:a:0", "-vn",
        "-af", chain, "-f", "null", "-",
    ])
    if r.returncode != 0:
        return None
    peaks = []
    for m in PEAK_RE.finditer(r.stderr.decode("utf-8", "replace")):
        v = m.group(1).lower()
        if "inf" in v:
            continue
        peaks.append(float(v))
    return max(peaks) if peaks else None


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

PRINT_LOCK = threading.Lock()
LOG_LOCK = threading.Lock()
LOG_HANDLE = None
STOP = threading.Event()


def emit(msg: str):
    with PRINT_LOCK:
        print(msg, flush=True)
    if LOG_HANDLE is not None:
        with LOG_LOCK:
            LOG_HANDLE.write(msg + "\n")
            LOG_HANDLE.flush()


# --------------------------------------------------------------------------
# Converting a single file
# --------------------------------------------------------------------------

@dataclass
class Result:
    path: Path
    status: str          # converted | skipped | dryrun | error | aborted
    detail: str = ""


def convert_one(src: Path, tools: Tools, args) -> Result:
    if STOP.is_set():
        return Result(src, "aborted", "not started due to interrupt")

    try:
        sf = read_flac(src)
        st = src.stat()
    except Exception as e:
        return Result(src, "error", f"unreadable: {e}")

    si = sf.streaminfo
    tr = target_rate(si.sample_rate) if args.rate == "auto" else int(args.rate)

    # Never upsample: it invents data and only inflates the file.
    if tr > si.sample_rate:
        tr = si.sample_rate

    need_bits = si.bits > TARGET_BITS
    need_rate = tr != si.sample_rate

    if not need_bits and not need_rate:
        return Result(src, "skipped",
                      f"already {si.bits} bit / {si.sample_rate} Hz")

    plan = f"{si.bits}bit/{si.sample_rate}Hz -> 16bit/{tr}Hz"

    # Report collisions early rather than after minutes of encoding.
    bak = src.with_name(src.name + args.backup_suffix)
    if bak.exists() and not args.force:
        return Result(src, "error",
                      f"backup already exists: {bak.name} "
                      f"(use --force to overwrite)")

    if args.dry_run:
        return Result(src, "dryrun", plan)

    tmp_dir = Path(args.temp_dir) if args.temp_dir else None
    wav_fd, wav_name = tempfile.mkstemp(suffix=".wav", prefix="flacdc_",
                                        dir=str(tmp_dir) if tmp_dir else None)
    os.close(wav_fd)
    wav = Path(wav_name)
    enc_tmp = src.with_name(src.name + ".enc.tmp")
    fin_tmp = src.with_name(src.name + ".new.tmp")

    gain_db = 0.0
    try:
        # --- 1. Peak measurement, only when resampling -------------------
        if need_rate:
            peak = measure_peak_db(tools, src, tr)
            if peak is None:
                emit(f"  ! {src.name}: peak measurement failed, no guard")
            elif peak > 0.0:
                gain_db = -(peak + 0.05)
                emit(f"  ~ {src.name}: peak {peak:+.2f} dBFS after resampling, "
                     f"attenuating by {gain_db:.2f} dB")

        # --- 2. ffmpeg -> 16-bit WAV -------------------------------------
        chain = []
        if gain_db != 0.0:
            chain.append(f"volume={gain_db:.4f}dB")
        chain.append(aresample(tools.resampler, tr, "s16", args.dither))
        r = run([
            tools.ffmpeg, "-v", "error", "-hide_banner", "-nostdin", "-y",
            "-i", str(src), "-map", "0:a:0", "-vn", "-map_metadata", "-1",
            "-af", ",".join(chain),
            "-c:a", "pcm_s16le", "-rf64", "auto", "-f", "wav", str(wav),
        ])
        if r.returncode != 0:
            return Result(src, "error", "ffmpeg: " + short_err(r.stderr))

        # --- 3. flac encoder ---------------------------------------------
        cmd = [tools.flac, "-s", "-f", "--verify", "--no-padding", "-8"]
        if not args.fast:
            cmd += ["--exhaustive-model-search", "--qlp-coeff-precision-search"]
        cmd += ["-o", str(enc_tmp), str(wav)]
        r = run(cmd)
        if r.returncode != 0:
            return Result(src, "error", "flac: " + short_err(r.stderr))

        # --- 4. Carry over metadata --------------------------------------
        ef = read_flac(enc_tmp)
        drop_rg = (gain_db != 0.0) and not args.keep_replaygain
        blocks, dropped = assemble_blocks(
            ef, sf, keep_cuesheet=not need_rate, drop_rg=drop_rg
        )
        write_flac_with_blocks(enc_tmp, fin_tmp, blocks, ef.audio_offset)

        # --- 5. Verification ---------------------------------------------
        ns = read_flac(fin_tmp).streaminfo
        expected = round(si.total_samples * tr / si.sample_rate) if si.total_samples else 0
        problems = []
        if ns.bits != TARGET_BITS:
            problems.append(f"bit depth {ns.bits}")
        if ns.sample_rate != tr:
            problems.append(f"sample rate {ns.sample_rate}")
        if ns.channels != si.channels:
            problems.append(f"{ns.channels} channels instead of {si.channels}")
        if expected and abs(ns.total_samples - expected) > max(64, expected // 100000):
            problems.append(f"length {ns.total_samples} instead of ~{expected}")
        if problems:
            return Result(src, "error", "implausible result: " + ", ".join(problems))

        if run([tools.flac, "-t", "-s", str(fin_tmp)]).returncode != 0:
            return Result(src, "error", "flac -t failed, original untouched")

        # --- 6. Swap in, keeping a backup --------------------------------
        if bak.exists():
            bak.unlink()            # only reachable with --force
        os.replace(str(src), str(bak))
        try:
            os.replace(str(fin_tmp), str(src))
        except OSError:
            os.replace(str(bak), str(src))       # roll back
            raise

        try:
            os.utime(src, (st.st_atime, st.st_mtime))
            if os.name != "nt":
                os.chmod(src, st.st_mode & 0o7777)
        except OSError:
            pass

        if args.no_backup:
            try:
                bak.unlink()
            except OSError:
                pass

        extra = []
        pics = len(sf.all_of(BLOCK_PICTURE))
        if pics:
            extra.append(f"{pics} picture(s)")
        if dropped:
            extra.append(f"{dropped} ReplayGain tag(s) dropped")
        if not args.no_backup:
            extra.append(f"backup {bak.name}")
        return Result(src, "converted", plan + (" | " + ", ".join(extra) if extra else ""))

    except Exception as e:
        return Result(src, "error", f"{type(e).__name__}: {e}")
    finally:
        for p in (wav, enc_tmp, fin_tmp):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass


def short_err(raw: bytes, limit: int = 300) -> str:
    txt = raw.decode("utf-8", "replace").strip()
    txt = " / ".join(line.strip() for line in txt.splitlines() if line.strip())
    return txt[:limit]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def collect_files(paths, backup_suffix: str):
    files = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            files.extend(sorted(
                q for q in p.rglob("*")
                if q.is_file()
                and q.suffix.lower() == ".flac"
                and not q.name.endswith(backup_suffix)
            ))
        elif p.is_file():
            files.append(p)
        else:
            print(f"WARNING: {p} does not exist - skipped", file=sys.stderr)
    seen, uniq = set(), []
    for f in files:
        try:
            k = f.resolve()
        except OSError:
            k = f
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


def build_parser():
    ap = argparse.ArgumentParser(
        prog="flac_downconvert.py",
        description="Down-convert FLAC to 16 bit / 44.1 or 48 kHz, "
                    "in place with a backup.",
    )
    ap.add_argument("paths", nargs="*", default=["."],
                    help="files or directories (recursive). Default: current directory")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="only show what would happen")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="start without asking for confirmation")
    ap.add_argument("-j", "--jobs", type=int, default=0,
                    help="files in parallel (default: CPU cores, capped at 4)")
    ap.add_argument("--rate", default="auto", choices=["auto", "44100", "48000"],
                    help="target sample rate. auto = keep the rate family (default)")
    ap.add_argument("--dither", default="triangular_hp",
                    choices=["rectangular", "triangular", "triangular_hp",
                             "lipshitz", "shibata", "low_shibata", "high_shibata"],
                    help="ffmpeg dither method (default: triangular_hp)")
    ap.add_argument("--resampler", default="auto", choices=["auto", "soxr", "swr"],
                    help="resampling engine. auto = soxr if the ffmpeg build "
                         "has it, otherwise swr (default)")
    ap.add_argument("--fast", action="store_true",
                    help="use -8 instead of -8 -e -p (much faster, ~0.5%% larger)")
    ap.add_argument("--no-backup", action="store_true",
                    help="delete the backup again after a successful swap")
    ap.add_argument("--backup-suffix", default=".bak",
                    help="suffix for the backup (default: .bak)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing backups")
    ap.add_argument("--keep-replaygain", action="store_true",
                    help="keep ReplayGain tags even when attenuation was applied")
    ap.add_argument("--temp-dir", default=None,
                    help="where to put the intermediate WAV (default: system temp)")
    ap.add_argument("--log", default=None, help="also write output to a log file")
    ap.add_argument("--ffmpeg", default=None, help="path to the ffmpeg binary")
    ap.add_argument("--flac", default=None, help="path to the flac binary")
    ap.add_argument("-V", "--version", action="version",
                    version=f"flac_downconvert {__version__}")
    return ap


def main():
    # On Windows the console code page may not cover every character;
    # replacing beats crashing with UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args()
    tools = locate_tools(args)

    files = collect_files(args.paths, args.backup_suffix)
    if not files:
        print("No FLAC files found.")
        return 0

    global LOG_HANDLE
    if args.log:
        LOG_HANDLE = open(args.log, "a", encoding="utf-8")

    jobs = args.jobs if args.jobs > 0 else min(4, os.cpu_count() or 1)
    mode = "-8" if args.fast else "-8 -e -p"
    print(f"{len(files)} FLAC files | compression {mode} | "
          f"resampler {tools.resampler} | {jobs} in parallel")
    if tools.resampler == "swr" and args.resampler == "auto":
        print("Note: this ffmpeg build has no libsoxr, "
              "falling back to swr with high filter quality.")
    if args.dry_run:
        print("DRY RUN - nothing will be written.")
    elif not args.yes:
        print(f"Originals will be replaced, backed up as *{args.backup_suffix}.")
        try:
            if input("Continue? [y/N] ").strip().lower() != "y":
                print("Aborted.")
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1
    print()

    counts = {"converted": 0, "skipped": 0, "dryrun": 0, "error": 0, "aborted": 0}
    tags = {"converted": "OK  ", "skipped": "--  ", "dryrun": "DRY ",
            "error": "FAIL", "aborted": "ABRT"}
    total = len(files)
    width = len(str(total))
    done = 0
    interrupted = False

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=jobs)
    try:
        futures = [ex.submit(convert_one, f, tools, args) for f in files]
        for fut in concurrent.futures.as_completed(futures):
            try:
                res = fut.result()
            except Exception as e:                       # should never happen
                res = Result(Path("?"), "error", f"unexpected: {e}")
            done += 1
            counts[res.status] += 1
            emit(f"[{done:>{width}}/{total}] {tags[res.status]} "
                 f"{res.path} - {res.detail}")
    except KeyboardInterrupt:
        interrupted = True
        STOP.set()
        print("\nInterrupt requested - files already in flight will be "
              "finished, then the run stops.", file=sys.stderr)
    finally:
        ex.shutdown(wait=True)
        if LOG_HANDLE:
            LOG_HANDLE.close()

    print()
    print(f"Converted: {counts['converted']}   "
          f"Skipped: {counts['skipped']}   "
          f"Dry run: {counts['dryrun']}   "
          f"Aborted: {counts['aborted']}   "
          f"Errors: {counts['error']}")
    if interrupted:
        return 130
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # e.g. when piped into "head": silence stdout and exit quietly
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        os._exit(0)
