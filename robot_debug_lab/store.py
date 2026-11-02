import json, sqlite3, threading, time, uuid
from .config import RUNTIME
from .contracts import (
    TERMINAL_PHASES,
    phase_context,
    phase_for,
    validate_event,
    validate_transition,
)


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
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS experiment_labels(
                run_id TEXT PRIMARY KEY,
                experiment_id TEXT NOT NULL,
                true_cause TEXT NOT NULL,
                true_subsystem TEXT NOT NULL,
                created REAL NOT NULL
            )"""
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
                    status="interrupted",
                    phase="failed",
                    phase_managed=True,
                    phase_updated_at=time.time(),
                    phase_context=phase_context(reason="server_restarted"),
                    terminal=True,
                    finished=time.time(),
                    error="Server restarted before completion",
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
            phase="planned",
            phase_managed=False,
            phase_updated_at=time.time(),
            phase_context=phase_context(reason="run_created"),
            terminal=False,
            created=time.time(),
            finished=None,
            active_target=None,
            events=[],
            usage={"prompt_tokens": 0, "completion_tokens": 0},
            diagnosis=None,
            repair_authorization=None,
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

    def event_v2(self, r, kind, title, data, step_id=None):
        """Append a validated v2 event while preserving the legacy envelope."""
        with self.lock:
            evidence_id = f"E{len(r['events']) + 1}"
            value = dict(data)
            if kind == "tool_call":
                value["evidence_id"] = evidence_id
            validate_event(kind, value)
            event = {
                "id": evidence_id,
                "time": time.time(),
                "kind": kind,
                "title": title,
                "data": value,
                "schema_version": 2,
                "run_id": r["id"],
                "robot_id": r["robot_id"],
                "step_id": step_id,
            }
            r["events"].append(event)
            self.save(r)
            return event

    def transition(self, r, following, reason, *, activity=None, return_to=None,
                   step_id=None):
        """Persist phase and its history under one re-entrant store lock."""
        with self.lock:
            previous = phase_for(r)
            validate_transition(previous, following)
            now = time.time()
            r.update(
                phase=following,
                phase_managed=True,
                phase_updated_at=now,
                phase_context=phase_context(
                    reason=reason,
                    step_id=step_id,
                    activity=activity,
                    return_to=return_to,
                ),
                terminal=following in TERMINAL_PHASES,
            )
            if r["terminal"]:
                r["finished"] = now
            event = self.event_v2(r, "phase_changed", "任务阶段变更", {
                "from": previous,
                "to": following,
                "reason": reason,
                "activity": activity,
                "return_to": return_to,
            }, step_id=step_id)
            return event

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

    def label_experiment(self, run_id, experiment_id, true_cause, true_subsystem):
        """Write hidden evaluation truth; it is deliberately absent from run reads."""
        with self.lock:
            if not self.db.execute(
                "SELECT 1 FROM runs WHERE id=?", (run_id,)
            ).fetchone():
                raise ValueError("Cannot label an unknown run")
            self.db.execute(
                "INSERT OR REPLACE INTO experiment_labels VALUES (?,?,?,?,?)",
                (run_id, experiment_id, true_cause, true_subsystem, time.time()),
            )
            self.db.commit()

    def labeled_runs(self, *, experiment_id=None, strategies=(), limit=100):
        """Private metrics input. Never call this from public run/state reads."""
        limit = max(1, min(int(limit), 100))
        clauses, values = [], []
        if experiment_id:
            clauses.append("l.experiment_id=?")
            values.append(experiment_id)
        strategy_values = tuple(dict.fromkeys(strategies))
        if strategy_values:
            clauses.append(
                "json_extract(r.body, '$.diagnosis.strategy') IN ("
                + ",".join("?" for _ in strategy_values) + ")"
            )
            values.extend(strategy_values)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = (
            "SELECT r.body,l.experiment_id,l.true_cause,l.true_subsystem "
            "FROM experiment_labels l JOIN runs r ON r.id=l.run_id"
            + where + " ORDER BY l.created DESC"
        )
        with self.lock:
            rows = self.db.execute(query, values).fetchall()
        result = [
            {
                "run": json.loads(body),
                "experiment_id": experiment,
                "true_cause": cause,
                "true_subsystem": subsystem,
            }
            for body, experiment, cause, subsystem in rows
        ]
        return [
            item for item in result
            if phase_for(item["run"]) in TERMINAL_PHASES
        ][:limit]

    def unlabeled_run_count(self):
        with self.lock:
            row = self.db.execute(
                "SELECT COUNT(*) FROM runs r LEFT JOIN experiment_labels l "
                "ON r.id=l.run_id WHERE l.run_id IS NULL"
            ).fetchone()
        return int(row[0])

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
