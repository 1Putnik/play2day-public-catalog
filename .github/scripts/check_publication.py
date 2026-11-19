#!/usr/bin/env python3
"""Refuse to publish catalog documents that would replace the last correct output.

Runs before the collector state and the public files are committed. Standard library only.
The document rules mirror the 0.5.51 client validators (modules/travel/sailings.mjs and
world-parties.mjs): the client rejects a WHOLE document when one row is invalid, so one bad row
would hide every live listing. Exit 0 = safe to publish, 1 = refuse (the previous commit stays).
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import zoneinfo
from urllib.parse import urlsplit

MAX_DOC = 2 * 1024 * 1024          # live-catalog.mjs maxSnapshot
MAX_IMAGE = 512 * 1024             # live-catalog.mjs maxImage
FILE_PATH = re.compile(r'^/?assets/(?:cruises|world-parties)/[a-z0-9_-]+\.(?:webp|png|jpe?g)$', re.I)
REMOTE_ART = re.compile(r'^(?:https://play2day\.co\.il)?/api/catalog/media/([0-9a-f]{64})\.webp$')
ROW_ID = re.compile(r'^[a-z0-9-]+$')
TEL = re.compile(r'^tel:\+[1-9]\d{7,14}$')
ISO_DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')
ALLOWED_PORTS = set('US CW AW MX SX KN PR VI JM KY DO TC HR GR ME IT PF CK WS TO FJ JP KR VG AI MF BL AG BS HT HN'.split())
BOT_EMAILS = ('41898282+github-actions[bot]@users.noreply.github.com',)


def local(value, lang='en'):
    text = value if isinstance(value, str) else (value.get(lang) or value.get('en')) if isinstance(value, dict) else None
    return text if isinstance(text, str) else ''


def safe_link(value):
    if not isinstance(value, str):
        return ''
    try:
        url = urlsplit(value)
    except ValueError:
        return ''
    try:
        host = url.hostname
        url.port  # validate the port number, as the browser URL constructor does
    except ValueError:
        return ''
    return value if url.scheme == 'https' and host and not re.search(r'[\s%\\<>^|]', host) and not url.username and not url.password else ''


def catalog_image(value):
    if not isinstance(value, str):
        return ''
    path = re.sub(r'^https://play2day\.co\.il(?=/assets/)', '', value)
    return value if FILE_PATH.match(path) or REMOTE_ART.match(value) else ''


def day(value):
    if not isinstance(value, str) or not ISO_DAY.match(value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def instant(value):
    """Date.parse equivalent for the ISO strings the collector writes."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


_ZONES = zoneinfo.available_timezones()


def time_zone(name):
    if not isinstance(name, str) or not name:
        return False
    if not _ZONES:  # No tz database on this machine (e.g. Windows without tzdata): shape check only.
        return bool(re.match(r'^[A-Za-z_]+(?:/[A-Za-z0-9_+-]+){0,2}$', name))
    return name in _ZONES


def action_url(row):
    if row.get('action') == 'waitlist' and isinstance(row.get('bookingUrl'), str) and TEL.match(row['bookingUrl']):
        return row['bookingUrl']
    return safe_link(row.get('bookingUrl'))


def cruise_errors(doc):
    rows = doc.get('sailings') if isinstance(doc, dict) else None
    if not isinstance(rows, list) or len(rows) > 1000 or not day(doc.get('checkedDate')):
        return ['cruises: document shape or checkedDate']
    errors, seen = [], set()
    for row in rows:
        rid = row.get('id') if isinstance(row, dict) else None
        if not isinstance(row, dict) or not isinstance(rid, str) or not ROW_ID.match(rid):
            errors.append('cruises: bad id %r' % (rid,))
            continue
        problems = [name for name, ok in (
            ('duplicate id', rid not in seen), ('title', local(row.get('title'))),
            ('start', day(row.get('start'))), ('end', day(row.get('end'))),
            ('ports', isinstance(row.get('ports'), list) and all(local(p) for p in row['ports'])),
            ('sourceUrl', safe_link(row.get('sourceUrl'))), ('bookingUrl', action_url(row)),
            ('photo', catalog_image(row.get('photo'))), ('poster', catalog_image(row.get('poster'))),
        ) if not ok]
        errors += ['cruises: %s %s' % (rid, p) for p in problems]
        seen.add(rid)
    return errors


