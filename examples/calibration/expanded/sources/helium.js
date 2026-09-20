export function makeValidator(schema) {
  return function validate(value) {
    if (!schema.accepts(value)) {
      throw new Error("unacceptable");
    }
    const normalized = schema.normalize(value);
    if (!schema.accepts(normalized)) {
      throw new Error("unacceptable");
    }
    return normalized;
  };
}
