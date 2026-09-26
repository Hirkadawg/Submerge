#!/usr/bin/env python3
"""SubMerge - combine two subtitle files into one dual-language subtitle file.

The MAIN subtitle is the timing reference. The SECOND subtitle is shifted /
stretched / snapped onto the main subtitle's timing, then both are written to a
single .ass file (both at the bottom, main above second, by default) that VLC can play.
Optional: Japanese -> romaji, and colouring Japanese/English words with the same
meaning using the JMdict dictionary (downloaded once into jmdict_index.json.gz).

Usage:
    python submerge.py                      -> opens the GUI
    python submerge.py main.srt second.srt -o out.ass [options]   -> command line
"""
import argparse
import bisect
import codecs
import functools
import gzip
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass

ENCODINGS = ['auto', 'utf-8', 'cp1254', 'cp1252', 'iso-8859-9', 'cp1250', 'cp1251',
             'cp1253', 'cp1256', 'iso-8859-1', 'utf-16']
# Frame-rate conversions tried by auto-detect (23.976 <-> 25 fps is the classic mismatch).
SCALES = (1.0, 25 / 23.976, 23.976 / 25, 25 / 24, 24 / 25, 24 / 23.976, 23.976 / 24)


@dataclass
class Cue:
    start: int  # milliseconds
    end: int
    text: str   # lines separated by '\n', may contain <i>/<b>/<font> tags


# ---------------------------------------------------------------- reading ---

def read_text(path, encoding='auto'):
    """Return (text, encoding_used)."""
    with open(path, 'rb') as f:
        raw = f.read()
    if encoding != 'auto':
        return raw.decode(encoding, errors='replace'), encoding
    if raw.startswith(codecs.BOM_UTF8):
        return raw[3:].decode('utf-8', errors='replace'), 'utf-8'
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode('utf-16', errors='replace'), 'utf-16'
    try:
        return raw.decode('utf-8'), 'utf-8'
    except UnicodeDecodeError:
        pass
    text = raw.decode('cp1252', errors='replace')
    # Turkish cp1254 text decoded as cp1252 shows up as Ý ý Þ þ Ð ð (İ ı Ş ş Ğ ğ).
    if sum(text.count(c) for c in 'ÝýÞþÐð') >= 3:
        return raw.decode('cp1254', errors='replace'), 'cp1254'
    return text, 'cp1252'


TIME_RE = re.compile(r'(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:[,.](\d{1,3}))?')


def parse_time(s):
    m = TIME_RE.search(s)
    if not m:
        raise ValueError(f'bad time: {s!r}')
    h, mi, se, ms = m.groups()
    return ((int(h or 0) * 60 + int(mi)) * 60 + int(se)) * 1000 + int((ms or '0').ljust(3, '0'))


def parse_srt_vtt(text):
    cues = []
    blocks = re.split(r'\n[ \t]*\n', text.replace('\r\n', '\n').replace('\r', '\n'))
    for block in blocks:
        lines = block.strip('\n').split('\n')
        idx = next((i for i, l in enumerate(lines) if '-->' in l), None)
        if idx is None:
            # Blank line inside a cue: glue stray text to the previous cue.
            if cues and block.strip() and not block.strip().isdigit() and block.strip() != 'WEBVTT':
                cues[-1].text += '\n' + block.strip()
            continue
        left, right = lines[idx].split('-->', 1)
        try:
            start, end = parse_time(left), parse_time(right)
        except ValueError:
            continue
        body = '\n'.join(l.strip() for l in lines[idx + 1:]).strip()
        if body:
            cues.append(Cue(start, end, body))
    return cues


def parse_ass(text):
    cues, fmt = [], None
    for line in text.splitlines():
        if line.startswith('Format:') and fmt is None and 'Text' in line and 'Start' in line:
            fmt = [f.strip().lower() for f in line[7:].split(',')]
        elif line.startswith('Dialogue:'):
            f = fmt or ['layer', 'start', 'end', 'style', 'name', 'marginl', 'marginr',
                        'marginv', 'effect', 'text']
            parts = line[9:].split(',', len(f) - 1)
            if len(parts) < len(f):
                continue
            d = dict(zip(f, parts))
            body = re.sub(r'\{[^}]*\}', '', d['text']).replace('\\N', '\n').replace('\\n', '\n')
            body = body.replace('\\h', ' ').strip()
            if body:
                cues.append(Cue(parse_time(d['start']), parse_time(d['end']), body))
    return cues


def load_subtitle(path, encoding='auto'):
    text, used = read_text(path, encoding)
    if path.lower().endswith(('.ass', '.ssa')):
        cues = parse_ass(text)
    else:
        cues = parse_srt_vtt(text)
    cues.sort(key=lambda c: (c.start, c.end))
    return cues, used


# ---------------------------------------------------------------- japanese ---