def party_errors(doc):
    events = doc.get('events') if isinstance(doc, dict) else None
    media = doc.get('media') if isinstance(doc, dict) else None
    if not isinstance(events, list) or len(events) > 1000 or not isinstance(media, dict) or not instant(doc.get('checked')):
        return ['parties: document shape or checked']
    errors, seen = [], set()
    for event in events:
        eid = event.get('id') if isinstance(event, dict) else None
        if not isinstance(eid, str) or not eid:
            errors.append('parties: bad id %r' % (eid,))
            continue
        art = media.get(event.get('cityKey')) if isinstance(event.get('cityKey'), str) else None
        problems = [name for name, ok in (
            ('duplicate id', eid not in seen), ('name', event.get('name')),
            ('sourceUrl', safe_link(event.get('sourceUrl'))), ('bookingUrl', safe_link(event.get('bookingUrl'))),
            ('city', local(event.get('city'))), ('start', instant(event.get('start'))),
            ('hideAfter', instant(event.get('hideAfter'))),
            ('end', not event.get('end') or instant(event.get('end'))),
            ('timeZone', time_zone(event.get('timeZone'))),
            ('media', isinstance(art, dict) and catalog_image(art.get('photo')) and catalog_image(art.get('poster'))),
        ) if not ok]
        errors += ['parties: %s %s' % (eid, p) for p in problems]
        seen.add(eid)
    return errors


def admitted_cruises(doc, today):
    """sailings.mjs admit() with no legacy itinerary (the strict case)."""
    out = []
    for row in doc.get('sailings', []):
        if not isinstance(row, dict) or row.get('status') == 'cancelled' or (row.get('status') == 'soldout' and row.get('action') != 'waitlist'):
            continue
        start, end, ports, cc = row.get('start'), row.get('end'), row.get('ports'), row.get('portCountryCodes')
        if not (isinstance(row.get('id'), str) and ROW_ID.match(row['id']) and day(start) and day(end) and today <= start <= end):
            continue
        if not (row.get('operator') and local(row.get('title')) and catalog_image(row.get('photo')) and catalog_image(row.get('poster'))
                and safe_link(row.get('sourceUrl')) and action_url(row)):
            continue
        if row.get('action') not in ('book', 'apply', 'waitlist', 'quote') or not isinstance(ports, list) or len(ports) < 2 or not all(local(p) for p in ports):
            continue
        if not isinstance(cc, list) or len(cc) != len(ports) or not all(c in ALLOWED_PORTS for c in cc):
            continue
        norm = [re.sub(r'\s*,\s*', ',', local(p)).strip().lower() for p in ports]
        loop = norm[0] == norm[-1]
        if (row.get('routeType') == 'ONE_WAY' and not loop) or (row.get('routeType') != 'ONE_WAY' and loop and len(ports) >= 3):
            out.append(row)
    return out


def visible_parties(doc, now):
    return [e for e in doc.get('events', []) if isinstance(e, dict) and (instant(e.get('hideAfter')) or now) > now
            and e.get('status') != 'cancelled' and (e.get('status') != 'soldout' or e.get('action') == 'waitlist')]


def duplicate_errors(cruises, parties):
    """A repeated import must not list the same sailing/event twice under a new id."""
    errors, keys = [], {}
    for row in cruises.get('sailings', []):
        key = (str(row.get('operator')).lower(), local(row.get('ship')).lower(), row.get('start'), row.get('end'))
        if key in keys:
            errors.append('cruises: %s duplicates %s' % (row.get('id'), keys[key]))
        keys.setdefault(key, row.get('id'))
    keys = {}
    for event in parties.get('events', []):
        key = (event.get('sourceUrl'), event.get('start'))
        if key in keys:
            errors.append('parties: %s duplicates %s' % (event.get('id'), keys[key]))
        keys.setdefault(key, event.get('id'))
    return errors


def media_errors(directory, *docs):
    errors = []
    refs = set()

    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            match = REMOTE_ART.match(value)
            if match:
                refs.add(match.group(1))
    for doc in docs:
        walk(doc)
    for digest in sorted(refs):
        path = os.path.join(directory, 'media', digest + '.webp')
        if not os.path.isfile(path):
            errors.append('media: missing %s.webp' % digest)
            continue
        with open(path, 'rb') as handle:
            data = handle.read()
        if hashlib.sha256(data).hexdigest() != digest:
            errors.append('media: hash mismatch %s.webp' % digest)
        if len(data) > MAX_IMAGE or data[:4] != b'RIFF' or data[8:12] != b'WEBP':
            errors.append('media: not a client-loadable webp %s.webp' % digest)
    return errors


