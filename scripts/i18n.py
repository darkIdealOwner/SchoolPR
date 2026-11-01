"""Переводы интерфейса без GNU gettext.

    python scripts/i18n.py extract   # собрать фразы из шаблонов и кода в locale/<язык>/LC_MESSAGES/django.po
    python scripts/i18n.py compile   # собрать .po в .mo (их читает Django)
    python scripts/i18n.py status    # сколько фраз ещё не переведено

Исходный язык — русский: фразы в коде пишутся по-русски и служат ключами (msgid).
Уже сделанные переводы при повторном extract сохраняются; фразы, которых больше нет в коде, удаляются.
Стандартный `manage.py makemessages` тоже подойдёт, если установлен GNU gettext.

locale/<язык>/LC_MESSAGES/overrides.po — ручные дополнения к встроенным переводам Django
(например, в узбекском каталоге Django нет подписей формы смены пароля). extract этот файл не трогает,
compile добавляет его в .mo.
"""
import ast
import re
import struct
import sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCALE_DIR = ROOT / 'locale'
LANGUAGES = {
    'uz': 'nplurals=2; plural=(n != 1);',
    'en': 'nplurals=2; plural=(n != 1);',
    'ko': 'nplurals=1; plural=0;',
}
TEMPLATE_DIRS = [ROOT / 'pages']
PYTHON_DIRS = [ROOT / 'users', ROOT / 'integrations', ROOT / 'config']
GETTEXT_NAMES = {'_', 'gettext', 'gettext_lazy', 'pgettext', 'ngettext'}

TRANSLATE_RE = re.compile(r'{%\s*(?:translate|trans)\s+(["\'])(.*?)\1(?:\s+as\s+\w+)?\s*%}', re.S)
DEFAULT_RE = re.compile(r'\b_\((["\'])(.*?)\1\)')
BLOCK_RE = re.compile(r'{%\s*(?:blocktranslate|blocktrans)\b([^%]*)%}(.*?){%\s*(?:endblocktranslate|endblocktrans)\s*%}', re.S)
VAR_RE = re.compile(r'{{\s*(\w+)\s*}}')


def extract_templates(messages):
    for path in sorted(p for d in TEMPLATE_DIRS for p in d.rglob('*.html')):
        text = path.read_text(encoding='utf-8')
        ref = path.relative_to(ROOT).as_posix()
        # В шаблонах Django ищет перевод с удвоенным «%» (как makemessages) — храним так же
        for m in TRANSLATE_RE.finditer(text):
            messages.setdefault(m.group(2).replace('%', '%%'), set()).add(ref)
        for m in DEFAULT_RE.finditer(text):
            messages.setdefault(m.group(2).replace('%', '%%'), set()).add(ref)
        for m in BLOCK_RE.finditer(text):
            body = m.group(2)
            if 'trimmed' in m.group(1):
                body = ' '.join(body.split())
            msgid = VAR_RE.sub(r'%(\1)s', body.replace('%', '%%'))
            messages.setdefault(msgid, set()).add(ref)


def extract_python(messages):
    for path in sorted(p for d in PYTHON_DIRS for p in d.rglob('*.py')):
        if 'migrations' in path.parts or path.name.startswith('test'):
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'))
        ref = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in GETTEXT_NAMES
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                messages.setdefault(node.args[0].value, set()).add(ref)


def po_escape(s):
    return s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\t', '\\t')


def po_unescape(s):
    return re.sub(r'\\(.)', lambda m: {'n': '\n', 't': '\t'}.get(m.group(1), m.group(1)), s)


def read_po(path):
    """{msgid: msgstr} из .po (только одиночные сообщения — других мы не создаём)."""
    entries = OrderedDict()
    if not path.exists():
        return entries
    msgid = msgstr = None
    target = None
    for line in path.read_text(encoding='utf-8').splitlines() + ['']:
        line = line.strip()
        if line.startswith('msgid '):
            if msgid is not None:
                entries[msgid] = msgstr or ''
            msgid, msgstr, target = po_unescape(line[7:-1]), '', 'id'
        elif line.startswith('msgstr '):
            msgstr, target = po_unescape(line[8:-1]), 'str'
        elif line.startswith('"') and target:
            if target == 'id':
                msgid += po_unescape(line[1:-1])
            else:
                msgstr += po_unescape(line[1:-1])
        elif not line and msgid is not None:
            entries[msgid] = msgstr or ''
            msgid = msgstr = target = None
    entries.pop('', None)
    return entries