PARTICLES = {'は': 'wa', 'へ': 'e', 'を': 'o'}
READINGS = {'私': 'watashi'}  # dictionary gives the formal "watakushi"
JP_PUNCT = str.maketrans({'、': ',', '。': '.', '・': ' ', '〜': '~', '～': '~'})
OPENERS = {'「': '"', '『': '"', '（': '(', '(': '(', '【': '['}
CLOSERS = {'」': '"', '』': '"', '）': ')', ')': ')', '】': ']'}
JP_CHARS = re.compile(r'[぀-ヿ㐀-鿿]')
# Word types worth looking up in the dictionary (nouns, verbs, adjectives, adverbs...).
CONTENT_POS = {'名詞', '代名詞', '動詞', '形容詞', '形状詞', '副詞', '感動詞', '連体詞'}
# Grammar-like verbs/nouns ("to do", "to be", "thing") that would only give false matches.
SKIP_LEMMAS = {'為る', 'する', '居る', 'いる', '有る', 'ある', '在る', '成る', 'なる', '事', 'こと',
               '物', 'もの', '方', 'ほう', '其れ', 'それ', '此れ', 'これ', '彼れ', 'あれ', '其の', 'その', '此の', 'この'}
_kakasi = None
_tagger = False  # False = not tried yet, None = fugashi unavailable


@dataclass(frozen=True)
class JpWord:
    orig: str
    romaji: str
    keys: tuple = ()     # dictionary lookup keys, best first; empty = not a content word
    english: str = ''    # English origin of loanwords (コーヒー -> coffee), from the tokenizer


def _kana_to_romaji(text):
    return ''.join(x['hepburn'] for x in _kakasi.convert(text)).strip()


def _hiragana(text):
    return ''.join(chr(ord(c) - 0x60) if 'ァ' <= c <= 'ヶ' else c for c in text)


def clean_japanese(line):
    return unicodedata.normalize('NFKC', re.sub(r'<[^>]+>|\{[^}]*\}', '', line))


@functools.lru_cache(maxsize=8192)
def japanese_words(line):
    """Split one line into JpWord's. Uses fugashi (real word splitting and grammar,
    if installed) and falls back to pykakasi's rougher splitting."""
    global _kakasi, _tagger
    if _kakasi is None:
        try:
            import pykakasi
        except ImportError:
            raise RuntimeError('Romaji conversion needs the pykakasi library.\n'
                               'Install it with:  python -m pip install pykakasi fugashi unidic-lite')
        _kakasi = pykakasi.kakasi()
    if _tagger is False:
        try:
            import fugashi
            _tagger = fugashi.Tagger()
        except Exception:
            _tagger = None
    words = []
    if _tagger is None:
        for item in _kakasi.convert(line):
            orig = item['orig']
            content = bool(JP_CHARS.search(orig)) and orig not in PARTICLES
            words.append(JpWord(orig, PARTICLES.get(orig, item['hepburn'].strip()), (orig,) if content else ()))
        return tuple(words)
    for w in _tagger(line):
        s, f = w.surface, w.feature
        pos1 = getattr(f, 'pos1', None)
        if pos1 in ('補助記号', '空白') or not JP_CHARS.search(s):
            words.append(JpWord(s, s))  # punctuation, latin letters, digits
            continue
        if pos1 == '助詞' and s in PARTICLES:
            r = PARTICLES[s]
        elif s in READINGS:
            r = READINGS[s]
        else:
            r = _kana_to_romaji(getattr(f, 'kana', None) or s)
        keys, english = (), ''
        lemma, _, origin = (getattr(f, 'lemma', None) or '').partition('-')
        base = getattr(f, 'orthBase', None)
        if pos1 in CONTENT_POS and lemma not in SKIP_LEMMAS and base not in SKIP_LEMMAS:
            kana = _hiragana(getattr(f, 'kanaBase', None) or '')
            keys = tuple(dict.fromkeys(k for k in (base, lemma, s, kana) if k and k != '*'))
            english = origin if origin.isascii() and origin.isalpha() else ''
        words.append(JpWord(s, r, keys, english))
    return tuple(words)


def cue_words(text):
    """JpWord's of every line of a cue, as a list of lines."""
    return [japanese_words(clean_japanese(line)) for line in text.split('\n')]


def paint(s, color, fmt, base):
    if not color or fmt == 'plain':
        return s
    if fmt == 'ass':
        return '{\\c&H%s&}%s{\\c&H%s&}' % (bgr(color), s, bgr(base))
    return f'<font color="#{color}">{s}</font>'


def bgr(rgb):
    return rgb[4:6] + rgb[2:4] + rgb[0:2]


def render_japanese(text, romaji, colors=None, fmt='plain', base='FFFFFF'):
    """Japanese text (optionally as romaji), with colours: {word number: 'RRGGBB'}."""
    colors = colors or {}
    out_lines, n = [], 0
    for words in cue_words(text):
        out, glue = '', True
        for w in words:
            color = colors.get(n)
            n += 1
            if not romaji:
                out += paint(w.orig, color, fmt, base)
                continue
            r = re.sub(r'(konnichi|konban)ha$', r'\1wa', w.romaji.strip())
            if not r:
                continue
            if not re.search(r'\w', w.orig):  # punctuation: attach to the neighbouring word
                for ch in w.orig:
                    if ch in OPENERS:
                        out += ('' if glue else ' ') + OPENERS[ch]
                        glue = True
                    elif not ch.translate(JP_PUNCT).isspace():
                        out += CLOSERS.get(ch, ch.translate(JP_PUNCT))
                        glue = False
            else:
                out += ('' if glue else ' ') + paint(r, color, fmt, base)
                glue = False
        out_lines.append(out.strip())
    return '\n'.join(out_lines)


def romaji_text(text):
    return render_japanese(text, True)


# -------------------------------------------------------- word colouring ---

