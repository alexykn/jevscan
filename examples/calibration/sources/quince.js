export class Notes {
  add(note) {
    return this.writeNote(note);
  }

  writeNote(note) {
    return this.store.insert(note);
  }
}
