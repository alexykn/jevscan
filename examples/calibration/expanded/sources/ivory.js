export function validateParcel(parcel, schema) {
  if (!schema.accepts(parcel)) {
    throw new Error("unacceptable");
  }
  return schema.normalize(parcel);
}
