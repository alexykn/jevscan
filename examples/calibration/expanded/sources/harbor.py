def stream_events(events, policy, checkpoint, encode):
    iterator = iter(events)
    event = next(iterator, None)
    while event is not None:
        try:
            command = yield {
                "sequence": event["sequence"],
                "payload": encode(event),
            }
            if command == "stop":
                checkpoint.mark_stopped(event["sequence"])
                return
            if command == "pause":
                checkpoint.pause(event["sequence"])
            elif policy.accepts(event):
                checkpoint.commit(event["sequence"])
            else:
                checkpoint.reject(event["sequence"])
            event = next(iterator, None)
        except ValueError as error:
            checkpoint.record_error(event["sequence"], error)
            event = next(iterator, None)
