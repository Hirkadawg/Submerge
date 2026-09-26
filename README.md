# SubMerge

Show two subtitles at the same time in VLC (or any player that supports `.ass`).
SubMerge combines two subtitle files into one, both at the bottom of the screen: the main subtitle
on the upper line and the second one below it.

## Features

- **Uses the main subtitle's timing.** The second subtitle is fitted to it:
  - **Auto-detect** finds a constant delay, and also a frame-rate mismatch (23.976 ↔ 25 fps).
  - **Sync points** let you click the same sentence in both files: 1 point fixes a delay, 2 or more also fix slow drift.
  - **Snapping** gives lines within a tolerance exactly the main line's timing.
- **Layouts:** both at the bottom (either order), or one at the top of the screen and one at the bottom.
- **Japanese → romaji** with proper word splitting (`minna ochitsuku n da`).
- **Word colouring:** Japanese and English words with the same meaning get the same colour, using the free
  [JMdict](https://www.edrdg.org/wiki/index.php/JMdict-EDICT_Dictionary_Project) dictionary.
  Words it can't match stay uncoloured.
- **Hint mode** (`.ass` only): shows only the main (English) subtitle, with the Japanese of matched words
  in small yellow text right above them, as romaji, Japanese, or both.
- **Split lines:** a second-subtitle line that spans two main lines is shown under both.
- **Left out:** second-subtitle lines with no main line at that time (gasps, songs…) are dropped.
- **On-screen text** (titles, captions) in a main `.ass` file keeps its own style and position.
- **Optional cleanup:**
  - Remove speaker names and sound descriptions (`（テンマ）はい` → `はい`, `[knocking]` → removed).
  - Remove Japanese reading hints (`弛緩(しかん)` → `弛緩`, read as *shikan* in romaji).
- Reads `.srt`, `.vtt`, `.ass`; detects UTF-8 / Windows encodings (including Turkish); keeps italics and bold.

## Install

Requires Python 3.9+. Licensed under the MIT License (see `LICENSE`).

```bash
python -m pip install pykakasi fugashi unidic-lite
```

These are only needed for the Japanese features; merging works with plain Python.

## Use

On Windows, double-click **`SubMerge.bat`**. On first start it installs the Japanese libraries if they
are missing. Or run `python submerge.py`. Then:

1. Choose the main subtitle (timing reference) and the second subtitle.
2. Click **Check sync**. If the match rate is low, click the same sentence in both lists and press **Add sync point**.
3. Click **Merge & Save…** and load the `.ass` file in VLC (*Subtitle → Add Subtitle File…*). If you name it like the video (`movie.mkv` → `movie.ass`), VLC loads it automatically.

Command line:

```bash
python submerge.py main.srt second.srt -o out.ass --romaji2 --color
python submerge.py --help
```

## Credits

- Dictionary data: [JMdict](https://www.edrdg.org/wiki/index.php/JMdict-EDICT_Dictionary_Project) by the
  Electronic Dictionary Research and Development Group, licensed under
  [CC BY-SA 4.0](https://www.edrdg.org/edrdg/licence.html). It is downloaded on first use from
  [jmdict-simplified](https://github.com/scriptin/jmdict-simplified) and is not included in this repository.
- Japanese word splitting: [fugashi](https://github.com/polm/fugashi) + [unidic-lite](https://github.com/polm/unidic-lite);
  romaji: [pykakasi](https://codeberg.org/miurahr/pykakasi).
