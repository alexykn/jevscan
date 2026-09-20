def stream_updates(source):
    try:
        for item in source.read():
            yield item
    except (OSError, ValueError) as error:
        raise RuntimeError("update stream failed") from error
