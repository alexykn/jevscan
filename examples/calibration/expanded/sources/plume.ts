export class RevisionWriter {
  async write(record: { version: number }, store: Store) {
    if (record.version < 1) {
      throw new Error("unversioned");
    }
    await store.refresh();
    if (record.version < 1) {
      throw new Error("unversioned");
    }
    await store.write(record);
    return record.version;
  }
}
