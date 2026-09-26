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
import dataclasses
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
    sign: bool = False  # on-screen text (titles, captions) from an .ass file: never paired
    ass: tuple = None   # original .ass fields (layer, style, name, marginL, marginR, marginV, effect, text)


class Cues(list):
    """List of Cue's, plus the header of the .ass file it came from (if any)."""
    play_res = None   # (x, y)
    styles = ()       # original 'Style: ...' lines


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


# Style names typically used for on-screen text rather than dialogue.
SIGN_STYLE_RE = re.compile(r'sign|title|caption|note|name|screen|karaoke|kfx|song|lyric|insert|typeset|^op|^ed',
                           re.I)
# Override tags that move text away from the normal bottom-centre subtitle position.
SIGN_TAG_RE = re.compile(r'\\(?:pos|move)\(|\\an[4-9]|\\a(?:5|6|7|9|10|11)(?!\d)')


def _ass_formatting(text):
    r"""ASS override tags -> <i>/<b> tags; all other tags are dropped. '{\i1}x{\i0}' -> '<i>x</i>'."""
    def block(m):
        out = ''
        for tag, val in re.findall(r'\\([ib])(\d*)(?=\\|\})', m.group(0)):
            out += f'<{tag}>' if val not in ('', '0') else f'</{tag}>'
        return out
    t = re.sub(r'\{[^}]*\}', block, text)
    return t.replace('\\N', '\n').replace('\\n', '\n').replace('\\h', ' ').strip()