# English words never used for matching (grammar words that appear in countless glosses).
STOP = set('''a an the to of in on at for from by with as into onto about be is are was were been
being am do does did done have has had it its it's this that these those there and or but if so
not one one's oneself someone something somebody somewhere sth sb etc esp eg ie oneself's
let let's get got'''.split())
# Only allowed as the continuation of a phrase ("calm" + "down"), never as a match on their own.
WEAK = set('up down out off away back over around along through'.split())
PALETTE = ('66D9FF', '8CFF66', 'FF9F40', 'FF80D5', 'B38CFF', '4DFFC3', 'FF6B6B')
EN_WORD = re.compile(r"<[^>]*>|\{[^}]*\}|([A-Za-z]+(?:'[A-Za-z]+)*)")
DICT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'jmdict_index.json.gz')
DICT_RELEASES = 'https://api.github.com/repos/scriptin/jmdict-simplified/releases/latest'


def stem(word):
    """Very small English stemmer: 'emergencies' -> 'emergency', 'calmed' -> 'calm'."""
    w = word.lower()
    for suf, rep in (('ies', 'y'), ('ied', 'y'), ('ying', 'y'), ('ing', ''), ('ed', ''), ('es', ''), ('s', '')):
        if w.endswith(suf) and len(w) - len(suf) >= (2 if suf == 'ing' else 3):  # going -> go
            w = w[:-len(suf)] + rep
            if suf in ('ing', 'ed') and len(w) > 3 and w[-1] == w[-2] and w[-1] not in 'aeiouls':
                w = w[:-1]  # stopped -> stop
            break
    if w.endswith('e') and len(w) >= 4:
        w = w[:-1]  # settle / settled -> settl
    return w


def english_words(text):
    return [m.group(1) for m in EN_WORD.finditer(text) if m.group(1)]


def render_other(text, colors, fmt, base):
    counter = iter(range(10 ** 9))

    def sub(m):
        if not m.group(1):
            return m.group(0)
        return paint(m.group(1), colors.get(next(counter)), fmt, base)
    return EN_WORD.sub(sub, text) if colors else text


class Dictionary:
    """JMdict, reduced to {japanese word: stems of its English meanings}."""

    def __init__(self, path=DICT_FILE):
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            self.index = json.load(f)

    def meanings(self, w):
        kws = set()
        for k in w.keys:
            if k in self.index:
                kws = set(self.index[k].split())
                break
        if w.english:
            kws.add(stem(w.english))
        return kws


_dictionary = None


def get_dictionary():
    """Load the JMdict index; raises FileNotFoundError if it was never downloaded."""
    global _dictionary
    if _dictionary is None:
        _dictionary = Dictionary()
    return _dictionary


def download_dictionary(log=print):
    """Download JMdict (English) from github.com/scriptin/jmdict-simplified and build the index."""
    import tempfile
    import urllib.request
    import zipfile
    log('Finding the latest JMdict release...')
    with urllib.request.urlopen(DICT_RELEASES, timeout=30) as r:
        assets = json.load(r)['assets']
    url = next(a['browser_download_url'] for a in assets
               if re.fullmatch(r'jmdict-eng-\d.*\.json\.zip', a['name']))
    tmp = os.path.join(tempfile.gettempdir(), 'jmdict-eng.json.zip')
    log('Downloading JMdict (about 12 MB)...')
    urllib.request.urlretrieve(url, tmp)
    log('Building the word index (takes a moment, only needed once)...')
    with zipfile.ZipFile(tmp) as z:
        data = json.loads(z.read(z.namelist()[0]))
    os.remove(tmp)
    index = {}
    for entry in data['words']:
        kws = set()
        for sense in entry['sense'][:5]:
            for g in sense['gloss']:
                t = re.sub(r'\([^)]*\)', ' ', g['text'].lower())
                kws.update(stem(w) for w in re.findall(r"[a-z]+(?:'[a-z]+)*", t) if w not in STOP)
        if kws:
            for k in entry['kanji'] + entry['kana']:
                index.setdefault(k['text'], set()).update(kws)
    del data
    with gzip.open(DICT_FILE, 'wt', encoding='utf-8') as f:
        json.dump({k: ' '.join(sorted(v)) for k, v in index.items()}, f, ensure_ascii=False)
    log(f'Dictionary ready: {len(index)} words.')


def align(ja_text, en_text, dictionary):
    """Match Japanese words to English words that mean the same thing.
    Returns ({japanese word number: color}, {english word number: color})."""
    ja = [w for line in cue_words(ja_text) for w in line]
    en = [w.lower() for w in english_words(en_text)]
    en_stems = [None if w in STOP else stem(w) for w in en]
    used, ja_colors, en_colors = set(), {}, {}
    for i, w in enumerate(ja):
        if not w.keys:
            continue
        meanings = dictionary.meanings(w)
        hits = {j for j, s in enumerate(en_stems) if s and s in meanings and j not in used}
        starts = sorted(j for j in hits if en[j] not in WEAK)
        if not starts:
            continue
        span = [starts[0]]
        while span[-1] + 1 in hits:  # "calm" + "down"
            span.append(span[-1] + 1)
        color = PALETTE[len(ja_colors) % len(PALETTE)]
        ja_colors[i] = color
        for j in span:
            en_colors[j] = color
        used.update(span)
    return ja_colors, en_colors


