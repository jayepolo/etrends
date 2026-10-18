"""Backfill historical vendor prices and scrape logs from an older eTrends DB.

Copies rows dated before a cutoff from SOURCE into TARGET, without touching
existing rows or the user table, and writes a JSON manifest of every inserted
row id so the backfill can be audited and rolled back.

Usage:
    python scripts/backfill_price_history.py backfill SOURCE TARGET --before 2025-11-18 --manifest out.json
    python scripts/backfill_price_history.py undo TARGET --manifest out.json

Rules:
    - Vendors are matched by exact name; missing vendors are created.
    - price_data rows with date < --before are copied, skipping $0 prices and
      any (vendor, date) pair the target already has.
    - scrape_log rows older than the target's oldest log entry are copied.
"""
import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime


def backfill(source, target, before, manifest_path):
    conn = sqlite3.connect(target)
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('ATTACH DATABASE ? AS src', (source,))

    manifest = {
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'source': source,
        'target': target,
        'before': before,
        'vendor_ids': [],
        'price_data_ids': [],
        'scrape_log_ids': [],
        'new_vendors': [],
        'skipped_existing_prices': 0,
    }

    with conn:
        # 1. Vendors: map source vendor id -> target vendor id, creating missing ones
        target_vendors = dict(conn.execute('SELECT name, id FROM main.vendor'))
        vendor_map = {}
        for src_id, name, town, phone in conn.execute(
                'SELECT id, name, town, phone FROM src.vendor ORDER BY id').fetchall():
            if name not in target_vendors:
                cur = conn.execute('INSERT INTO main.vendor (name, town, phone) VALUES (?, ?, ?)',
                                   (name, town, phone))
                target_vendors[name] = cur.lastrowid
                manifest['vendor_ids'].append(cur.lastrowid)
                manifest['new_vendors'].append(name)
            vendor_map[src_id] = target_vendors[name]

        # 2. Prices before the cutoff, skipping (vendor, date) pairs already present
        existing = set(conn.execute('SELECT vendor_id, date FROM main.price_data'))
        by_year = Counter()
        by_month = Counter()
        rows = conn.execute(
            'SELECT vendor_id, price, date FROM src.price_data WHERE date < ? AND price > 0 ORDER BY date, vendor_id',
            (before,)).fetchall()
        for src_vendor_id, price, date in rows:
            vendor_id = vendor_map[src_vendor_id]
            if (vendor_id, date) in existing:
                manifest['skipped_existing_prices'] += 1
                continue
            cur = conn.execute('INSERT INTO main.price_data (vendor_id, price, date) VALUES (?, ?, ?)',
                               (vendor_id, price, date))
            existing.add((vendor_id, date))
            manifest['price_data_ids'].append(cur.lastrowid)
            by_year[date[:4]] += 1
            by_month[date[:7]] += 1

        # 3. Scrape logs older than the target's oldest entry
        oldest_log = conn.execute('SELECT min(timestamp) FROM main.scrape_log').fetchone()[0]
        for ts, success, message, scheduled, scrape_type in conn.execute(
                'SELECT timestamp, success, message, scheduled, scrape_type FROM src.scrape_log '
                'WHERE ? IS NULL OR timestamp < ? ORDER BY timestamp',
                (oldest_log, oldest_log)).fetchall():
            cur = conn.execute(
                'INSERT INTO main.scrape_log (timestamp, success, message, scheduled, scrape_type) '
                'VALUES (?, ?, ?, ?, ?)', (ts, success, message, scheduled, scrape_type))
            manifest['scrape_log_ids'].append(cur.lastrowid)

    dates = [r[0] for r in conn.execute(
        'SELECT date FROM main.price_data WHERE id IN (%s)' % ','.join('?' * len(manifest['price_data_ids'])),
        manifest['price_data_ids'])] if manifest['price_data_ids'] else []
    manifest['price_date_range'] = [min(dates), max(dates)] if dates else None
    manifest['prices_by_year'] = dict(sorted(by_year.items()))
    manifest['prices_by_month'] = dict(sorted(by_month.items()))
    conn.close()

    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)

    print(f"New vendors ({len(manifest['new_vendors'])}): {', '.join(manifest['new_vendors'])}")
    print(f"Prices inserted: {len(manifest['price_data_ids'])} "
          f"(skipped {manifest['skipped_existing_prices']} already present), range {manifest['price_date_range']}")
    print(f"Prices by year: {manifest['prices_by_year']}")
    print(f"Scrape logs inserted: {len(manifest['scrape_log_ids'])}")
    print(f"Manifest: {manifest_path}")


def undo(target, manifest_path):
    with open(manifest_path) as f:
        manifest = json.load(f)
    conn = sqlite3.connect(target)
    with conn:
        for table, key in [('price_data', 'price_data_ids'), ('scrape_log', 'scrape_log_ids'),
                           ('vendor', 'vendor_ids')]:
            ids = manifest[key]
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                conn.execute(f'DELETE FROM {table} WHERE id IN ({",".join("?" * len(chunk))})', chunk)
            print(f'{table}: removed {len(ids)} rows')
    conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    b = sub.add_parser('backfill')
    b.add_argument('source')
    b.add_argument('target')
    b.add_argument('--before', required=True, help='Copy prices dated before this (YYYY-MM-DD)')
    b.add_argument('--manifest', required=True)
    u = sub.add_parser('undo')
    u.add_argument('target')
    u.add_argument('--manifest', required=True)
    args = parser.parse_args()

    if args.command == 'backfill':
        backfill(args.source, args.target, args.before, args.manifest)
    else:
        undo(args.target, args.manifest)