def parse_ass(text):
    cues, styles, raw_styles = Cues(), {}, []
    fmt = style_fmt = None
    res = {}
    for line in text.splitlines():
        low = line.lower()
        if low.startswith(('playresx:', 'playresy:')):
            res[low[7]] = int(re.sub(r'\D', '', line) or 0)
        elif line.startswith('Format:') and 'Fontname' in line:
            style_fmt = [f.strip().lower() for f in line[7:].split(',')]
        elif line.startswith('Style:'):
            raw_styles.append(line)
            f = style_fmt or ['name', 'fontname', 'fontsize', 'primarycolour', 'secondarycolour',
                              'outlinecolour', 'backcolour', 'bold', 'italic', 'underline', 'strikeout',
                              'scalex', 'scaley', 'spacing', 'angle', 'borderstyle', 'outline', 'shadow',
                              'alignment', 'marginl', 'marginr', 'marginv', 'encoding']
            d = dict(zip(f, (p.strip() for p in line[6:].split(','))))
            styles[d.get('name', '')] = d
        elif line.startswith('Format:') and 'Text' in line and 'Start' in line:
            fmt = [f.strip().lower() for f in line[7:].split(',')]
        elif line.startswith('Dialogue:'):
            f = fmt or ['layer', 'start', 'end', 'style', 'name', 'marginl', 'marginr',
                        'marginv', 'effect', 'text']
            parts = line[9:].split(',', len(f) - 1)
            if len(parts) < len(f):
                continue
            d = dict(zip(f, parts))
            body = _ass_formatting(d['text'])
            if re.sub(r'<[^>]+>', '', body).strip():
                fields = tuple(d.get(k, '').strip() if k != 'text' else d[k]
                               for k in ('layer', 'style', 'name', 'marginl', 'marginr', 'marginv', 'effect', 'text'))
                cues.append(Cue(parse_time(d['start']), parse_time(d['end']), body, ass=fields))

    # The most used style is the dialogue style; on-screen text is anything positioned
    # elsewhere, or in another style that is named / aligned like a sign.
    counts = {}
    for c in cues:
        counts[c.ass[1]] = counts.get(c.ass[1], 0) + 1
    dialogue_style = max(counts, key=counts.get) if counts else ''
    for c in cues:
        style = c.ass[1]
        st = styles.get(style, {})
        if SIGN_TAG_RE.search(c.ass[7]):
            c.sign = True
        elif style != dialogue_style and (SIGN_STYLE_RE.search(style)
                                          or st.get('alignment', '2') not in ('1', '2', '3')):
            c.sign = True
        elif st.get('italic') == '-1' and '<i>' not in c.text:
            c.text = f'<i>{c.text}</i>'  # the style itself is italic
    if 'x' in res or 'y' in res:  # same defaults as libass when only one is given
        cues.play_res = (res.get('x') or res['y'] * 4 // 3, res.get('y') or res['x'] * 3 // 4)
    else:
        cues.play_res = (384, 288)
    cues.styles = tuple(raw_styles)
    return cues


# Speaker names and sound descriptions (hearing-impaired subtitles):
#   （テンマ）はい -> はい   （ノック） -> (removed)   TENMA: Yes -> Yes   [door opens] -> (removed)
SPEAKER_RES = (
    re.compile(r'^(\s*[-‐]?\s*)[（(][^（()）]{1,25}[)）]\s*'),       # (Name) / (sound) at line start
    re.compile(r'^(\s*[-‐]?\s*)[A-Z][A-Z0-9 .\'-]{1,24}:\s+'),        # NAME: at line start
    re.compile(r'()[\[［][^\[\]［］]{1,40}[\]］]'),                       # [sound] anywhere
)


def remove_speakers(text):
    lines = []
    for line in text.split('\n'):
        for rx in SPEAKER_RES:
            prev = None
            while prev != line:  # repeat: "（テンマ）（笑）はい"
                prev, line = line, rx.sub(lambda m: m.group(1), line, count=1)
        line = re.sub(r'  +', ' ', line).strip()
        if re.sub(r'<[^>]+>|[-‐\s]', '', line):
            lines.append(line)
    return '\n'.join(lines)


def load_subtitle(path, encoding='auto', no_speakers=False):
    text, used = read_text(path, encoding)
    if path.lower().endswith(('.ass', '.ssa')):
        cues = parse_ass(text)
    else:
        cues = Cues(parse_srt_vtt(text))
    if no_speakers:
        for c in cues:
            if not c.sign:
                c.text = remove_speakers(c.text)
        cues[:] = [c for c in cues if c.sign or c.text]
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


KANJI = re.compile(r'[㐀-鿿々]')
HINT_RE = re.compile(r'(?<=[㐀-鿿々A-Za-z.])\([ぁ-んァ-ヶー]+\)')


@dataclass(frozen=True)
class JpWord:
    orig: str
    romaji: str
    keys: tuple = ()     # dictionary lookup keys, best first; empty = not a content word
    english: str = ''    # English origin of loanwords (コーヒー -> coffee), from the tokenizer
    kana: str = ''       # reading in hiragana
    glue: bool = False   # romaji continues into the next word without a space (だっ + た -> datta)


def _apply_hints(words):
    """Reading hints: 弛緩(しかん) -> one word '弛緩' read 'shikan'; Dr.(ドクター) -> 'Dr.'."""
    out, i = [], 0
    while i < len(words):
        w = words[i]
        if w.orig == '(' and out:
            j = i + 1
            while j < len(words) and words[j].orig != ')' and j - i <= 8:
                j += 1
            hint = ''.join(x.orig for x in words[i + 1:j])
            prev = out[-1].orig[-1:]
            if j < len(words) and words[j].orig == ')' and re.fullmatch(r'[ぁ-んァ-ヶー]+', hint) \
                    and (KANJI.match(prev) or re.match(r'[A-Za-z.]', prev)):
                if KANJI.match(prev):
                    # The hint covers the last few kanji words: take words from the end until
                    # their readings are as long as the hint.
                    k, covered = len(out), 0
                    while k > 0 and KANJI.search(out[k - 1].orig) and covered < len(hint):
                        k -= 1
                        covered += len(out[k].kana or out[k].orig)
                    group = out[k:]
                    orig = ''.join(x.orig for x in group)
                    out[k:] = [JpWord(orig, _kana_to_romaji(hint), tuple(dict.fromkeys((orig,) + group[-1].keys)),
                                      group[-1].english, _hiragana(hint), group[-1].glue)]
                i = j + 1
                continue
        out.append(w)
        i += 1
    return out


def _kana_to_romaji(text):
    return ''.join(x['hepburn'] for x in _kakasi.convert(text)).strip()


def _hiragana(text):
    return ''.join(chr(ord(c) - 0x60) if 'ァ' <= c <= 'ヶ' else c for c in text)


def clean_japanese(line):
    return unicodedata.normalize('NFKC', re.sub(r'<[^>]+>|\{[^}]*\}', '', line))


@functools.lru_cache(maxsize=8192)
def japanese_words(line, hints=False):
    """Split one line into JpWord's. Uses fugashi (real word splitting and grammar,
    if installed) and falls back to pykakasi's rougher splitting.
    hints=True: remove reading hints like 弛緩(しかん) and use them as the reading."""
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
        if hints:
            line = HINT_RE.sub('', line)
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
            kana_base = _hiragana(getattr(f, 'kanaBase', None) or '')
            keys = tuple(dict.fromkeys(k for k in (base, lemma, s, kana_base) if k and k != '*'))
            english = origin if origin.isascii() and origin.isalpha() else ''
        words.append(JpWord(s, r, keys, english, _hiragana(getattr(f, 'kana', None) or '')))
    # A word ending in small っ doubles the next consonant: だっ + た -> "dat" + "ta" = "datta",
    # and at the end of a sentence it is silent: あっ -> "a" (not "atsu").
    for i, w in enumerate(words):
        if w.kana.endswith('っ') and w.romaji.endswith('tsu'):
            nxt = words[i + 1].romaji if i + 1 < len(words) and JP_CHARS.search(words[i + 1].orig) else ''
            if re.match(r'[bcdfghjkmpqrstvwxyz]', nxt):
                words[i] = dataclasses.replace(w, romaji=w.romaji[:-3] + ('t' if nxt.startswith('ch') else nxt[0]),
                                               glue=True)
            else:
                words[i] = dataclasses.replace(w, romaji=w.romaji[:-3])
    return tuple(_apply_hints(words) if hints else words)


def cue_words(text, hints=False):
    """JpWord's of every line of a cue, as a list of lines."""
    return [japanese_words(clean_japanese(line), hints) for line in text.split('\n')]


def paint(s, color, fmt, base):
    if not color or fmt == 'plain':
        return s
    if fmt == 'ass':
        return '{\\c&H%s&}%s{\\c&H%s&}' % (bgr(color), s, bgr(base))
    return f'<font color="#{color}">{s}</font>'


def bgr(rgb):
    return rgb[4:6] + rgb[2:4] + rgb[0:2]


def render_japanese(text, romaji, colors=None, fmt='plain', base='FFFFFF', hints=False):
    """Japanese text (optionally as romaji), with colours: {word number: 'RRGGBB'}."""
    colors = colors or {}
    out_lines, n = [], 0
    for words in cue_words(text, hints):
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
                glue = w.glue
        out_lines.append(out.strip())
    return '\n'.join(out_lines)


def romaji_text(text, hints=False):
    return render_japanese(text, True, hints=hints)


# -------------------------------------------------------- word colouring ---

# English words never used for matching (grammar words that appear in countless glosses).
STOP = set('''a an the to of in on at for from by with as into onto about be is are was were been
being am do does did done have has had it its it's this that these those there and or but if so
not one one's oneself someone something somebody somewhere sth sb etc esp eg ie oneself's
let let's get got don't doesn't didn't can't cannot won't wouldn't isn't aren't wasn't weren't
haven't hasn't hadn't couldn't shouldn't mustn't'''.split())
STOP |= {"there's", "here's", "that's"}
NEGATIONS = set("not no never nothing nobody none cannot can't don't doesn't won't isn't aren't without".split())
# English pronouns are only matched to the Japanese pronouns below, because many dictionary
# phrases contain them ("I see", "excuse me") and would colour them wrongly.
_FIRST = "i me my mine myself i'm i've i'll i'd we us our ours ourselves we're we've we'll we'd"
_SECOND = "you your yours yourself yourselves you're you've you'll you'd"
_HE = "he him his himself he's he'll he'd they them their theirs themselves they're they've they'll"
_SHE = "she her hers herself she's she'll she'd"
PRONOUN_MAP = {}
for _words, _en in ((('私', 'わたし', 'わたくし', 'あたし', '僕', 'ぼく', '俺', 'おれ', '我', '我々', 'われわれ'), _FIRST),
                    (('あなた', '貴方', '君', 'きみ', 'お前', 'おまえ', 'あんた', '貴様', 'てめえ'), _SECOND),
                    (('彼', 'かれ'), _HE), (('彼女', 'かのじょ'), _SHE)):
    PRONOUN_MAP.update(dict.fromkeys(_words, frozenset(_en.split())))
PRONOUNS = frozenset(_FIRST.split() + _SECOND.split() + _HE.split() + _SHE.split())
# Only allowed as the continuation of a phrase ("calm" + "down"), never as a match on their own.
WEAK = set('up down out off away back over around along through'.split())
PALETTE = ('66D9FF', '8CFF66', 'FF9F40', 'FF80D5', 'B38CFF', '4DFFC3', 'FF6B6B')
EN_WORD = re.compile(r"<[^>]*>|\{[^}]*\}|([A-Za-z]+(?:'[A-Za-z]+)*)")
DICT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'jmdict_index_v3.json.gz')
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
    def meanings(senses):
        kws = set()
        for sense in senses[:3]:  # the main meanings only: rare ones cause wrong matches
            for g in sense['gloss']:
                t = re.sub(r'\([^)]*\)', ' ', g['text'].lower())
                words = re.findall(r"[a-z]+(?:'[a-z]+)*", t)
                # Skip negated phrases ("not enough" is not "enough") and hyphenated
                # compounds ("you-know-what"); a plain "no" stays.
                if '-' in t or (len(words) > 1 and NEGATIONS & set(words)):
                    continue
                kws.update(stem(w) for w in words if w not in STOP)
        return kws

    index = {}
    for entry in data['words']:
        for k in entry['kanji']:
            index.setdefault(k['text'], set()).update(meanings(entry['sense']))
        # A kana spelling (はい) is shared by many words (灰 ash, 肺 lung...): only link it to
        # words that are really written in kana, or to their kana-usage meanings.
        if entry['kanji']:
            kana_senses = [s for s in entry['sense'] if 'uk' in s.get('misc', [])]
        else:
            kana_senses = entry['sense']
        kws = meanings(kana_senses)
        if kws:
            for k in entry['kana']:
                index.setdefault(k['text'], set()).update(kws)
    index = {k: v for k, v in index.items() if v}
    del data
    with gzip.open(DICT_FILE, 'wt', encoding='utf-8') as f:
        json.dump({k: ' '.join(sorted(v)) for k, v in index.items()}, f, ensure_ascii=False)
    log(f'Dictionary ready: {len(index)} words.')


