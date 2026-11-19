
"""Real sqlite3 store: content-hash primary key, INSERT-OR-IGNORE dedup, watermark."""
import json, sqlite3
from . import kernel
from .config import DB_PATH

class Store:
    def __init__(self, con=None):
        self.con = con
        if con is not None:
            self._init()

    def connect(self):
        con = sqlite3.connect(DB_PATH)
        self.__init__(con)
        return con

    def _init(self):
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS records("
            "content_hash TEXT PRIMARY KEY, id TEXT, ts TEXT, payload TEXT NOT NULL)")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        self.con.commit()
        self._migrate()

    def _migrate(self):
        """Re-key legacy rows whose hash covered derived annotations.

        Record identity is defined by source fields only.  Rows written by
        older versions are re-annotated and merged so that unchanged data
        keeps deduplicating instead of doubling.
        """
        rows = self.con.execute(
            "SELECT content_hash,payload FROM records").fetchall()
        for old_hash, payload in rows:
            rec = json.loads(payload)
            raw = {k: v for k, v in rec.items() if k not in kernel.ANNOTATION_KEYS}
            fresh = dict(raw)
            fresh.update(kernel.process(raw))
            new_hash = self.hash(fresh)
            if new_hash == old_hash and fresh == rec:
                continue
            self.con.execute("DELETE FROM records WHERE content_hash=?", (old_hash,))
            self.con.execute(
                "INSERT OR IGNORE INTO records(content_hash,id,ts,payload) "
                "VALUES(?,?,?,?)",
                (new_hash, str(fresh.get("id", "")), str(fresh.get("ts", "")),
                 json.dumps(fresh, ensure_ascii=False)))
        self.con.commit()

    @staticmethod
    def hash(record):
        return kernel.content_hash(record)

    def upsert(self, record):
        cur = self.con.execute(
            "INSERT OR IGNORE INTO records(content_hash,id,ts,payload) VALUES(?,?,?,?)",
            (self.hash(record), str(record.get("id", "")),
             str(record.get("ts", "")), json.dumps(record, ensure_ascii=False)))
        self.con.commit()
        return cur.rowcount > 0

    def count(self):
        return self.con.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    def all_records(self):
        return [json.loads(p) for (_h, p) in
                self.con.execute("SELECT content_hash,payload FROM records")]

    def get(self, content_hash):
        row = self.con.execute(
            "SELECT payload FROM records WHERE content_hash=?", (content_hash,)).fetchone()
        return json.loads(row[0]) if row else None

    def schedule(self, as_of=None):
        """Per-case 庭期结果 as of ``as_of`` (None means 今天)."""
        return kernel.schedule(self.all_records(), as_of=as_of)

    def watermark(self):
        row = self.con.execute("SELECT v FROM meta WHERE k='watermark'").fetchone()
        return row[0] if row else None

    def set_watermark(self, value):
        self.con.execute(
            "INSERT INTO meta(k,v) VALUES('watermark',?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(value),))
        self.con.commit()

    def reset(self):
        self.con.execute("DELETE FROM records")
        self.con.execute("DELETE FROM meta")
        self.con.commit()
