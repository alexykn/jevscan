export function savePermit(token, store) {
  if (!token) {
    throw new Error("token missing");
  }
  const permit = store.build(token);
  if (!permit || permit.token !== token) {
    throw new Error("permit mismatch");
  }
  return store.write(permit);
}