def align(ja_text, en_text, dictionary, hints=False):
    """Colours for matched words: ({japanese word number: color}, {english word number: color})."""
    ja_colors, en_colors = {}, {}
    for n, (i, span) in enumerate(match_words(ja_text, en_text, dictionary, hints)):
        ja_colors[i] = PALETTE[n % len(PALETTE)]
        en_colors.update(dict.fromkeys(span, ja_colors[i]))
    return ja_colors, en_colors


def match_words(ja_text, en_text, dictionary, hints=False):
    """Match Japanese words to English words that mean the same thing.
    Returns [(japanese word number, [english word numbers]), ...]."""
    ja = [w for line in cue_words(ja_text, hints) for w in line]
    en = [w.lower() for w in english_words(en_text)]
    en_stems = [None if w in STOP else stem(w) for w in en]
    used, pairs = set(), []
    for i, w in enumerate(ja):
        if not w.keys:
            continue
        pronouns = next((PRONOUN_MAP[k] for k in w.keys if k in PRONOUN_MAP), None)
        if pronouns:  # 私 -> I/me/my..., 君 -> you/your...
            hits = {j for j, word in enumerate(en) if word in pronouns and j not in used}
        else:
            meanings = dictionary.meanings(w)
            hits = {j for j, s in enumerate(en_stems)
                    if s and s in meanings and j not in used and en[j] not in PRONOUNS}
        starts = sorted(j for j in hits if en[j] not in WEAK or pronouns)
        if not starts:
            continue
        span = [starts[0]]
        while span[-1] + 1 in hits:  # "calm" + "down"
            span.append(span[-1] + 1)
        pairs.append((i, span))
        used.update(span)
    return pairs


