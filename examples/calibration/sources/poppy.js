export function make_checker(schema) {
  return function check(item) {
    if (!schema.accepts(item)) {
      throw new Error("invalid item");
    }
    const normalized = schema.normalize(item);
    if (!schema.accepts(normalized)) {
      throw new Error("invalid item");
    }
    return normalized;
  };
}
