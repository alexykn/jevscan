def _walk_events(iterator, policy, checkpoint, encode):
    event = next(iterator, None)
    if event is None:
        return
    if policy.accepts(event):
        checkpoint.commit(event["sequence"])
        yield {"sequence": event["sequence"], "payload": encode(event)}
    yield from _walk_events(iterator, policy, checkpoint, encode)


def stream_events(events, policy, checkpoint, encode):
    yield from _walk_events(iter(events), policy, checkpoint, encode)