def render_event(role, text, fmt, romaji=(False, False), dictionary=None, bases=('FFFFFF', 'FFFFFF'),
                 hints=False):
    """Returns (main, second) display texts for one event (None where absent)."""
    sides = list(text) if role == 'pair' else ([text, None] if role == 'main' else [None, text])
    colors = [{}, {}]
    if role == 'pair' and dictionary:
        is_ja = [bool(JP_CHARS.search(s)) for s in sides]
        if is_ja[0] != is_ja[1]:  # exactly one Japanese side
            j = 0 if is_ja[0] else 1
            colors[j], colors[1 - j] = align(sides[j], sides[1 - j], dictionary, hints)
    out = []
    for k, s in enumerate(sides):
        if s is None:
            out.append(None)
        elif (romaji[k] or colors[k] or hints) and JP_CHARS.search(s):
            out.append(render_japanese(s, romaji[k], colors[k], fmt, bases[k], hints))
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
    first = [c for c in first if not c.sign]
    second = [c for c in second if not c.sign]
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
    # (start, end, role, data): role 'main' (text), 'pair' ((main text, second text)),
    # or 'sign' (the Cue of on-screen text from the main subtitle, written unchanged)
    events: list
    matched: int              # second-subtitle lines shown
    unmatched: int            # second-subtitle lines left out (no main line at that time)
    main_without_second: int  # main lines shown without a second line
    play_res: tuple = None    # screen size and styles of the main subtitle, if it is an .ass file
    styles: tuple = ()


