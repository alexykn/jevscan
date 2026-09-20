import os
import selectors
import subprocess


def process_archive(command, payload):
    input_read, input_write = os.pipe()
    output_read, output_write = os.pipe()
    error_read, error_write = os.pipe()
    open_fds = {input_read, input_write, output_read, output_write, error_read, error_write}
    selector = selectors.DefaultSelector()
    process = None
    output = bytearray()
    errors = bytearray()
    try:
        process = subprocess.Popen(
            command,
            stdin=input_read,
            stdout=output_write,
            stderr=error_write,
            close_fds=True,
        )
        os.close(input_read)
        open_fds.remove(input_read)
        os.close(output_write)
        open_fds.remove(output_write)
        os.close(error_write)
        open_fds.remove(error_write)
        os.write(input_write, payload)
        os.close(input_write)
        open_fds.remove(input_write)
        selector.register(output_read, selectors.EVENT_READ, output)
        selector.register(error_read, selectors.EVENT_READ, errors)
        while selector.get_map():
            for key, _ in selector.select(timeout=0.2):
                chunk = os.read(key.fd, 4096)
                if chunk:
                    key.data.extend(chunk)
                else:
                    selector.unregister(key.fd)
                    os.close(key.fd)
                    open_fds.remove(key.fd)
        status = process.wait()
        if status:
            raise RuntimeError(bytes(errors))
        return bytes(output)
    finally:
        selector.close()
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait()
        for fd in tuple(open_fds):
            try:
                os.close(fd)
            except OSError:
                pass
