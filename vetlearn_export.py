"""
vetlearn_export.py
vetlearn.db → vetlearn_data.json 変換スクリプト
"""
import sqlite3
import json
import os
import sys
from collections import Counter

sys.stdout.reconfigure(encoding='utf-8')

BASE = os.path.dirname(os.path.abspath(__file__))
DB   = os.path.join(BASE, 'vetlearn.db')
OUT  = os.path.join(BASE, 'vetlearn_data.json')


def get_db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def normalize_tag(name: str) -> str:
    """タグ名を #xxx 形式に統一する"""
    name = name.strip()
    return name if name.startswith('#') else '#' + name


def main():
    c = get_db()

    # ── カテゴリ ────────────────────────────────────────────────────
    categories = [
        {
            'id':        r['id'],
            'name':      r['name'],
            'parent_id': r['parent_id'],
            'level':     r['level'],
        }
        for r in c.execute(
            'SELECT id, name, parent_id, level FROM categories ORDER BY level, parent_id, id'
        ).fetchall()
    ]

    cat_name = {c_['id']: c_['name'] for c_ in categories}

    # ── タグマップ (item_id → [#tag, ...]) ─────────────────────────
    tags_by_item: dict[int, list[str]] = {}
    for r in c.execute('''
        SELECT it.item_id, t.name
        FROM item_tags it
        JOIN tags t ON t.id = it.tag_id
        ORDER BY it.item_id, t.name
    ''').fetchall():
        tags_by_item.setdefault(r['item_id'], []).append(normalize_tag(r['name']))

    # ── 添付ファイルマップ (item_id → [{filename, path}]) ──────────
    att_by_item: dict[int, list[dict]] = {}
    for r in c.execute(
        'SELECT item_id, filename, stored_path FROM attachments ORDER BY item_id, id'
    ).fetchall():
        att_by_item.setdefault(r['item_id'], []).append({
            'filename':    r['filename'],
            'stored_path': r['stored_path'],
        })

    # ── 項目 ────────────────────────────────────────────────────────
    items = []
    for r in c.execute('SELECT * FROM items ORDER BY id').fetchall():
        d   = dict(r)
        iid = d['id']
        title = (d.get('title') or '').strip()

        # 定義: items.definition 優先、空なら旧 content フィールドを使用
        definition = (d.get('definition') or '').strip() or (d.get('content') or '').strip()

        # SM-2: items カラム優先、未設定なら review_cards を参照
        ef   = d.get('ease_factor')   or 2.5
        iv   = d.get('interval_days') or 1
        reps = d.get('repetitions')   or 0
        nd   = d.get('next_review_date')

        if not nd:
            rc = c.execute(
                'SELECT due_date, ease_factor, interval_days, repetitions FROM review_cards WHERE item_id=? LIMIT 1',
                (iid,)
            ).fetchone()
            if rc:
                nd   = rc['due_date']
                ef   = rc['ease_factor']   or ef
                iv   = rc['interval_days'] or iv
                reps = rc['repetitions']   or reps

        items.append({
            'id':                   iid,
            'title':                title,
            'main_tag':             normalize_tag(title),
            'category_id':          d.get('category_id'),
            'definition':           definition,
            'normal_values':        (d.get('normal_values')         or '').strip(),
            'clinical_significance':(d.get('clinical_significance') or '').strip(),
            'related_diseases':     (d.get('related_diseases')      or '').strip(),
            'notes':                (d.get('notes')                 or '').strip(),
            'proficiency':          d.get('proficiency') or 0,
            'trust_flag':           d.get('trust_flag')  or '✅',
            'tags':                 tags_by_item.get(iid, []),
            'attachments':          att_by_item.get(iid, []),
            'next_review':          nd,
            'ease_factor':          round(float(ef), 4),
            'interval':             int(iv),
            'repetitions':          int(reps),
            'created_at':           d.get('created_at') or '',
        })

    c.close()

    # ── JSON 出力 ───────────────────────────────────────────────────
    payload = {'categories': categories, 'items': items}
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # ── サマリー表示 ────────────────────────────────────────────────
    print(f'\n出力先: {OUT}')
    print('=' * 52)

    print(f'\n■ カテゴリ  {len(categories)} 件')
    by_level = Counter(c_['level'] for c_ in categories)
    for lv in sorted(by_level):
        lv_names = [c_['name'] for c_ in categories if c_['level'] == lv]
        print(f'  Lv.{lv} ({by_level[lv]}件): {", ".join(lv_names)}')

    print(f'\n■ 項目      {len(items)} 件')
    by_cat = Counter(item['category_id'] for item in items)
    for cid, cnt in sorted(by_cat.items(), key=lambda x: (-x[1], x[0])):
        print(f'  {cat_name.get(cid, "未分類"):20s}: {cnt} 件')

    total_tags  = sum(len(item['tags']) for item in items)
    unique_tags = len({t for item in items for t in item['tags']})
    print(f'\n■ タグ      計 {total_tags} 件 / ユニーク {unique_tags} 種')
    all_tags = [t for item in items for t in item['tags']]
    for tag, cnt in Counter(all_tags).most_common(10):
        print(f'  {tag}: {cnt}件')

    atts = sum(len(item['attachments']) for item in items)
    print(f'\n■ 添付ファイル  {atts} 件')

    trust_cnt = Counter(item['trust_flag'] for item in items)
    print(f'\n■ 信頼度フラグ')
    for flag in ['✅', '⚠️', '❓']:
        print(f'  {flag}: {trust_cnt.get(flag, 0)} 件')

    overdue = sum(1 for item in items if item['next_review'] and item['next_review'] < '2026-06-03')
    print(f'\n■ 復習期限超過  {overdue} 件')

    print('\n✅ 変換完了')


if __name__ == '__main__':
    main()
