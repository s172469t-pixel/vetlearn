from flask import Flask, request, jsonify, send_from_directory
import sqlite3, os, uuid
from datetime import date, timedelta

app = Flask(__name__)
BASE = os.path.dirname(os.path.abspath(__file__))
DB   = os.path.join(BASE, 'vetlearn.db')
UPL  = os.path.join(BASE, 'attachments')
os.makedirs(UPL, exist_ok=True)

@app.after_request
def cors(r):
    r.headers['Access-Control-Allow-Origin']  = '*'
    r.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,PATCH,DELETE,OPTIONS'
    r.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return r

@app.route('/<path:p>', methods=['OPTIONS'])
@app.route('/', methods=['OPTIONS'])
def options(p=''):
    return '', 204

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c

def _add_col(conn, table, col, defn):
    existing = [r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()]
    if col not in existing:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {col} {defn}')

def setup():
    c = db()
    # Core tables (created if missing)
    c.executescript("""
CREATE TABLE IF NOT EXISTS categories(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT    NOT NULL,
  parent_id  INTEGER REFERENCES categories(id),
  level      INTEGER NOT NULL DEFAULT 1,
  created_at TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS items(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  title       TEXT    NOT NULL,
  content     TEXT    NOT NULL DEFAULT '',
  category_id INTEGER REFERENCES categories(id),
  created_at  TEXT    DEFAULT (datetime('now')),
  updated_at  TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS tags(
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS item_tags(
  item_id INTEGER NOT NULL,
  tag_id  INTEGER NOT NULL,
  PRIMARY KEY(item_id, tag_id)
);
CREATE TABLE IF NOT EXISTS attachments(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id     INTEGER NOT NULL,
  filename    TEXT    NOT NULL,
  stored_path TEXT    NOT NULL,
  created_at  TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS review_cards(
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id       INTEGER NOT NULL,
  ease_factor   REAL    NOT NULL DEFAULT 2.5,
  interval_days INTEGER NOT NULL DEFAULT 1,
  repetitions   INTEGER NOT NULL DEFAULT 0,
  due_date      TEXT,
  last_reviewed TEXT,
  created_at    TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS related_items(
  item_id         INTEGER NOT NULL,
  related_item_id INTEGER NOT NULL,
  PRIMARY KEY(item_id, related_item_id)
);
    """)
    # Add new columns to items if missing
    _add_col(c, 'items', 'proficiency',          'INTEGER NOT NULL DEFAULT 0')
    _add_col(c, 'items', 'trust_flag',            "TEXT NOT NULL DEFAULT '✅'")
    _add_col(c, 'items', 'definition',            "TEXT DEFAULT ''")
    _add_col(c, 'items', 'normal_values',         "TEXT DEFAULT ''")
    _add_col(c, 'items', 'clinical_significance', "TEXT DEFAULT ''")
    _add_col(c, 'items', 'related_diseases',      "TEXT DEFAULT ''")
    _add_col(c, 'items', 'notes',                 "TEXT DEFAULT ''")
    _add_col(c, 'items', 'ease_factor',           'REAL NOT NULL DEFAULT 2.5')
    _add_col(c, 'items', 'interval_days',         'INTEGER NOT NULL DEFAULT 1')
    _add_col(c, 'items', 'repetitions',           'INTEGER NOT NULL DEFAULT 0')
    _add_col(c, 'items', 'next_review_date',      'TEXT')
    # Migrate SM-2 data from review_cards → items (for items that lack SM-2 cols)
    c.execute("""
        UPDATE items SET
            ease_factor   = (SELECT rc.ease_factor   FROM review_cards rc WHERE rc.item_id=items.id LIMIT 1),
            interval_days = (SELECT rc.interval_days FROM review_cards rc WHERE rc.item_id=items.id LIMIT 1),
            repetitions   = (SELECT rc.repetitions   FROM review_cards rc WHERE rc.item_id=items.id LIMIT 1),
            next_review_date = (SELECT rc.due_date   FROM review_cards rc WHERE rc.item_id=items.id LIMIT 1)
        WHERE ease_factor=2.5 AND EXISTS (SELECT 1 FROM review_cards rc WHERE rc.item_id=items.id)
    """)
    c.commit(); c.close()

setup()

def sm2(grade, ef, iv, reps):
    if grade >= 3:
        new_iv   = 1 if reps == 0 else 6 if reps == 1 else round(iv * ef)
        new_reps = reps + 1
        new_ef   = max(1.3, ef + 0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02))
    else:
        new_iv, new_reps, new_ef = 1, 0, max(1.3, ef - 0.2)
    next_date = (date.today() + timedelta(days=new_iv)).isoformat()
    return new_ef, new_iv, new_reps, next_date

def row_to_item(r):
    d = dict(r)
    d['name'] = d.pop('title', d.get('name', ''))
    # Backward compat: show legacy content in definition if definition is empty
    if not d.get('definition') and d.get('content'):
        d['definition'] = d.get('content', '')
    return d

