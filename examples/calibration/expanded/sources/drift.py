def stream_updates(source):
    try:
        for item in source.read():
            yield item
    except StopIteration:
        yield None
