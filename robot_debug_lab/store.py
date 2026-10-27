import json, sqlite3, threading, time, uuid
from .config import RUNTIME


class Store:
    def __init__(self, path=None):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(
            path or RUNTIME / "lab.sqlite3", check_same_thread=False
        )
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, body TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, body TEXT NOT NULL)"
        )
        for pid, body in self.db.execute("SELECT id,body FROM plans").fetchall():
            plan = json.loads(body)
            if plan["status"] in {"planning", "ready"}:
                plan["status"] = "interrupted"
                self.db.execute("UPDATE plans SET body=? WHERE id=?", (json.dumps(plan), pid))
        # Hardware/process state does not survive restarts; stale jobs must be visible.
        for rid, body in self.db.execute("SELECT id,body FROM runs").fetchall():
            r = json.loads(body)
            if r["status"] not in [
                "verified",
                "unresolved",
                "failed",
                "canceled",
                "diagnosed",
            ]:
                r.update(
                    status="interrupted", error="Server restarted before completion"
                )
                self.db.execute(
                    "UPDATE runs SET body=? WHERE id=?", (json.dumps(r), rid)
                )
        self.db.commit()

    def new(self, robot, mode, prompt):
        r = dict(
            id=uuid.uuid4().hex[:12],
            robot_id=robot,
            mode=mode,
            prompt=prompt,
            status="queued",
            created=time.time(),
            events=[],
            usage={"prompt_tokens": 0, "completion_tokens": 0},
            diagnosis=None,
            baseline=None,
            validation=None,
        )
        self.save(r)
        return r

    def save(self, r):
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?)",
                (r["id"], json.dumps(r, ensure_ascii=False)),
            )
            self.db.commit()

    def event(self, r, kind, title, data=None):
        r["events"].append(
            dict(
                id=f"E{len(r['events']) + 1}",
                time=time.time(),
                kind=kind,
                title=title,
                data=data,
            )
        )
        self.save(r)
        return r["events"][-1]

    def get(self, rid):
        with self.lock:
            row = self.db.execute("SELECT body FROM runs WHERE id=?", (rid,)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT body FROM runs ORDER BY json_extract(body, '$.created') DESC LIMIT 100"
            ).fetchall()
        return [json.loads(x[0]) for x in rows]

    def save_plan(self, plan):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO plans VALUES (?,?)",
                            (plan["id"], json.dumps(plan, ensure_ascii=False)))
            self.db.commit()

    def get_plan(self, pid):
        with self.lock:
            row = self.db.execute("SELECT body FROM plans WHERE id=?", (pid,)).fetchone()
        return json.loads(row[0]) if row else None

    def recent_plans(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT body FROM plans ORDER BY json_extract(body, '$.created') DESC LIMIT 30"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]
