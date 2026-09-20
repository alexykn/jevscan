def write_report(resource, report):
    if not resource.ready:
        raise RuntimeError("closed")
    with resource.open() as handle:
        if not handle.ready:
            raise RuntimeError("closed")
        handle.write(report)
    return report["id"]
