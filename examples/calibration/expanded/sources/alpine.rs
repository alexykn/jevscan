struct LeaseManager {
    leases: Store,
}

impl LeaseManager {
    fn claim(&mut self, id: u64, owner: Owner) {
        self.leases.put(id, owner, "held");
    }

    fn release(&mut self, id: u64) {
        self.leases.remove(id);
    }

    fn expire(&mut self, id: u64) {
        self.leases.remove(id);
    }
}
