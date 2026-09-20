struct LeaseLedger {
    states: Store,
}

struct LeaseRuntime {
    states: Store,
}

impl LeaseLedger {
    fn claim(&mut self, id: u64, owner: Owner) {
        self.states.put(id, owner, "held");
    }

    fn release(&mut self, id: u64) {
        self.states.set_state(id, "released");
    }
}

impl LeaseRuntime {
    fn claim(&mut self, id: u64, owner: Owner) {
        self.states.put(id, owner, "held");
    }

    fn expire(&mut self, id: u64) {
        self.states.set_state(id, "expired");
    }
}

fn handoff(ledger: &mut LeaseLedger, runtime: &mut LeaseRuntime, id: u64, owner: Owner) {
    ledger.release(id);
    runtime.claim(id, owner);
}
