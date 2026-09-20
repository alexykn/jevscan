export class Bridge {
  execute(payload: { body: string }) {
    return this.dispatch(payload);
  }

  dispatch(payload: { body: string }) {
    const envelope = { sentAt: Date.now(), body: payload.body };
    this.metrics.count("dispatch");
    return this.transport.send(envelope);
  }
}