def render_event(role, text, fmt, romaji=(False, False), dictionary=None, bases=('FFFFFF', 'FFFFFF')):
    """Returns (main, second) display texts for one event (None where absent)."""
    sides = list(text) if role == 'pair' else ([text, None] if role == 'main' else [None, text])
    colors = [{}, {}]
    if role == 'pair' and dictionary:
        is_ja = [bool(JP_CHARS.search(s)) for s in sides]
        if is_ja[0] != is_ja[1]:  # exactly one Japanese side
            j = 0 if is_ja[0] else 1
            colors[j], colors[1 - j] = align(sides[j], sides[1 - j], dictionary)
    out = []
    for k, s in enumerate(sides):
        if s is None:
            out.append(None)
        elif (romaji[k] or colors[k]) and JP_CHARS.search(s):
            out.append(render_japanese(s, romaji[k], colors[k], fmt, bases[k]))
        else:
            out.append(render_other(s, colors[k], fmt, bases[k]))
    return out


# ----------------------------------------------------------------- syncing ---

class TimeMap:
    """Maps a time in the SECOND subtitle to the MAIN subtitle's timeline."""

    def __init__(self, anchors=(), offset=0, scale=1.0):
        pts = {}
        for t2, t1 in anchors:
            pts[t2] = t1
        self.anchors = sorted(pts.items())
        self.offset, self.scale = offset, scale

    def __call__(self, t):
        a = self.anchors
        if not a:
            return round(t * self.scale + self.offset)
        if len(a) == 1:
            return t + a[0][1] - a[0][0]
        # piecewise linear between sync points, extrapolated at both ends
        i = bisect.bisect_right([x for x, _ in a], t)
        i = min(max(i, 1), len(a) - 1)
        (x0, y0), (x1, y1) = a[i - 1], a[i]
        return round(y0 + (t - x0) * (y1 - y0) / (x1 - x0))

    def describe(self):
        if len(self.anchors) == 1:
            return f'shift {fmt_offset(self.anchors[0][1] - self.anchors[0][0])} (1 sync point)'
        if self.anchors:
            return f'{len(self.anchors)} sync points (shift + speed correction)'
        s = f'shift {fmt_offset(self.offset)}'
        if abs(self.scale - 1) > 1e-6:
            s += f', speed x{self.scale:.4f}'
        return s


def fmt_offset(ms):
    return f'{"+" if ms >= 0 else "-"}{abs(ms) / 1000:.2f}s'


def estimate_sync(first, second, max_shift=300_000, bucket=100):
    """Guess offset/scale that best lines up SECOND with MAIN.
    Returns (offset_ms, scale, confidence 0..1)."""
    if not first or not second:
        return 0, 1.0, 0.0
    s1 = sorted(c.start for c in first)
    results = []
    for scale in SCALES:
        hist = {}
        for c in second:
            t = c.start * scale
            lo = bisect.bisect_left(s1, t - max_shift)
            hi = bisect.bisect_right(s1, t + max_shift)
            for x in s1[lo:hi]:
                b = round((x - t) / bucket)
                hist[b] = hist.get(b, 0) + 1
        best_b, best_score = 0, -1
        for b in hist:
            score = sum(hist.get(b + k, 0) for k in (-2, -1, 0, 1, 2))
            if score > best_score or (score == best_score and abs(b) < abs(best_b)):
                best_b, best_score = b, score
        results.append((best_score, scale, best_b * bucket))
    base = results[0]
    best = max(results, key=lambda r: r[0])
    if best[0] < base[0] * 1.3:  # only change speed if clearly better
        best = base
    score, scale, offset = best
    return offset, scale, min(1.0, score / len(second))


@dataclass
class MergeResult:
    events: list          # (start, end, role, text); role 'main'/'second', or 'pair' with text=(main, second)
    matched: int
    unmatched: int
    main_without_second: int


def merge(first, second, tmap, tolerance=1000):
    moved = [Cue(max(0, tmap(c.start)), max(0, tmap(c.end)), c.text) for c in second]
    starts1 = [c.start for c in first]
    groups = {}
    loose = []
    for c in moved:
        lo = bisect.bisect_left(starts1, c.start - 15000)
        hi = bisect.bisect_right(starts1, c.end + tolerance)
        best_i, best_ov, best_near = None, 0, None
        for i in range(lo, hi):
            f = first[i]
            ov = min(f.end, c.end) - max(f.start, c.start)
            if ov > best_ov:
                best_i, best_ov = i, ov
            d = abs(f.start - c.start)
            if d <= tolerance and (best_near is None or d < best_near[0]):
                best_near = (d, i)
        ok = False
        if best_i is not None:
            f = first[best_i]
            shorter = max(1, min(f.end - f.start, c.end - c.start))
            ok = best_ov >= 0.5 * shorter or abs(f.start - c.start) <= tolerance
        if not ok and best_near:
            best_i, ok = best_near[1], True
        if ok:
            groups.setdefault(best_i, []).append(c)
        else:
            loose.append(c)

    events = []
    for i, f in enumerate(first):
        if i in groups:
            text = '\n'.join(c.text for c in sorted(groups[i], key=lambda c: c.start))
            events.append((f.start, f.end, 'pair', (f.text, text)))
        else:
            events.append((f.start, f.end, 'main', f.text))
    for c in loose:
        events.append((c.start, c.end, 'second', c.text))
    events.sort(key=lambda e: e[0])
    return MergeResult(events, len(moved) - len(loose), len(loose),
                       sum(1 for i in range(len(first)) if i not in groups))


