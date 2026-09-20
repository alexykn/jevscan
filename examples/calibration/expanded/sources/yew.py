import subprocess


def launch_worker(command, payload):
    completed = subprocess.run(command, input=payload, capture_output=True, check=False)
    if completed.returncode:
        raise RuntimeError(completed.stderr)
    return completed.stdout


def persist_result(output, sink):
    sink.write(output)
    return len(output)


def process_archive(command, payload, sink):
    output = launch_worker(command, payload)
    return persist_result(output, sink)
