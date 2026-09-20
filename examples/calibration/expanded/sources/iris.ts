export class UserDirectory {
  constructor(private store: Store) {}

  add(user: User) {
    return this.store.insert(user);
  }

  find(id: string) {
    return this.store.find(id);
  }

  remove(id: string) {
    return this.store.remove(id);
  }
}