# ----------------------------------------------------------------- writing ---

def ass_time(ms):
    cs = round(max(0, ms) / 10)
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f'{h}:{m:02}:{s:02}.{cs:02}'


def srt_time(ms):
    ms = max(0, ms)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f'{h:02}:{m:02}:{s:02},{ms:03}'


POS_TAG_RE = re.compile(r'\{\\(?:an?\d+|pos\([^)]*\))\}')


def to_ass_text(text):
    t = POS_TAG_RE.sub('', text)

    def color(m):
        c = m.group(1)
        return '{\\c&H%s%s%s&}' % (c[4:6], c[2:4], c[0:2])
    t = re.sub(r'<font[^>]*?color\s*=\s*["\']?#?([0-9a-fA-F]{6})["\']?[^>]*>', color, t, flags=re.I)
    t = re.sub(r'</font\s*>', '{\\\\c}', t, flags=re.I)
    for tag in 'ibu':
        t = re.sub(rf'<{tag}\s*>', rf'{{\\{tag}1}}', t, flags=re.I)
        t = re.sub(rf'</{tag}\s*>', rf'{{\\{tag}0}}', t, flags=re.I)
    t = re.sub(r'<[^>]+>', '', t)
    return t.strip().replace('\n', '\\N')


# Screen layouts: 'main_first' / 'second_first' stack both at the bottom of the screen
# (the named one on the upper line); 'split' / 'split_rev' put one at the top of the screen.
LAYOUTS = {
    'Both at bottom, main above': 'main_first',
    'Both at bottom, second above': 'second_first',
    'Main at top of screen, second at bottom': 'split',
    'Second at top of screen, main at bottom': 'split_rev',
}


def rendered_events(result, fmt, romaji, colorize, second_yellow):
    """Yield (start, end, main_text or None, second_text or None) ready for output."""
    bases = ('FFFFFF', 'FFFF99' if second_yellow else 'FFFFFF')
    dictionary = get_dictionary() if colorize else None
    for start, end, role, text in result.events:
        main, second = render_event(role, text, fmt, romaji, dictionary, bases)
        yield start, end, main, second


def write_ass(path, result, layout='main_first', font='Arial', font_size=60, second_yellow=True,
              romaji=(False, False), colorize=False):
    main_align, second_align = {'split': (8, 2), 'split_rev': (2, 8)}.get(layout, (2, 2))
    second_color = '&H0099FFFF' if second_yellow else '&H00FFFFFF'
    style = '{name},{font},{size},{color},&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,3,1,{align},60,60,45,1'
    lines = [
        '[Script Info]', '; Created by SubMerge', 'ScriptType: v4.00+',
        'PlayResX: 1920', 'PlayResY: 1080', 'WrapStyle: 0', 'ScaledBorderAndShadow: yes', '',
        '[V4+ Styles]',
        'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, '
        'BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, '
        'BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
        'Style: ' + style.format(name='Main', font=font, size=font_size, color='&H00FFFFFF', align=main_align),
        'Style: ' + style.format(name='Second', font=font, size=font_size, color=second_color, align=second_align),
        '', '[Events]',
        'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text',
    ]
    stacked = layout in ('main_first', 'second_first')
    for start, end, main, second in rendered_events(result, 'ass', romaji, colorize, second_yellow):
        blocks = []
        if main is not None and second is not None and stacked:
            # One event, two blocks: {\r<Style>} switches colour/font for the lower block.
            main, second = to_ass_text(main), to_ass_text(second)
            if layout == 'main_first':
                blocks.append(('Main', f'{main}\\N{{\\rSecond}}{second}'))
            else:
                blocks.append(('Second', f'{second}\\N{{\\rMain}}{main}'))
        else:
            if main is not None:
                blocks.append(('Main', to_ass_text(main)))
            if second is not None:
                blocks.append(('Second', to_ass_text(second)))
        for style_name, body in blocks:
            lines.append(f'Dialogue: 0,{ass_time(start)},{ass_time(end)},{style_name},,0,0,0,,{body}')
    with open(path, 'w', encoding='utf-8-sig', newline='\r\n') as f:
        f.write('\n'.join(lines) + '\n')


def write_srt(path, result, layout='main_first', second_yellow=True, romaji=(False, False), colorize=False):
    def clean(t, is_second):
        t = POS_TAG_RE.sub('', t).strip()
        return f'<font color="#FFFF99">{t}</font>' if is_second and second_yellow else t

    stacked = layout in ('main_first', 'second_first')
    cues = []
    for start, end, main, second in rendered_events(result, 'srt', romaji, colorize, second_yellow):
        if main is not None and second is not None and stacked:
            parts = [clean(main, False), clean(second, True)]
            cues.append((start, end, '\n'.join(parts if layout == 'main_first' else parts[::-1])))
            continue
        for text, is_second in ((main, False), (second, True)):
            if text is not None:
                body = clean(text, is_second)
                if (layout, is_second) in (('split', False), ('split_rev', True)):
                    body = '{\\an8}' + body
                cues.append((start, end, body))
    out = [f'{n}\n{srt_time(s)} --> {srt_time(e)}\n{body}\n' for n, (s, e, body) in enumerate(cues, 1)]
    with open(path, 'w', encoding='utf-8-sig', newline='\r\n') as f:
        f.write('\n'.join(out))


