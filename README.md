# flac-downconvert

Konvertiert 24-Bit-FLAC-Dateien nach 16 Bit — mit korrektem Dither, Clipping-Schutz und **byte-identischer** Übernahme aller Metadaten. Läuft auf Linux, macOS und Windows.

Converts 24-bit FLAC files to 16 bit — with proper dithering, clipping protection and **byte-identical** preservation of all metadata. Runs on Linux, macOS and Windows.

[Deutsch](#deutsch) · [English](#english)

---

# Deutsch

## Wozu das Ganze

24-Bit-Alben klingen auf einem DAP, im Auto oder auf dem Handy keinen Deut besser als 16 Bit — sie brauchen nur ein bis zwei Drittel mehr Platz. Das Herunterrechnen ist trotzdem heikel: ohne Dither entstehen Quantisierungsverzerrungen, beim Resampling kann das Signal über 0 dBFS schießen und clippen, und die üblichen Ein-Zeiler verlieren unterwegs Cover, mehrzeilige Tags oder Cuesheets.

Dieses Skript nimmt diese drei Punkte ernst.

## Was es macht

**Zielformat:** immer 16 Bit. Die Samplerate-Familie bleibt erhalten:

| Quelle | Ziel | | Quelle | Ziel |
|---|---|---|---|---|
| 44 100 Hz | 44 100 Hz | | 48 000 Hz | 48 000 Hz |
| 88 200 Hz | 44 100 Hz | | 96 000 Hz | 48 000 Hz |
| 176 400 Hz | 44 100 Hz | | 192 000 Hz | 48 000 Hz |

Alles bei 48 kHz oder darunter behält seine Rate. Es wird **niemals hochgerechnet** — 48 kHz aus einer 44,1-kHz-Quelle zu erzeugen fügt keine Information hinzu, sondern nur Dateigröße und Resampling-Artefakte.

**Ablauf pro Datei:**

1. `STREAMINFO` direkt aus der Datei lesen — kein `ffprobe`-Aufruf, keine Rateraffäre über Sample-Formate.
2. Wird resampelt, misst ein Vorlauf den Peak des *resampelten* Signals. Liegt er über 0 dBFS, senkt das Skript exakt so weit ab, dass nichts clippt.
3. ffmpeg rechnet nach 16-Bit-WAV (RF64 jenseits von 4 GB), mit soxr oder als Fallback swr, plus TPDF-Dither mit Hochpass.
4. Die lokale `flac`-Binary enkodiert mit `-8 -e -p` und `--verify`.
5. `VORBIS_COMMENT`, `PICTURE` und `APPLICATION` werden byte-identisch aus dem Original übernommen. `CUESHEET` nur dann, wenn die Samplerate gleich bleibt — Cuesheet-Offsets sind Sample-Positionen und wären nach dem Resampling falsch.
6. `flac -t` prüft den MD5 des dekodierten Streams.
7. Erst danach wandert das Original nach `*.flac.bak` und die neue Datei an seine Stelle. mtime und Rechte bleiben erhalten.

Das Original wird also erst angefasst, wenn das Ergebnis vollständig verifiziert vorliegt.

## Installation

Es gibt nichts zu installieren außer den beiden externen Werkzeugen. Das Skript ist eine einzelne Datei und braucht keine Python-Pakete.

```bash
git clone https://github.com/bluhtsturm/flac-downconvert.git
cd flac-downconvert
chmod +x flac_downconvert_de.py
```

**Voraussetzungen:** Python ≥ 3.8, `ffmpeg` und `flac` im `PATH`.

| System | Befehl |
|---|---|
| Debian / Ubuntu | `sudo apt install ffmpeg flac` |
| Fedora | `sudo dnf install ffmpeg flac` |
| Arch | `sudo pacman -S ffmpeg flac` |
| macOS | `brew install ffmpeg flac` |
| Windows | `winget install Gyan.FFmpeg` und `choco install flac` |

Liegen die Binaries woanders, hilft `--ffmpeg` / `--flac` oder die Umgebungsvariablen `FFMPEG` / `FLAC`.

## Benutzung

```bash
# Erst schauen, was passieren würde
python3 flac_downconvert_de.py ~/Musik/Album -n

# Dann konvertieren
python3 flac_downconvert_de.py ~/Musik/Album

# Ganze Bibliothek, 8 parallel, ohne Rückfrage, mit Logfile
python3 flac_downconvert_de.py ~/Musik -y -j 8 --log ~/flac.log
```

Ohne Pfadangabe arbeitet das Skript im aktuellen Verzeichnis. Ordner werden rekursiv durchsucht.

Ein zweiter Lauf über denselben Ordner ist gefahrlos: alles bereits Konvertierte wird als `bereits 16 Bit` übersprungen.

### Optionen

| Option | Wirkung |
|---|---|
| `-n`, `--dry-run` | nur anzeigen, nichts schreiben |
| `-y`, `--yes` | ohne Rückfrage starten |
| `-j N`, `--jobs N` | N Dateien parallel (Default: CPU-Kerne, max. 4) |
| `--rate {auto,44100,48000}` | Zielrate. `auto` behält die Familie |
| `--dither METHODE` | `triangular_hp` (Default), `shibata`, `lipshitz`, … |
| `--resampler {auto,soxr,swr}` | Engine. `auto` nimmt soxr, wenn vorhanden |
| `--fast` | nur `-8` statt `-8 -e -p` |
| `--no-backup` | Sicherung nach erfolgreichem Tausch löschen |
| `--backup-suffix SUFFIX` | Suffix der Sicherung (Default `.bak`) |
| `--force` | vorhandene Sicherungen überschreiben |
| `--keep-replaygain` | ReplayGain-Tags auch nach Absenkung behalten |
| `--temp-dir PFAD` | Ablage der WAV-Zwischendatei |
| `--log DATEI` | Ausgabe zusätzlich in eine Datei schreiben |
| `--ffmpeg PFAD`, `--flac PFAD` | Binaries explizit angeben |

**Exit-Codes:** `0` alles gut · `1` mindestens ein Fehler oder Abbruch an der Rückfrage · `130` mit Strg-C abgebrochen.

## Wenn etwas schiefgeht

Die Sicherungen aufräumen, sobald du zufrieden bist:

```bash
find ~/Musik -name '*.flac.bak' -delete
```

Alles zurückrollen:

```bash
find ~/Musik -name '*.flac.bak' -exec sh -c 'mv "$1" "${1%.bak}"' _ {} \;
```

**Was, wenn der Rechner mitten im Lauf abstürzt?** Die neue Datei entsteht immer erst vollständig als `*.new.tmp` im Zielverzeichnis. Der eigentliche Tausch besteht aus zwei `rename()`-Aufrufen auf demselben Dateisystem und ist damit atomar. Liegen gebliebene `*.enc.tmp` oder `*.new.tmp` sind Müll und können gelöscht werden; das Original ist entweder noch da oder liegt als `.bak` vor.

## Details, die vielleicht interessieren

**Warum kein `metaflac` für die Metadaten?** Weil `--export-tags-to=-` ein zeilenbasiertes Format benutzt und mehrzeilige Tags wie `UNSYNCEDLYRICS` dabei zerfallen. Und weil `--import-picture-from` jedes Bild auf Typ 3 (Front Cover) setzt und Description sowie Dimensionsangaben wegwirft — aus einem Back Cover wird ein zweites Front Cover. Das Skript bringt deshalb einen kleinen FLAC-Blockparser mit (rund 60 Zeilen) und kopiert die Blöcke roh.

**Warum ein Peak-Vorlauf?** Resampling ist Interpolation, und Interpolation überschwingt. Ein auf 0 dBFS gemastertes Album kann nach 96 → 48 kHz Spitzen von +2 dBFS haben. Wer das direkt nach int16 quantisiert, clippt hart — und merkt es nicht, weil `sox` und `ffmpeg` dabei den Exit-Code 0 liefern.

**Warum `-8 -e -p` und nicht `-8`?** Weil es der höchste Kompressionsmodus des Referenz-Encoders ist, wie in der Aufgabenstellung verlangt. Praktisch bringt es unter 0,5 % gegenüber `-8`, kostet aber grob das Zehn- bis Zwanzigfache an Rechenzeit. Bei großen Bibliotheken ist `--fast` mit hohem `-j` deutlich sinnvoller.

**Warum fällt der Resampler manchmal auf swr zurück?** Nicht jeder ffmpeg-Build enthält libsoxr. Das Skript testet das beim Start, indem es 0,05 Sekunden Stille durch den Filter schickt. Fehlt soxr, kommt ffmpegs eigener Resampler zum Einsatz — allerdings mit `filter_size=256` statt der Voreinstellung 32, Kaiser-Fenster und `beta=12`. Für 96 → 48 kHz ist das unkritisch. Ob dein Build soxr hat: `ffmpeg -buildconf | grep soxr`.

**ReplayGain:** Musste wegen Clipping-Gefahr abgesenkt werden, sind die gespeicherten Gain- und Peak-Werte falsch. Das Skript entfernt sie dann, damit sie neu berechnet werden können. Mit `--keep-replaygain` bleiben sie stehen.

## Bekannte Grenzen

- Es wird immer der erste Audiostream verarbeitet. FLAC-Dateien mit mehreren Audiostreams sind exotisch, würden aber auf einen reduziert.
- `SEEKTABLE` kommt vom Encoder, nicht aus dem Original — anders geht es nicht, die Offsets beziehen sich auf die neuen Frames.
- Der `encoder=`-Tag des Originals bleibt im Vorbis-Comment stehen. Das ist Absicht: das Skript kopiert Tags, es kuratiert sie nicht.
- Auf Netzlaufwerken (SMB, NFS) ist `rename()` je nach Server nicht garantiert atomar.

## Lizenz

MIT

---

# English

## Why

24-bit albums sound no better than 16-bit on a DAP, in a car or on a phone — they just take one and a half to two times the space. Down-converting is trickier than it looks, though: without dithering you get quantisation distortion, resampling can push the signal past 0 dBFS and clip, and the usual one-liners lose cover art, multi-line tags or cuesheets along the way.

This script takes those three things seriously.

## What it does

**Target format:** always 16 bit. The sample rate family is preserved:

| Source | Target | | Source | Target |
|---|---|---|---|---|
| 44,100 Hz | 44,100 Hz | | 48,000 Hz | 48,000 Hz |
| 88,200 Hz | 44,100 Hz | | 96,000 Hz | 48,000 Hz |
| 176,400 Hz | 44,100 Hz | | 192,000 Hz | 48,000 Hz |

Anything at or below 48 kHz keeps its rate. It **never upsamples** — turning a 44.1 kHz source into 48 kHz adds no information, only file size and resampling artefacts.

**Per file:**

1. Read `STREAMINFO` straight from the file — no `ffprobe` call, no guessing from sample formats.
2. When resampling, a first pass measures the peak of the *resampled* signal. If it exceeds 0 dBFS, the script attenuates by exactly enough to avoid clipping.
3. ffmpeg renders to 16-bit WAV (RF64 beyond 4 GB) using soxr, or swr as a fallback, with high-pass TPDF dithering.
4. The local `flac` binary encodes with `-8 -e -p` and `--verify`.
5. `VORBIS_COMMENT`, `PICTURE` and `APPLICATION` are carried over byte for byte from the original. `CUESHEET` only when the sample rate is unchanged — cuesheet offsets are sample positions and would be wrong after resampling.
6. `flac -t` checks the MD5 of the decoded stream.
7. Only then does the original move to `*.flac.bak` and the new file take its place. mtime and permissions are preserved.

In other words, the original is not touched until a fully verified result exists.

## Installation

There is nothing to install beyond the two external tools. The script is a single file and needs no Python packages.

```bash
git clone https://github.com/bluhtsturm/flac-downconvert.git
cd flac-downconvert
chmod +x flac_downconvert_en.py
```

**Requirements:** Python ≥ 3.8, plus `ffmpeg` and `flac` on `PATH`.

| System | Command |
|---|---|
| Debian / Ubuntu | `sudo apt install ffmpeg flac` |
| Fedora | `sudo dnf install ffmpeg flac` |
| Arch | `sudo pacman -S ffmpeg flac` |
| macOS | `brew install ffmpeg flac` |
| Windows | `winget install Gyan.FFmpeg` and `choco install flac` |

If the binaries live elsewhere, use `--ffmpeg` / `--flac` or the `FFMPEG` / `FLAC` environment variables.

## Usage

```bash
# See what would happen first
python3 flac_downconvert_en.py ~/Music/Album -n

# Then convert
python3 flac_downconvert_en.py ~/Music/Album

# Whole library, 8 in parallel, no prompt, with a log file
python3 flac_downconvert_en.py ~/Music -y -j 8 --log ~/flac.log
```

With no path given, the script works on the current directory. Directories are searched recursively.

Running it twice over the same folder is safe: anything already converted is reported as `already 16 bit` and skipped.

### Options

| Option | Effect |
|---|---|
| `-n`, `--dry-run` | show only, write nothing |
| `-y`, `--yes` | start without a confirmation prompt |
| `-j N`, `--jobs N` | N files in parallel (default: CPU cores, capped at 4) |
| `--rate {auto,44100,48000}` | target rate. `auto` keeps the family |
| `--dither METHOD` | `triangular_hp` (default), `shibata`, `lipshitz`, … |
| `--resampler {auto,soxr,swr}` | engine. `auto` picks soxr when available |
| `--fast` | use `-8` instead of `-8 -e -p` |
| `--no-backup` | delete the backup after a successful swap |
| `--backup-suffix SUFFIX` | suffix for the backup (default `.bak`) |
| `--force` | overwrite existing backups |
| `--keep-replaygain` | keep ReplayGain tags even after attenuation |
| `--temp-dir PATH` | where to put the intermediate WAV |
| `--log FILE` | also write output to a file |
| `--ffmpeg PATH`, `--flac PATH` | point at specific binaries |

**Exit codes:** `0` all good · `1` at least one error, or declined at the prompt · `130` interrupted with Ctrl-C.

## When things go wrong

Clean up the backups once you are satisfied:

```bash
find ~/Music -name '*.flac.bak' -delete
```

Roll everything back:

```bash
find ~/Music -name '*.flac.bak' -exec sh -c 'mv "$1" "${1%.bak}"' _ {} \;
```

**What if the machine dies mid-run?** The new file is always built to completion as `*.new.tmp` in the target directory first. The swap itself is two `rename()` calls on the same filesystem, which is atomic. Leftover `*.enc.tmp` or `*.new.tmp` files are garbage and can be deleted; the original is either still in place or sitting there as `.bak`.

## Details you may care about

**Why not use `metaflac` for the metadata?** Because `--export-tags-to=-` uses a line-based format, so multi-line tags such as `UNSYNCEDLYRICS` fall apart in transit. And because `--import-picture-from` forces every image to type 3 (front cover) and discards the description and dimension fields — a back cover comes out as a second front cover. So the script ships a small FLAC block parser (about 60 lines) and copies the blocks raw.

**Why the peak pass?** Resampling is interpolation, and interpolation overshoots. An album mastered to 0 dBFS can peak at +2 dBFS after 96 → 48 kHz. Quantise that straight to int16 and it clips hard — silently, because both `sox` and `ffmpeg` return exit code 0 while it happens.

**Why `-8 -e -p` rather than `-8`?** Because it is the reference encoder's highest compression mode. In practice it buys under 0.5 % over `-8` while costing roughly ten to twenty times the CPU time. For large libraries, `--fast` with a high `-j` is the better trade.

**Why does the resampler sometimes fall back to swr?** Not every ffmpeg build includes libsoxr. The script tests for it at startup by pushing 0.05 seconds of silence through the filter. Without soxr it uses ffmpeg's own resampler, but with `filter_size=256` instead of the default 32, a Kaiser window and `beta=12`. For 96 → 48 kHz that is perfectly fine. To check your build: `ffmpeg -buildconf | grep soxr`.

**ReplayGain:** if attenuation was needed to prevent clipping, the stored gain and peak values are wrong. The script removes them in that case so they can be recomputed. `--keep-replaygain` keeps them.

## Known limitations

- Only the first audio stream is processed. FLAC files with several audio streams are exotic, but would be reduced to one.
- `SEEKTABLE` comes from the encoder rather than the original — it has to, since the offsets refer to the new frames.
- The original's `encoder=` tag stays in the Vorbis comment. That is deliberate: the script copies tags, it does not curate them.
- On network shares (SMB, NFS), `rename()` is not guaranteed atomic depending on the server.

## License

MIT