def merge(first, second, tmap, tolerance=1000):
    """Put each second-subtitle line under the main line(s) it belongs to. A second line that
    covers several main lines is shown under each of them; one that matches nothing is left out."""
    dialog = [c for c in first if not c.sign]
    moved = [Cue(max(0, tmap(c.start)), max(0, tmap(c.end)), c.text) for c in second if not c.sign]
    starts1 = [c.start for c in dialog]
    groups = {}
    unmatched = 0
    for c in moved:
        lo = bisect.bisect_left(starts1, c.start - 15000)
        hi = bisect.bisect_right(starts1, c.end + tolerance)
        covers, best, nearest = [], None, None
        for i in range(lo, hi):
            f = dialog[i]
            ov = min(f.end, c.end) - max(f.start, c.start)
            f_len, c_len = max(1, f.end - f.start), max(1, c.end - c.start)
            if ov >= 0.5 * f_len:       # this line covers most of main line i
                covers.append(i)
            share = ov / min(f_len, c_len)
            if share >= 0.5 and (best is None or share > best[0]):
                best = (share, i)       # mostly overlapping (the shorter of the two)
            d = abs(f.start - c.start)
            if d <= tolerance and (nearest is None or d < nearest[0]):
                nearest = (d, i)        # starts at about the same time
        targets = covers or ([best[1]] if best else []) or ([nearest[1]] if nearest else [])
        for i in targets:
            groups.setdefault(i, []).append(c)
        unmatched += not targets

    events = []
    for i, f in enumerate(dialog):
        if i in groups:
            text = '\n'.join(c.text for c in sorted(groups[i], key=lambda c: c.start))
            events.append((f.start, f.end, 'pair', (f.text, text)))
        else:
            events.append((f.start, f.end, 'main', f.text))
    events += [(c.start, c.end, 'sign', c) for c in first if c.sign]
    events.sort(key=lambda e: e[0])
    return MergeResult(events, len(moved) - unmatched, unmatched, len(dialog) - len(groups),
                       getattr(first, 'play_res', None), getattr(first, 'styles', ()))


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


def rendered_events(result, fmt, romaji, colorize, second_yellow, hints=False):
    """Yield (start, end, main_text or None, second_text or None, sign Cue or None) ready for output."""
    bases = ('FFFFFF', 'FFFF99' if second_yellow else 'FFFFFF')
    dictionary = get_dictionary() if colorize else None
    for start, end, role, data in result.events:
        if role == 'sign':
            yield start, end, None, None, data
            continue
        main, second = render_event(role, data, fmt, romaji, dictionary, bases, hints)
        yield start, end, main, second, None


# --------------------------------------------------------------- hint mode ---
# Only the main subtitle is shown; words that have a known translation get it written
# above them in small text. ASS has no "text above a word" feature, so we measure the
# words ourselves, wrap the lines ourselves and place every line and hint at exact positions.

HINT_MODES = {'Off': None, 'Romaji': 'romaji', 'Japanese': 'japanese', 'Japanese + romaji': 'both'}
HINT_SCALE = 0.55   # hint size relative to the subtitle size
_tk_root = None


def text_measurer(font, size):
    """Returns measure(text, italic=False) -> width in script pixels for an ASS font size.
    Uses Tk's font engine (the same Windows fonts VLC uses); an ASS font size is the
    font's line height, which is Tk's 'linespace'."""
    global _tk_root
    try:
        import tkinter
        import tkinter.font
        root = tkinter._default_root
        if root is None:
            if _tk_root is None:
                _tk_root = tkinter.Tk()
                _tk_root.withdraw()
            root = _tk_root
        fonts = {False: tkinter.font.Font(root=root, family=font, size=-200),
                 True: tkinter.font.Font(root=root, family=font, size=-200, slant='italic')}
        scale = size / fonts[False].metrics('linespace')
        return lambda text, italic=False: fonts[italic].measure(text) * scale
    except Exception:  # no Tk: rough estimate
        return lambda text, italic=False: sum(size * (1.0 if JP_CHARS.match(c) else 0.5) for c in text)


def _layout_rows(text, measure, max_width):
    """Split main text into screen rows. Returns rows of tokens:
    (x, width, text, italic, [(english word number, x0, x1), ...]) with x relative to the row start."""
    rows, italic, n_word = [], False, 0
    space = measure(' ')
    for source_line in text.split('\n'):
        tokens = []  # (text, italic) per space-separated word; italic = state at its first letter
        for raw in source_line.split(' '):
            lead = re.match(r'(?:<[^>]*>)*', raw).group(0)
            for tag in re.findall(r'<(/?)i>', lead, re.I):
                italic = not tag
            it = italic
            for tag in re.findall(r'<(/?)i>', raw[len(lead):], re.I):
                italic = not tag
            word = re.sub(r'<[^>]*>', '', raw)
            if word:
                tokens.append((word, it))
        row, x = [], 0
        for word, it in tokens:
            w = measure(word, it)
            spans = []
            for m in EN_WORD.finditer(word):
                if m.group(1):
                    spans.append((n_word, measure(word[:m.start()], it), measure(word[:m.end()], it)))
                    n_word += 1
            if row and x + space + w > max_width:
                rows.append(row)
                row, x = [], 0
            if row:
                x += space
            row.append((x, w, word, it, spans))
            x += w
        if row:
            rows.append(row)
    return rows