# --------------------------------------------------------------------- CLI ---

def run_cli(argv):
    p = argparse.ArgumentParser(description='Merge two subtitles into one dual-language subtitle.')
    p.add_argument('main', help='main subtitle (timing reference)')
    p.add_argument('second', help='second subtitle (gets synced to main)')
    p.add_argument('-o', '--output', help='output file (.ass or .srt); default: <main>.dual.ass')
    p.add_argument('--sync', action='append', default=[], metavar='MAIN_TIME=SECOND_TIME',
                   help='sync point, e.g. --sync 00:01:02,500=00:01:04,100 (repeatable)')
    p.add_argument('--no-auto', action='store_true', help='do not auto-detect offset')
    p.add_argument('--tolerance', type=int, default=1000, help='snap tolerance in ms (default 1000)')
    p.add_argument('--font-size', type=int, default=60)
    p.add_argument('--layout', choices=list(LAYOUTS.values()), default='main_first',
                   help='main_first/second_first: both at the bottom, that one on the upper line; '
                        'split/split_rev: main (or second) at the top of the screen')
    p.add_argument('--white', action='store_true', help='second subtitle white instead of yellow')
    p.add_argument('--enc1', default='auto')
    p.add_argument('--enc2', default='auto')
    p.add_argument('--romaji1', action='store_true', help='convert main (Japanese) subtitle to romaji')
    p.add_argument('--romaji2', action='store_true', help='convert second (Japanese) subtitle to romaji')
    p.add_argument('--color', action='store_true',
                   help='colour Japanese/English words with the same meaning (uses JMdict)')
    a = p.parse_args(argv)

    first, e1 = load_subtitle(a.main, a.enc1)
    second, e2 = load_subtitle(a.second, a.enc2)
    print(f'Main:   {len(first)} lines ({e1})\nSecond: {len(second)} lines ({e2})')
    if a.sync:
        anchors = []
        for s in a.sync:
            t1, t2 = s.split('=')
            anchors.append((parse_time(t2), parse_time(t1)))
        tmap = TimeMap(anchors)
    elif not a.no_auto:
        off, scale, conf = estimate_sync(first, second)
        tmap = TimeMap(offset=off, scale=scale)
        print(f'Auto-detect: {tmap.describe()}  (confidence {conf:.0%})')
    else:
        tmap = TimeMap()
    res = merge(first, second, tmap, a.tolerance)
    out = a.output or os.path.splitext(a.main)[0] + '.dual.ass'
    if a.color and not os.path.exists(DICT_FILE):
        download_dictionary()
    opts = dict(second_yellow=not a.white, romaji=(a.romaji1, a.romaji2), colorize=a.color)
    if out.lower().endswith('.srt'):
        write_srt(out, res, a.layout, **opts)
    else:
        write_ass(out, res, a.layout, font_size=a.font_size, **opts)
    print(f'Matched {res.matched}, unmatched {res.unmatched} second lines; '
          f'{res.main_without_second} main lines have no partner.\nWrote {out}')


# --------------------------------------------------------------------- GUI ---

