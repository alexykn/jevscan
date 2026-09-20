class ParcelSink {
  constructor() {
    this.last = null;
  }

  write(value, afterWrite) {
    this.last = value;
    afterWrite();
  }
}

export function acceptParcel(parcel, sink) {
  if (!(sink instanceof ParcelSink)) {
    throw new TypeError("parcel sink required");
  }
  const state = { current: parcel };
  const accepts = value => value !== null && typeof value === "object" && value.kind === "parcel";
  if (!accepts(state.current)) {
    return null;
  }
  sink.write(state.current, () => {
    state.current = null;
  });
  if (!accepts(state.current)) {
    return null;
  }
  return state.current;
}
