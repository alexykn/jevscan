def run_cycle(request, loader, writer):
    state = loader.read(request)
    result = writer.prepare(state)
    return writer.commit(result)