def write_po(path, lang, messages, translations):
    path.parent.mkdir(parents=True, exist_ok=True)
    out = [
        '# Переводы интерфейса SchoolPR. Исходный язык — русский.',
        '# Обновление: python scripts/i18n.py extract && python scripts/i18n.py compile',
        'msgid ""',
        'msgstr ""',
        '"Content-Type: text/plain; charset=UTF-8\\n"',
        f'"Language: {lang}\\n"',
        f'"Plural-Forms: {LANGUAGES[lang]}\\n"',
        '',
    ]
    for msgid in sorted(messages):
        refs = ' '.join(sorted(messages[msgid]))
        out.append(f'#: {refs}')
        if '%(' in msgid:
            out.append('#, python-format')
        out.append(f'msgid "{po_escape(msgid)}"')
        out.append(f'msgstr "{po_escape(translations.get(msgid, ""))}"')
        out.append('')
    path.write_text('\n'.join(out), encoding='utf-8', newline='\n')


def write_mo(path, translations):
    """Файл .mo в формате GNU gettext (little-endian)."""
    header = 'Content-Type: text/plain; charset=UTF-8\n'
    items = sorted([('', header)] + [(k, v) for k, v in translations.items() if v])
    ids = b''
    strs = b''
    offsets = []
    for msgid, msgstr in items:
        bid, bstr = msgid.encode('utf-8'), msgstr.encode('utf-8')
        offsets.append((len(ids), len(bid), len(strs), len(bstr)))
        ids += bid + b'\0'
        strs += bstr + b'\0'
    n = len(items)
    keys_start = 7 * 4 + 16 * n
    values_start = keys_start + len(ids)
    koffsets, voffsets = [], []
    for o1, l1, o2, l2 in offsets:
        koffsets += [l1, o1 + keys_start]
        voffsets += [l2, o2 + values_start]
    data = struct.pack('Iiiiiii', 0x950412de, 0, n, 7 * 4, 7 * 4 + n * 8, 0, 0)
    data += struct.pack(f'{len(koffsets)}i', *koffsets) + struct.pack(f'{len(voffsets)}i', *voffsets)
    path.write_bytes(data + ids + strs)


def po_path(lang):
    return LOCALE_DIR / lang / 'LC_MESSAGES' / 'django.po'


def overrides_path(lang):
    return LOCALE_DIR / lang / 'LC_MESSAGES' / 'overrides.po'


def read_overrides(path):
    """Ручные дополнения к стандартным переводам Django (overrides.po), в том числе формы множественного числа.

    Ключ для множественного числа — "msgid\\0msgid_plural", значение — "msgstr[0]\\0msgstr[1]...", как в .mo.
    """
    result = {}
    if not path.exists():
        return result
    for block in path.read_text(encoding='utf-8').split('\n\n'):
        def joined(tag):
            m = re.search(rf'^{tag} ((?:".*"\n?)+)', block, re.M)
            return po_unescape(''.join(re.findall(r'"(.*)"', m.group(1)))) if m else None
        msgid = joined('msgid')
        if not msgid:
            continue
        plural = joined('msgid_plural')
        if plural is None:
            result[msgid] = joined('msgstr') or ''
        else:
            forms = []
            for i in range(10):
                form = joined(rf'msgstr\[{i}\]')
                if form is None:
                    break
                forms.append(form)
            if all(forms):
                result[f'{msgid}\0{plural}'] = '\0'.join(forms)
    return result


def check_placeholders(msgid, msgstr):
    want = sorted(re.findall(r'%\(\w+\)s', msgid))
    return not msgstr or sorted(re.findall(r'%\(\w+\)s', msgstr)) == want


def main(cmd):
    if cmd == 'extract':
        messages = {}
        extract_templates(messages)
        extract_python(messages)
        for lang in LANGUAGES:
            write_po(po_path(lang), lang, messages, read_po(po_path(lang)))
        print(f'{len(messages)} фраз → locale/{{{",".join(LANGUAGES)}}}/LC_MESSAGES/django.po')
    elif cmd == 'compile':
        for lang in LANGUAGES:
            translations = read_po(po_path(lang))
            bad = [k for k, v in translations.items() if not check_placeholders(k, v)]
            if bad:
                sys.exit(f'{lang}: не совпадают подстановки %(...)s в {len(bad)} фразах, например: {bad[0]!r}')
            overrides = read_overrides(overrides_path(lang))
            write_mo(po_path(lang).with_suffix('.mo'), {**translations, **overrides})
            extra = f' + {len(overrides)} дополнений к Django' if overrides else ''
            print(f'{lang}: {sum(1 for v in translations.values() if v)} переводов{extra}')
    elif cmd == 'status':
        for lang in LANGUAGES:
            translations = read_po(po_path(lang))
            missing = [k for k, v in translations.items() if not v]
            print(f'{lang}: переведено {len(translations) - len(missing)} из {len(translations)}')
            for k in missing[:15]:
                print('   -', k)
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '')
