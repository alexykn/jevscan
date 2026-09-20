def run_steps(request, prepare, execute, finish):
    prepared = prepare(request)
    executed = execute(prepared)
    return finish(executed)