# ── Categories ──────────────────────────────────────────────────────

@app.route('/api/categories')
def get_cats():
    c = db()
    rows = c.execute('SELECT * FROM categories ORDER BY level, parent_id, name').fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/categories', methods=['POST'])
def new_cat():
    d    = request.json or {}
    name = (d.get('name') or '').strip()
    if not name: return jsonify({'error': 'name required'}), 400
    c   = db()
    cur = c.execute('INSERT INTO categories(name, parent_id, level) VALUES(?,?,?)',
                    (name, d.get('parent_id'), d.get('level', 1)))
    c.commit(); cid = cur.lastrowid; c.close()
    return jsonify({'id': cid, 'name': name, 'parent_id': d.get('parent_id'), 'level': d.get('level', 1)})

# ── Items ────────────────────────────────────────────────────────────

@app.route('/api/items/random')
def random_items():
    today = date.today().isoformat()
    c = db(); seen = set(); res = []
    for r in c.execute(
        "SELECT * FROM items WHERE next_review_date IS NOT NULL AND next_review_date<=? ORDER BY next_review_date", (today,)
    ).fetchall():
        if r['id'] not in seen: seen.add(r['id']); res.append(row_to_item(r))
    for r in c.execute(
        "SELECT * FROM items WHERE trust_flag IN('⚠️','❓') ORDER BY proficiency, RANDOM()"
    ).fetchall():
        if r['id'] not in seen: seen.add(r['id']); res.append(row_to_item(r))
    for r in c.execute("SELECT * FROM items ORDER BY proficiency, RANDOM()").fetchall():
        if r['id'] not in seen: seen.add(r['id']); res.append(row_to_item(r))
    c.close()
    return jsonify(res[:3])

@app.route('/api/items')
def get_items():
    q   = (request.args.get('q') or '').strip()
    cat = request.args.get('category_id')
    c   = db()
    if q:
        rows = c.execute("SELECT * FROM items WHERE title LIKE ? ORDER BY title", (f'%{q}%',)).fetchall()
    elif cat:
        rows = c.execute("""
            WITH RECURSIVE desc(id) AS (
                SELECT ?
                UNION ALL
                SELECT c.id FROM categories c JOIN desc ON c.parent_id = desc.id
            )
            SELECT * FROM items WHERE category_id IN (SELECT id FROM desc) ORDER BY title
        """, (cat,)).fetchall()
    else:
        rows = c.execute("SELECT * FROM items ORDER BY title").fetchall()
    c.close()
    return jsonify([row_to_item(r) for r in rows])

@app.route('/api/items/<int:iid>')
def get_item(iid):
    c    = db()
    item = c.execute('SELECT * FROM items WHERE id=?', (iid,)).fetchone()
    if not item: c.close(); return jsonify({'error': 'not found'}), 404
    rel  = c.execute("""
        SELECT i.id, i.title AS name FROM items i
        JOIN related_items ri ON ri.related_item_id=i.id
        WHERE ri.item_id=?
    """, (iid,)).fetchall()
    att  = c.execute('SELECT id, filename, stored_path AS filepath FROM attachments WHERE item_id=?', (iid,)).fetchall()
    crumbs = []; cat_id = item['category_id']
    while cat_id:
        cat = c.execute('SELECT * FROM categories WHERE id=?', (cat_id,)).fetchone()
        if not cat: break
        crumbs.insert(0, {'id': cat['id'], 'name': cat['name']}); cat_id = cat['parent_id']
    c.close()
    res = row_to_item(item)
    res['related_tags'] = [dict(r) for r in rel]
    res['attachments']  = [dict(a) for a in att]
    res['breadcrumb']   = crumbs
    return jsonify(res)

@app.route('/api/items', methods=['POST'])
def new_item():
    d   = request.json or {}
    c   = db()
    today = date.today().isoformat()
    cur = c.execute("""
        INSERT INTO items(title, category_id, definition, normal_values,
            clinical_significance, related_diseases, notes,
            proficiency, trust_flag, next_review_date,
            ease_factor, interval_days, repetitions, content)
        VALUES(?,?,?,?,?,?,?,?,?,?,2.5,1,0,'')
    """, (
        (d.get('name') or '').strip(),
        d.get('category_id'),
        d.get('definition', ''), d.get('normal_values', ''),
        d.get('clinical_significance', ''), d.get('related_diseases', ''),
        d.get('notes', ''),
        d.get('proficiency', 0), d.get('trust_flag', '✅'), today,
    ))
    iid = cur.lastrowid
    for rid in (d.get('related_tag_ids') or []):
        try: c.execute('INSERT OR IGNORE INTO related_items VALUES(?,?)', (iid, rid))
        except: pass
    c.commit(); c.close()
    return jsonify({'id': iid})