def hint_dialogues(main, second, t0, t1, dictionary, mode, reading_hints, geo):
    """Dialogue lines for one main line with hints above the words that have a translation,
    or None when nothing matched (the caller then writes the line normally)."""
    if not dictionary or JP_CHARS.search(main) or not JP_CHARS.search(second):
        return None
    pairs = match_words(second, main, dictionary, reading_hints)
    if not pairs:
        return None
    ja = [w for line in cue_words(second, reading_hints) for w in line]

    def word_text(i, romaji):
        w, text = ja[i], ''
        while True:  # 逆らっ + て -> 逆らって / sakaratte
            text += w.romaji.strip() if romaji else w.orig
            if not w.glue or i + 1 >= len(ja):
                break
            i += 1
            w = ja[i]
        return re.sub(r'(konnichi|konban)ha$', r'\1wa', text) if romaji else text

    hint_of = {}  # english word number -> hint, for the first word of each matched phrase
    for i, js in pairs:
        parts = [word_text(i, False)] * (mode in ('japanese', 'both')) + [word_text(i, True)] * (mode in ('romaji', 'both'))
        hint_of[js[0]] = (js, parts)

    measure, fs, hs = geo['measure'], geo['font_size'], geo['hint_size']
    rows = _layout_rows(main, measure, geo['res_x'] - 2 * geo['margin'])
    hint_lines = 2 if mode == 'both' else 1
    out, y = [], geo['res_y'] - geo['margin_v']
    for row in reversed(rows):
        width = row[-1][0] + row[-1][1]
        left = (geo['res_x'] - width) / 2
        words = {}  # english word number -> (x0, x1) on screen
        for x, w, word, it, spans in row:
            for j, x0, x1 in spans:
                words[j] = (left + x + x0, left + x + x1)
        # Main text of this row, rebuilt with italics, centred exactly where we measured it.
        pieces, italic = [], False
        for x, w, word, it, spans in row:
            pieces.append(('' if it == italic else '{\\i1}' if it else '{\\i0}') + word)
            italic = it
        body = ' '.join(pieces)
        out.append(f'Dialogue: 0,{t0},{t1},SubMerge-Main,,0,0,0,,{{\\an2\\pos({geo["res_x"] / 2:.0f},{y:.0f})\\q2}}{body}')
        # Hints of this row, centred over their words, pushed apart if they would touch.
        placed = []
        for j, (js, parts) in sorted(hint_of.items()):
            if j in words:
                x0 = words[j][0]
                x1 = max(words[k][1] for k in js if k in words)
                hint_w = max(measure(p) for p in parts) * hs / fs
                placed.append([(x0 + x1) / 2 - hint_w / 2, hint_w, parts])
        for a, b in zip(placed, placed[1:]):
            b[0] = max(b[0], a[0] + a[1] + hs * 0.4)
        for x, hint_w, parts in placed:
            cx = min(max(x + hint_w / 2, hint_w / 2), geo['res_x'] - hint_w / 2)
            out.append(f'Dialogue: 1,{t0},{t1},SubMerge-Hint,,0,0,0,,'
                       f'{{\\an2\\pos({cx:.0f},{y - fs * 0.92:.0f})}}' + '\\N'.join(parts))
        y -= fs + (hint_lines * hs if placed else 0)
    return out


