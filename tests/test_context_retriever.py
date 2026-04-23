from app.context.retriever import ContextRetriever


class FakeClient:
    def __init__(self, history_messages, replies_messages=None):
        self._history = history_messages
        self._replies = replies_messages or []
        self.history_calls = 0
        self.replies_calls = 0

    def conversations_history(self, channel, latest, limit, inclusive):
        self.history_calls += 1
        # Slack returns newest -> oldest; capped by limit.
        return {"messages": list(self._history[:limit])}

    def conversations_replies(self, channel, ts, limit):
        self.replies_calls += 1
        return {"messages": list(self._replies)}


def test_builds_window_with_chronological_history():
    client = FakeClient(
        history_messages=[
            {"ts": "3.0", "user": "U1", "text": "c"},
            {"ts": "2.0", "user": "U2", "text": "b"},
            {"ts": "1.0", "user": "U3", "text": "a"},
        ]
    )
    retriever = ContextRetriever(client, window_before=10)
    source = {"ts": "4.0", "user": "U1", "text": "source"}
    window = retriever.build(conversation_id="C1", source_message=source)

    assert [m["text"] for m in window.history_before] == ["a", "b", "c"]
    assert window.source_message["text"] == "source"
    assert window.thread_messages == []


def test_includes_thread_replies_when_source_is_in_thread():
    client = FakeClient(
        history_messages=[],
        replies_messages=[
            {"ts": "1.0", "user": "U1", "text": "root"},
            {"ts": "1.1", "user": "U2", "text": "reply"},
        ],
    )
    retriever = ContextRetriever(client, window_before=5)
    source = {"ts": "1.1", "user": "U2", "text": "reply", "thread_ts": "1.0"}
    window = retriever.build(conversation_id="C1", source_message=source)

    assert window.thread_ts == "1.0"
    assert len(window.thread_messages) == 2
    # flat messages should not repeat the source
    flat = window.flat_messages()
    assert sum(1 for m in flat if m["text"] == "reply") == 1
