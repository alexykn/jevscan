export class Bridge {
  execute(payload: unknown, done: (value: unknown) => unknown) {
    return this._start(payload, done);
  }

  _start(payload: unknown, done: (value: unknown) => unknown) {
    return this._dispatch({ payload, done });
  }

  _dispatch(envelope: { payload: unknown; done: (value: unknown) => unknown }) {
    return this.transport.send(envelope.payload, (result: unknown) =>
      this._complete(envelope, result),
    );
  }

  _complete(
    envelope: { payload: unknown; done: (value: unknown) => unknown },
    result: unknown,
  ) {
    const next = envelope.done(result);
    return next ? this._dispatch({ payload: next, done: envelope.done }) : result;
  }
}