def write_ass(path, result, layout='main_first', font='Arial', font_size=60, second_yellow=True,
              romaji=(False, False), colorize=False, reading_hints=False, hint_mode=None):
    """hint_mode 'romaji' / 'japanese' / 'both': show only the main subtitle, with the
    translation of matched words in small yellow text above them."""
    main_align, second_align = {'split': (8, 2), 'split_rev': (2, 8)}.get(layout, (2, 2))
    second_color = '&H0099FFFF' if second_yellow else '&H00FFFFFF'
    # Keep the main subtitle's screen size so its on-screen texts stay where they were;
    # our own sizes are given for 1080 lines and scaled to it.
    res_x, res_y = result.play_res or (1920, 1080)
    k = res_y / 1080
    style = (f'{{name}},{font},{round(font_size * k)},{{color}},&H000000FF,&H00000000,&H80000000,'
             f'0,0,0,0,100,100,0,0,1,{round(3 * k, 2)},{round(1 * k, 2)},{{align}},'
             f'{round(60 * k)},{round(60 * k)},{round(45 * k)},1')
    lines = [
        '[Script Info]', '; Created by SubMerge', 'ScriptType: v4.00+',
        f'PlayResX: {res_x}', f'PlayResY: {res_y}', 'WrapStyle: 0', 'ScaledBorderAndShadow: yes', '',
        '[V4+ Styles]',
        'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, '
        'BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, '
        'BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
        'Style: ' + style.format(name='SubMerge-Main', color='&H00FFFFFF', align=main_align),
        'Style: ' + style.format(name='SubMerge-Second', color=second_color, align=second_align),
        *result.styles,  # the main subtitle's own styles, used by its on-screen texts
        '', '[Events]',
        'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text',
    ]
    if hint_mode:
        fs = round(font_size * k)
        hs = round(fs * HINT_SCALE)
        lines.insert(lines.index('[Events]') - 1, 'Style: ' + style.format(
            name='SubMerge-Hint', color='&H0099FFFF', align=2).replace(f',{fs},', f',{hs},', 1))
        geo = dict(res_x=res_x, res_y=res_y, font_size=fs, hint_size=hs, margin=round(60 * k),
                   margin_v=round(45 * k), measure=text_measurer(font, fs))
        dictionary = get_dictionary()
        for start, end, role, data in result.events:
            t0, t1 = ass_time(start), ass_time(end)
            if role == 'sign':
                layer, st, name, ml, mr, mv, effect, text = data.ass
                lines.append(f'Dialogue: {layer},{t0},{t1},{st},{name},{ml},{mr},{mv},{effect},{text}')
                continue
            main = POS_TAG_RE.sub('', data[0] if role == 'pair' else data).strip()
            hinted = role == 'pair' and hint_dialogues(main, data[1], t0, t1, dictionary, hint_mode,
                                                       reading_hints, geo)
            lines += hinted or [f'Dialogue: 0,{t0},{t1},SubMerge-Main,,0,0,0,,{to_ass_text(main)}']
        with open(path, 'w', encoding='utf-8-sig', newline='\r\n') as f:
            f.write('\n'.join(lines) + '\n')
        return
    stacked = layout in ('main_first', 'second_first')
    for start, end, main, second, sign in rendered_events(result, 'ass', romaji, colorize, second_yellow,
                                                          reading_hints):
        t0, t1 = ass_time(start), ass_time(end)
        if sign:
            layer, st, name, ml, mr, mv, effect, text = sign.ass
            lines.append(f'Dialogue: {layer},{t0},{t1},{st},{name},{ml},{mr},{mv},{effect},{text}')
            continue
        blocks = []
        if main is not None and second is not None and stacked:
            # One event, two blocks: {\r<Style>} switches colour/font for the lower block.
            main, second = to_ass_text(main), to_ass_text(second)
            if layout == 'main_first':
                blocks.append(('SubMerge-Main', f'{main}\\N{{\\rSubMerge-Second}}{second}'))
            else:
                blocks.append(('SubMerge-Second', f'{second}\\N{{\\rSubMerge-Main}}{main}'))
        else:
            if main is not None:
                blocks.append(('SubMerge-Main', to_ass_text(main)))
            if second is not None:
                blocks.append(('SubMerge-Second', to_ass_text(second)))
        for style_name, body in blocks:
            lines.append(f'Dialogue: 0,{t0},{t1},{style_name},,0,0,0,,{body}')
    with open(path, 'w', encoding='utf-8-sig', newline='\r\n') as f:
        f.write('\n'.join(lines) + '\n')


