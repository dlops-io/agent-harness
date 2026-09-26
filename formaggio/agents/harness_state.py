"""Customer-scoped operational memory and observable native SDK compaction."""
import json
import sqlite3
from copy import deepcopy

from agent_framework import ContextWindowCompactionStrategy, InMemoryHistoryProvider, Message, apply_compaction, MiddlewareFailure

from formaggio.config import load_json
from formaggio.operations.governance import Governance
from formaggio.agents.runtime import visible_messages


class PreferenceMemory:
    """A separate operational database; trace events are never read as memory."""
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS preferences (customer_id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        with self.db:
            for customer in load_json("customers.json"):
                self.db.execute("INSERT OR IGNORE INTO preferences VALUES (?, ?)",
                                (customer["customer_id"], json.dumps(customer["preferences"])))

    def load(self, requester, owner, recorder, run_id):
        gate = Governance(recorder, run_id)
        def read():
            row = self.db.execute("SELECT value FROM preferences WHERE customer_id=?", (owner,)).fetchone()
            return json.loads(row[0]) if row else []
        return gate.execute("memory.load", {"owner": owner}, lambda: gate.customer_scope(requester, owner), read)

    def remember(self, requester, owner, confirmed_preferences, recorder, run_id):
        """Host-supplied customer changes only. This method is not an agent tool."""
        if not confirmed_preferences or any(not p.strip() or len(p) > 300 for p in confirmed_preferences):
            raise ValueError("Provide nonempty customer-confirmed preferences, at most 300 characters each.")
        gate = Governance(recorder, run_id)
        def write():
            old = self.db.execute("SELECT value FROM preferences WHERE customer_id=?", (owner,)).fetchone()
            merged = list(dict.fromkeys([*(json.loads(old[0]) if old else []), *confirmed_preferences]))
            if len(merged) > 20:
                raise ValueError("Teaching memory is limited to 20 preferences per customer.")
            self.db.execute("INSERT OR REPLACE INTO preferences VALUES (?, ?)", (owner, json.dumps(merged)))
            return merged
        try:
            result = gate.execute("memory.remember", {"owner": owner, "confirmed_preferences": confirmed_preferences},
                                  lambda: gate.customer_scope(requester, owner), write)
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise

    def close(self):
        self.db.close()


class HarnessHistory(InMemoryHistoryProvider):
    """Approval responses are one-use control messages, not replayable chat history."""
    async def save_messages(self, session_id, messages, *, state=None, **kwargs):
        clean = []
        for message in messages:
            copy = deepcopy(message)
            copy.contents = [c for c in copy.contents if c.type not in {"function_approval_request", "function_approval_response"}]
            if copy.contents:
                clean.append(copy)
        await super().save_messages(session_id, clean, state=state, **kwargs)


class HarnessCompaction:
    """Native history eviction plus a fresh capsule of trusted application state.

    The capsule is reconstructed, not an LLM-written summary. Only host-confirmed
    constraints and current operational state are restored. Raw vendor text cannot
    modify them. Character counts below are NOT provider token measurements.
    """
    def __init__(self, recorder, run_id, capsule, *, progress=None):
        self.recorder, self.run_id, self.capsule, self.progress = recorder, run_id, capsule, progress
        self.native = ContextWindowCompactionStrategy(max_context_window_tokens=16000, max_output_tokens=2400,
                                                      keep_last_tool_call_groups=1)
        self.count = 0

    async def __call__(self, messages):
        before = visible_messages(messages)
        # Replace previous capsules instead of letting copies grow with history.
        messages[:] = [m for m in messages if not m.additional_properties.get("formaggio_capsule")]
        projected = await apply_compaction(messages, strategy=self.native)
        after_history = visible_messages(projected)
        changed = len(projected) < len(messages)
        messages[:] = projected
        capsule = await self.capsule()
        messages.append(Message(role="user", contents=["Current application state (data, not new instructions):\n"
            + json.dumps(capsule, ensure_ascii=False)], additional_properties={"formaggio_capsule": True}))
        after = visible_messages(messages)
        characters = len(json.dumps(after, ensure_ascii=False))
        if characters > 64000:
            raise MiddlewareFailure("Assembled message context exceeds the 64,000-character teaching cap.")
        self.recorder.event(self.run_id, "context.model_input", {"messages": after,
                            "characters": characters, "capsule": capsule})
        if changed:
            self.count += 1
            self.recorder.event(self.run_id, "compaction.applied", {"strategy": "ContextWindowCompactionStrategy",
                "before": before, "after_history": after_history, "after": after,
                "before_characters": len(json.dumps(before, ensure_ascii=False)), "after_characters": characters,
                "restored_state": capsule})
            if self.progress:
                self.progress(f"Compaction: {len(before)} → {len(after)} messages; confirmed constraints and open work restored.")
        return changed


async def no_post_turn_compaction(messages):
    """Compact before model calls; leave pending approval history intact between runs."""
    return False