@app.route('/api/items/<int:iid>', methods=['PUT'])
def upd_item(iid):
    d    = request.json or {}
    c    = db()
    item = c.execute('SELECT * FROM items WHERE id=?', (iid,)).fetchone()
    if not item: c.close(); return jsonify({'error': 'not found'}), 404
    np   = d.get('proficiency', item['proficiency'])
    ef, iv, reps, nd = item['ease_factor'], item['interval_days'], item['repetitions'], item['next_review_date']
    if np != item['proficiency']:
        ef, iv, reps, nd = sm2(np, ef, iv, reps)
    c.execute("""UPDATE items SET
        title=?, category_id=?, definition=?, normal_values=?,
        clinical_significance=?, related_diseases=?, notes=?,
        proficiency=?, trust_flag=?, next_review_date=?,
        ease_factor=?, interval_days=?, repetitions=?, updated_at=datetime('now')
        WHERE id=?""",
        (d.get('name', item['title']),
         d.get('category_id', item['category_id']),
         d.get('definition', item['definition'] or ''),
         d.get('normal_values', item['normal_values'] or ''),
         d.get('clinical_significance', item['clinical_significance'] or ''),
         d.get('related_diseases', item['related_diseases'] or ''),
         d.get('notes', item['notes'] or ''),
         np, d.get('trust_flag', item['trust_flag']),
         nd, ef, iv, reps, iid))
    if 'related_tag_ids' in d:
        c.execute('DELETE FROM related_items WHERE item_id=?', (iid,))
        for rid in (d['related_tag_ids'] or []):
            try: c.execute('INSERT OR IGNORE INTO related_items VALUES(?,?)', (iid, rid))
            except: pass
    c.commit(); c.close()
    return jsonify({'ok': True})

@app.route('/api/items/<int:iid>/proficiency', methods=['PATCH'])
def patch_prof(iid):
    d    = request.json or {}
    c    = db()
    item = c.execute('SELECT * FROM items WHERE id=?', (iid,)).fetchone()
    if not item: c.close(); return jsonify({'error': 'not found'}), 404
    np   = d.get('proficiency', 0)
    ef, iv, reps, nd = sm2(np, item['ease_factor'], item['interval_days'], item['repetitions'])
    c.execute("""UPDATE items SET proficiency=?, ease_factor=?, interval_days=?,
        repetitions=?, next_review_date=?, updated_at=datetime('now') WHERE id=?""",
        (np, ef, iv, reps, nd, iid))
    c.commit(); c.close()
    return jsonify({'ok': True, 'next_review_date': nd})

@app.route('/api/items/<int:iid>/trust', methods=['PATCH'])
def patch_trust(iid):
    d = request.json or {}
    c = db()
    c.execute("UPDATE items SET trust_flag=?, updated_at=datetime('now') WHERE id=?",
              (d.get('trust_flag', '✅'), iid))
    c.commit(); c.close()
    return jsonify({'ok': True})

@app.route('/api/items/<int:iid>', methods=['DELETE'])
def del_item(iid):
    c = db()
    atts = c.execute('SELECT stored_path FROM attachments WHERE item_id=?', (iid,)).fetchall()
    for a in atts:
        try: os.remove(os.path.join(UPL, a['stored_path']))
        except: pass
    c.execute('DELETE FROM items WHERE id=?', (iid,))
    c.commit(); c.close()
    return jsonify({'ok': True})

# ── Attachments ──────────────────────────────────────────────────────

@app.route('/api/items/<int:iid>/attachments', methods=['POST'])
def upload_att(iid):
    if 'file' not in request.files: return jsonify({'error': 'no file'}), 400
    f   = request.files['file']
    ext = os.path.splitext(f.filename)[1]
    sfn = uuid.uuid4().hex + ext
    f.save(os.path.join(UPL, sfn))
    c   = db()
    cur = c.execute('INSERT INTO attachments(item_id, filename, stored_path) VALUES(?,?,?)',
                    (iid, f.filename, sfn))
    c.commit(); aid = cur.lastrowid; c.close()
    return jsonify({'id': aid, 'filename': f.filename, 'filepath': sfn})

@app.route('/api/attachments/<int:aid>', methods=['DELETE'])
def del_att(aid):
    c   = db()
    att = c.execute('SELECT * FROM attachments WHERE id=?', (aid,)).fetchone()
    if att:
        try: os.remove(os.path.join(UPL, att['stored_path']))
        except: pass
        c.execute('DELETE FROM attachments WHERE id=?', (aid,))
    c.commit(); c.close()
    return jsonify({'ok': True})

@app.route('/attachments/<path:fn>')
def serve_att(fn):
    return send_from_directory(UPL, fn)

if __name__ == '__main__':
    print('VetLearn API → http://localhost:5000')
    print('起動: pip install flask  →  python vetlearn_api.py')
    app.run(debug=True, port=5000, host='0.0.0.0')