def write_srt(path, result, layout='main_first', second_yellow=True, romaji=(False, False), colorize=False,
              reading_hints=False):
    def clean(t, is_second):
        t = POS_TAG_RE.sub('', t).strip()
        return f'<font color="#FFFF99">{t}</font>' if is_second and second_yellow else t

    stacked = layout in ('main_first', 'second_first')
    cues = []
    for start, end, main, second, sign in rendered_events(result, 'srt', romaji, colorize, second_yellow,
                                                          reading_hints):
        if sign:  # on-screen text: SRT has no positioning, show it at the top
            cues.append((start, end, '{\\an8}' + sign.text))
            continue
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
    p.add_argument('--no-speakers', action='store_true',
                   help='remove speaker names and sound descriptions: （テンマ）はい -> はい, （ノック） -> removed')
    p.add_argument('--reading-hints', action='store_true',
                   help='remove Japanese reading hints: 弛緩(しかん) -> 弛緩 (romaji uses the hint: shikan)')
    p.add_argument('--hint-mode', choices=['romaji', 'japanese', 'both'],
                   help='show only the main subtitle, with the Japanese of matched words in small text '
                        'above them (.ass output only)')
    a = p.parse_args(argv)
    if a.hint_mode and a.output and a.output.lower().endswith('.srt'):
        p.error('hint mode only works with .ass output (.srt cannot place text above words)')

    first, e1 = load_subtitle(a.main, a.enc1, a.no_speakers)
    second, e2 = load_subtitle(a.second, a.enc2, a.no_speakers)
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
    if (a.color or a.hint_mode) and not os.path.exists(DICT_FILE):
        download_dictionary()
    opts = dict(second_yellow=not a.white, romaji=(a.romaji1, a.romaji2), colorize=a.color,
                reading_hints=a.reading_hints)
    if out.lower().endswith('.srt'):
        write_srt(out, res, a.layout, **opts)
    else:
        write_ass(out, res, a.layout, font_size=a.font_size, hint_mode=a.hint_mode, **opts)
    print(f'Second subtitle: {res.matched} lines used, {res.unmatched} left out (no main line at that time); '
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
            self.hint_var = tk.StringVar(value='Off')
            ttk.Label(of2, text='   Hint mode:').pack(side='left')
            hint_cb = ttk.Combobox(of2, textvariable=self.hint_var, values=list(HINT_MODES), width=18,
                                   state='readonly')
            hint_cb.pack(side='left')
            hint_cb.bind('<<ComboboxSelected>>', self.hint_mode_selected)
            self.speakers_var = tk.BooleanVar(value=False)
            self.hints_var = tk.BooleanVar(value=False)
            reload_both = lambda: (self.load(0), self.load(1))  # noqa: E731
            ttk.Checkbutton(self, variable=self.speakers_var, command=reload_both,
                            text='Remove speaker names and sound descriptions (both subtitles), e.g. '
                                 '"（テンマ）はい" -> "はい", "(TENMA) Yes" -> "Yes", "（ノック）" / "[knocking]" '
                                 '-> line removed').pack(anchor='w', padx=8, pady=(2, 0))
            ttk.Checkbutton(self, variable=self.hints_var, command=reload_both,
                            text='Remove Japanese reading hints in brackets, e.g. "筋肉弛緩(しかん)剤" -> '
                                 '"筋肉弛緩剤", "Dr.(ドクター)テンマ" -> "Dr.テンマ". With romaji, the hint is '
                                 'used as the reading: "kinniku shikan zai"').pack(anchor='w', padx=8, pady=(2, 0))

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
                cues, enc = load_subtitle(p, self.enc_vars[i].get(), self.speakers_var.get())
                romaji, hints = self.romaji_vars[i].get(), self.hints_var.get()
                shown = [render_japanese(c.text, romaji, hints=hints)
                         if (romaji or hints) and not c.sign and JP_CHARS.search(c.text) else c.text
                         for c in cues]
            except Exception as e:
                messagebox.showerror('Error', f'Could not read {p}:\n{e}')
                return
            if len(cues) != len(self.subs[i]):  # different lines: sync points no longer fit
                self.anchors = []
            self.subs[i] = cues
            lb = self.lists[i]
            lb.delete(0, 'end')
            for c, text in zip(cues, shown):
                prefix = '[on-screen text, not paired] ' if c.sign else ''
                lb.insert('end', f'{srt_time(c.start)}  {prefix}{one_line(text)}')
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
            if self.subs[0][s0[0]].sign or self.subs[1][s1[0]].sign:
                messagebox.showinfo('Sync point', 'Please pick spoken lines, not on-screen text.')
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
            total = res.matched + res.unmatched
            pct = res.matched / max(1, total)
            self.status.set(f'Timing: {tmap.describe()}{note}. Matched {res.matched}/{total} second lines '
                            f'({pct:.0%}); {res.unmatched} have no main line at that time and are left out. '
                            + ('Looks good.' if pct > 0.8 else 'Low match rate: check the sync or add sync points.'))

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
            hint_mode = HINT_MODES[self.hint_var.get()]
            if hint_mode and out.lower().endswith('.srt'):
                if not messagebox.askyesno('Hint mode needs .ass',
                                           'Hint mode only works with .ass files (.srt cannot place text '
                                           'above words).\n\nSave as .ass instead?'):
                    return
                out = out[:-4] + '.ass'
            layout = LAYOUTS[self.pos_var.get()]
            colorize = self.color_var.get()
            if (colorize or hint_mode) and not self.ensure_dictionary():
                return
            opts = dict(second_yellow=self.yellow_var.get(), colorize=colorize, reading_hints=self.hints_var.get(),
                        romaji=(self.romaji_vars[0].get(), self.romaji_vars[1].get()))
            try:
                if out.lower().endswith('.srt'):
                    write_srt(out, res, layout, **opts)
                else:
                    write_ass(out, res, layout, font_size=self.size_var.get(), hint_mode=hint_mode, **opts)
            except Exception as e:
                messagebox.showerror('Error', f'Could not save:\n{e}')
                return
            self.check()
            messagebox.showinfo('Saved', f'Saved:\n{out}\n\nIn VLC: Subtitle > Add Subtitle File...\n'
                                'Tip: name it exactly like the video (movie.mkv -> movie.ass) and VLC '
                                'loads it automatically.')

        def hint_mode_selected(self, _=None):
            if HINT_MODES[self.hint_var.get()]:
                messagebox.showinfo(
                    'Hint mode',
                    'Hint mode shows only the main (English) subtitle. Words whose Japanese translation '
                    'is known get it in small yellow text right above them; the second subtitle itself '
                    'is not shown.\n\n'
                    'Note: hint mode only works with .ass output. .srt files cannot place text above '
                    'words, so save as .ass.\n\n'
                    'The layout, colour and "second subtitle in yellow" settings are not used in hint mode.')

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
