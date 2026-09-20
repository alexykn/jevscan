export class UserHub {
  loadAccount(id: string) {
    return this.accountStore.read(id);
  }

  renderAvatar(id: string) {
    return this.imageRenderer.render(id);
  }

  savePreference(id: string, value: string) {
    return this.preferenceStore.write(id, value);
  }
}
