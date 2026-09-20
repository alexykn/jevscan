export function applyChange(store, entry) {
  const prepared = prepare(entry);
  store.publish(prepared);
  store.record(entry.key);
  return store.result();
}
