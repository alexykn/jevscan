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
  const accepts = value => value !== null && typeof value === "object" && value.kind === "parcel";
  const accepted = accepts(parcel);
  if (!accepted) {
    return null;
  }
  const deliver = () => {
    sink.write(parcel, () => {
      if (!accepted) {
        throw new Error("parcel no longer accepted");
      }
    });
    return parcel;
  };
  return deliver();
}