def shrink_errors(label, before, after):
    if before and not after:
        return ['%s: %d upcoming listings would become 0' % (label, before)]
    if before - after >= 3 and after * 2 < before:
        return ['%s: upcoming listings would drop from %d to %d' % (label, before, after)]
    return []


def read_text(path):
    if not os.path.isfile(path):
        return None
    with open(path, encoding='utf-8') as handle:
        return handle.read()


def read_doc(text, name, errors):
    if text is None:
        return None
    if len(text.encode('utf-8')) > MAX_DOC:
        errors.append('%s: larger than the client limit' % name)
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        errors.append('%s: not JSON (%s)' % (name, exc))
        return None


def baseline_text(args, name):
    if args.baseline_dir:
        return read_text(os.path.join(args.baseline_dir, name))
    result = subprocess.run(['git', '-C', args.dir, 'show', '%s:%s' % (args.baseline_rev, name)],
                            capture_output=True, text=True, encoding='utf-8')
    return result.stdout if result.returncode == 0 else None


def workflow_author_warning(directory):
    """Operator check before pushing a workflow change; needs full history (CI checkouts are depth 1)."""
    shallow = subprocess.run(['git', '-C', directory, 'rev-parse', '--is-shallow-repository'], capture_output=True, text=True)
    if shallow.stdout.strip() != 'false':
        return ''
    result = subprocess.run(['git', '-C', directory, 'log', '-1', '--format=%ae', '--', '.github/workflows/refresh.yml'],
                            capture_output=True, text=True)
    email = result.stdout.strip()
    if result.returncode == 0 and email in BOT_EMAILS:
        return ('The last change to refresh.yml is attributed to github-actions[bot]; GitHub runs a schedule as the '
                'user who last changed it, so scheduled runs may never start. Re-commit the file as the owner account.')
    return ''


def check(args, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(zoneinfo.ZoneInfo('Asia/Jerusalem')).date().isoformat() if _ZONES else now.date().isoformat()
    errors = []
    new = {}
    for name in ('cruises.json', 'parties.json'):
        path = os.path.join(args.dir, name)
        new[name] = read_doc(read_text(path), name, errors)
        if new[name] is None and not any(e.startswith(name) for e in errors):
            errors.append('%s: missing' % name)
    old = {name: read_doc(baseline_text(args, name), 'baseline ' + name, errors) for name in new}
    if not args.baseline_dir and not all(isinstance(doc, dict) for doc in old.values()):
        errors.append('baseline: cannot read both documents from the last published commit')
    report = {'now': now.isoformat(timespec='seconds'), 'errors': errors}
    cruises, parties = new['cruises.json'], new['parties.json']
    errors += cruise_errors(cruises) + party_errors(parties)
    if not errors:
        errors += duplicate_errors(cruises, parties) + media_errors(args.dir, cruises, parties)
        before_c = len(admitted_cruises(old['cruises.json'], today)) if isinstance(old['cruises.json'], dict) else 0
        before_p = len(visible_parties(old['parties.json'], now)) if isinstance(old['parties.json'], dict) else 0
        after_c, after_p = len(admitted_cruises(cruises, today)), len(visible_parties(parties, now))
        errors += shrink_errors('cruises', before_c, after_c) + shrink_errors('parties', before_p, after_p)
        report.update(cruises={'rows': len(cruises.get('sailings', [])), 'upcomingBefore': before_c, 'upcomingAfter': after_c},
                      parties={'rows': len(parties.get('events', [])), 'upcomingBefore': before_p, 'upcomingAfter': after_p})
    report['publish'] = not errors
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--dir', default='.', help='published catalog checkout with the new documents')
    parser.add_argument('--baseline-rev', default='HEAD', help='git revision holding the last published output')
    parser.add_argument('--baseline-dir', help='directory with the last published output (instead of git)')
    parser.add_argument('--report', help='write the JSON report here as well')
    parser.add_argument('--warn-workflow-author', action='store_true')
    args = parser.parse_args(argv)
    if args.warn_workflow_author:
        warning = workflow_author_warning(args.dir)
        if warning:
            print('::warning title=Schedule owner::' + warning)
    report = check(args)
    text = json.dumps(report, indent=1, ensure_ascii=False)
    if args.report:
        with open(args.report, 'w', encoding='utf-8') as handle:
            handle.write(text + '\n')
    print(text)
    for error in report['errors']:
        print('::error title=Catalog not published::' + error)
    return 0 if report['publish'] else 1


if __name__ == '__main__':
    sys.exit(main())
