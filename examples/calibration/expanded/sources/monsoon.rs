struct Relay;

impl Relay {
    fn execute(&mut self, packet: Packet) -> Result<(), Error> {
        self.stage(packet);
        self.complete()
    }

    fn stage(&mut self, packet: Packet) {
        self.pending = Some(packet);
    }

    fn complete(&mut self) -> Result<(), Error> {
        let packet = self.pending.take().unwrap();
        self.send(packet)
    }

    fn send(&self, packet: Packet) -> Result<(), Error> {
        self.transport.send(packet)
    }
}