def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    def one_line(text):
        return re.sub(r'<[^>]+>|\{[^}]*\}', '', text).replace('\n', ' / ')

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title('SubMerge - dual subtitles')
            self.geometry('1200x760')
            self.subs = [[], []]
            self.anchors = []      # (index in main, index in second)
            self._auto = None

            top = ttk.Frame(self, padding=8)
            top.pack(fill='x')
            self.path_vars = [tk.StringVar(), tk.StringVar()]
            self.enc_vars = [tk.StringVar(value='auto'), tk.StringVar(value='auto')]
            self.romaji_vars = [tk.BooleanVar(value=False), tk.BooleanVar(value=False)]
            labels = ('1) Main subtitle (timing reference):', '2) Second subtitle (will be synced):')
            for i, label in enumerate(labels):
                ttk.Label(top, text=label).grid(row=i, column=0, sticky='w')
                ttk.Entry(top, textvariable=self.path_vars[i]).grid(row=i, column=1, sticky='we', padx=4, pady=2)
                ttk.Button(top, text='Browse...', command=lambda i=i: self.browse(i)).grid(row=i, column=2)
                ttk.Label(top, text=' encoding:').grid(row=i, column=3)
                cb = ttk.Combobox(top, textvariable=self.enc_vars[i], values=ENCODINGS, width=11)
                cb.grid(row=i, column=4)
                cb.bind('<<ComboboxSelected>>', lambda e, i=i: self.load(i))
                ttk.Checkbutton(top, text='Japanese -> romaji', variable=self.romaji_vars[i],
                                command=lambda i=i: self.load(i)).grid(row=i, column=5, padx=(8, 0))
            top.columnconfigure(1, weight=1)

            mid = ttk.Frame(self, padding=(8, 0))
            mid.pack(fill='both', expand=True)
            self.lists, self.search_vars = [], []
            for i, title in enumerate(('Main subtitle', 'Second subtitle')):
                f = ttk.Frame(mid)
                f.grid(row=0, column=i, sticky='nsew', padx=4)
                head = ttk.Frame(f)
                head.pack(fill='x')
                ttk.Label(head, text=title, font=('Segoe UI', 10, 'bold')).pack(side='left')
                sv = tk.StringVar()
                ent = ttk.Entry(head, textvariable=sv, width=28)
                ent.pack(side='right')
                ent.bind('<Return>', lambda e, i=i: self.search(i))
                ttk.Label(head, text='find (Enter):').pack(side='right')
                self.search_vars.append(sv)
                body = ttk.Frame(f)
                body.pack(fill='both', expand=True)
                lb = tk.Listbox(body, exportselection=False, font=('Consolas', 10), activestyle='none')
                sb = ttk.Scrollbar(body, command=lb.yview)
                lb.config(yscrollcommand=sb.set)
                sb.pack(side='right', fill='y')
                lb.pack(fill='both', expand=True)
                self.lists.append(lb)
            mid.columnconfigure((0, 1), weight=1, uniform='x')
            mid.rowconfigure(0, weight=1)
            self.lists[0].bind('<<ListboxSelect>>', self.follow_main)

            pf = ttk.LabelFrame(self, padding=6, text='Sync points (optional): click the SAME sentence in both '
                                'lists, then "Add sync point". 1 point = fixed shift; 2+ points (start & end '
                                'of the movie) also fix speed drift.')
            pf.pack(fill='x', padx=8, pady=6)
            ttk.Button(pf, text='Add sync point', command=self.add_anchor).pack(side='left', padx=(0, 6))
            self.anchor_lb = tk.Listbox(pf, height=3, font=('Consolas', 9))
            self.anchor_lb.pack(side='left', fill='x', expand=True)
            ttk.Button(pf, text='Remove', command=self.remove_anchor).pack(side='left', padx=6)

            of = ttk.Frame(self, padding=(8, 0))
            of.pack(fill='x')
            self.auto_var = tk.BooleanVar(value=True)
            self.tol_var = tk.IntVar(value=1000)
            self.size_var = tk.IntVar(value=60)
            self.pos_var = tk.StringVar(value=next(iter(LAYOUTS)))
            self.yellow_var = tk.BooleanVar(value=True)
            ttk.Checkbutton(of, text='Auto-detect shift (when no sync points)', variable=self.auto_var,
                            command=self.invalidate).pack(side='left')
            ttk.Label(of, text='   Snap tolerance (ms):').pack(side='left')
            ttk.Spinbox(of, from_=0, to=5000, increment=100, textvariable=self.tol_var, width=6).pack(side='left')
            ttk.Label(of, text='   Font size:').pack(side='left')
            ttk.Spinbox(of, from_=20, to=120, textvariable=self.size_var, width=5).pack(side='left')
            ttk.Combobox(of, textvariable=self.pos_var, values=list(LAYOUTS),
                         width=36, state='readonly').pack(side='left', padx=10)
            ttk.Checkbutton(of, text='Second subtitle in yellow', variable=self.yellow_var).pack(side='left')
            of2 = ttk.Frame(self, padding=(8, 4, 8, 0))
            of2.pack(fill='x')
            self.color_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(of2, text='Colour words with the same meaning (Japanese <-> English, uses the '
                            'free JMdict dictionary; unmatched words stay uncoloured)',
                            variable=self.color_var).pack(side='left')

            bf = ttk.Frame(self, padding=8)
            bf.pack(fill='x')
            ttk.Button(bf, text='Merge & Save...', command=self.save).pack(side='right')
            ttk.Button(bf, text='Check sync', command=self.check).pack(side='right', padx=6)
            self.status = tk.StringVar(value='Choose two subtitle files (.srt, .vtt, .ass).')
            ttk.Label(bf, textvariable=self.status, wraplength=850).pack(side='left')

        # -- loading
        def browse(self, i):
            p = filedialog.askopenfilename(filetypes=[('Subtitles', '*.srt *.vtt *.ass *.ssa'), ('All', '*.*')])
            if p:
                self.path_vars[i].set(p)
                self.load(i)

        def load(self, i):
            p = self.path_vars[i].get()
            if not p:
                return
            try:
                cues, enc = load_subtitle(p, self.enc_vars[i].get())
                shown = [romaji_text(c.text) if self.romaji_vars[i].get() and JP_CHARS.search(c.text)
                         else c.text for c in cues]
            except Exception as e:
                messagebox.showerror('Error', f'Could not read {p}:\n{e}')
                return
            self.subs[i] = cues
            lb = self.lists[i]
            lb.delete(0, 'end')
            for c, text in zip(cues, shown):
                lb.insert('end', f'{srt_time(c.start)}  {one_line(text)}')
            self.anchors = [a for a in self.anchors if a[0] < len(self.subs[0]) and a[1] < len(self.subs[1])]
            self.refresh_anchors()
            self.invalidate()
            self.status.set(f'Loaded {len(cues)} lines from {os.path.basename(p)} (encoding: {enc}). '
                            'If letters look wrong, pick another encoding.')

        # -- list helpers
        def search(self, i):
            q = self.search_vars[i].get().lower()
            if not q:
                return
            lb, n = self.lists[i], len(self.subs[i])
            cur = lb.curselection()
            start = cur[0] + 1 if cur else 0
            for k in range(n):
                j = (start + k) % n
                if q in self.subs[i][j].text.lower():
                    self.select(i, j)
                    if i == 0:
                        self.follow_main()
                    return
            self.status.set(f'"{q}" not found.')

        def select(self, i, j):
            lb = self.lists[i]
            lb.selection_clear(0, 'end')
            lb.selection_set(j)
            lb.see(j)

        def follow_main(self, _=None):
            """When a main line is clicked, jump to the second line that currently maps near it."""
            sel = self.lists[0].curselection()
            if not sel or not self.subs[1]:
                return
            t = self.subs[0][sel[0]].start
            tmap = self.build_map()
            j = min(range(len(self.subs[1])), key=lambda k: abs(tmap(self.subs[1][k].start) - t))
            self.select(1, j)

        # -- sync
        def invalidate(self):
            self._auto = None

        def build_map(self):
            a, b = self.subs
            if self.anchors:
                return TimeMap([(b[j].start, a[i].start) for i, j in self.anchors])
            if self.auto_var.get():
                if self._auto is None:
                    self._auto = estimate_sync(a, b)
                off, scale, conf = self._auto
                if conf >= 0.15:
                    return TimeMap(offset=off, scale=scale)
            return TimeMap()

        def add_anchor(self):
            s0, s1 = self.lists[0].curselection(), self.lists[1].curselection()
            if not s0 or not s1:
                messagebox.showinfo('Sync point', 'Select one line in BOTH lists first.')
                return
            self.anchors = [a for a in self.anchors if a[0] != s0[0] and a[1] != s1[0]]
            self.anchors.append((s0[0], s1[0]))
            self.anchors.sort()
            self.refresh_anchors()
            self.check()

        def remove_anchor(self):
            sel = self.anchor_lb.curselection()
            if sel:
                del self.anchors[sel[0]]
                self.refresh_anchors()

        def refresh_anchors(self):
            self.anchor_lb.delete(0, 'end')
            a, b = self.subs
            for i, j in self.anchors:
                d = a[i].start - b[j].start
                self.anchor_lb.insert('end', f'{srt_time(a[i].start)} {one_line(a[i].text)[:40]!r}  <=>  '
                                             f'{srt_time(b[j].start)} {one_line(b[j].text)[:40]!r}   ({fmt_offset(d)})')

        def compute(self):
            if not self.subs[0] or not self.subs[1]:
                messagebox.showinfo('SubMerge', 'Load both subtitle files first.')
                return None, None
            tmap = self.build_map()
            return tmap, merge(self.subs[0], self.subs[1], tmap, self.tol_var.get())

        def check(self):
            tmap, res = self.compute()
            if not res:
                return
            note = ''
            if not self.anchors and self.auto_var.get() and self._auto:
                note = f' (auto confidence {self._auto[2]:.0%})'
                if self._auto[2] < 0.15:
                    note += ' - too low, not applied: add a sync point'
            pct = res.matched / max(1, len(self.subs[1]))
            self.status.set(f'Timing: {tmap.describe()}{note}. Matched {res.matched}/{len(self.subs[1])} '
                            f'second lines ({pct:.0%}); {res.unmatched} kept at their own time. '
                            + ('Looks good.' if pct > 0.8 else 'Low match rate: add sync points.'))

        def save(self):
            tmap, res = self.compute()
            if not res:
                return
            main = self.path_vars[0].get()
            out = filedialog.asksaveasfilename(
                initialdir=os.path.dirname(main),
                initialfile=os.path.splitext(os.path.basename(main))[0] + '.dual.ass',
                defaultextension='.ass',
                filetypes=[('ASS subtitle (recommended)', '*.ass'), ('SRT subtitle', '*.srt')])
            if not out:
                return
            layout = LAYOUTS[self.pos_var.get()]
            colorize = self.color_var.get()
            if colorize and not self.ensure_dictionary():
                return
            opts = dict(second_yellow=self.yellow_var.get(), colorize=colorize,
                        romaji=(self.romaji_vars[0].get(), self.romaji_vars[1].get()))
            try:
                if out.lower().endswith('.srt'):
                    write_srt(out, res, layout, **opts)
                else:
                    write_ass(out, res, layout, font_size=self.size_var.get(), **opts)
            except Exception as e:
                messagebox.showerror('Error', f'Could not save:\n{e}')
                return
            self.check()
            messagebox.showinfo('Saved', f'Saved:\n{out}\n\nIn VLC: Subtitle > Add Subtitle File...\n'
                                'Tip: name it exactly like the video (movie.mkv -> movie.ass) and VLC '
                                'loads it automatically.')

        def ensure_dictionary(self):
            if os.path.exists(DICT_FILE):
                return True
            if not messagebox.askyesno(
                    'Download dictionary?',
                    'Word colouring needs the free JMdict Japanese-English dictionary '
                    '(about 12 MB, from github.com/scriptin/jmdict-simplified).\n\n'
                    'Download it now? This is only needed once.'):
                return False

            def log(msg):
                self.status.set(msg)
                self.update()
            self.config(cursor='watch')
            try:
                download_dictionary(log)
                return True
            except Exception as e:
                messagebox.showerror('Download failed', f'Could not get the dictionary:\n{e}')
                return False
            finally:
                self.config(cursor='')

    App().mainloop()


if __name__ == '__main__':
    if len(sys.argv) > 1:
        run_cli(sys.argv[1:])
    else:
        run_gui()
