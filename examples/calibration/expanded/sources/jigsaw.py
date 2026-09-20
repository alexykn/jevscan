def write_report(handle, report):
    if not handle.ready:
        raise RuntimeError("closed")
    handle.prepare(report)
    if not handle.ready:
        raise RuntimeError("closed")
    handle.write(report)
    return report["id"]
