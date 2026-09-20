export class LeaseBook {
  private leases = new Map<string, { owner: string; state: string }>();

  claim(id: string, owner: string) {
    this.leases.set(id, { owner, state: "held" });
  }

  release(id: string) {
    this.leases.delete(id);
  }

  expire(id: string) {
    this.leases.delete(id);
  }
}
