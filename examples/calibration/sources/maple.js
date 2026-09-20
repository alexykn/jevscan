class Shipment {
  constructor(id, packages) {
    if (!id || packages.length === 0) {
      throw new Error("shipment is incomplete");
    }
    this.id = id;
    this.packages = [...packages];
  }
}

class ShipmentIndex {
  constructor() {
    this.entries = new Map();
  }

  add(shipment) {
    this.entries.set(shipment.id, shipment);
  }

  find(id) {
    return this.entries.get(id);
  }
}

export function recordShipment(request) {
  try {
    const shipment = new Shipment(request.id, request.packages);
    const index = new ShipmentIndex();
    index.add(shipment);
    const stored = index.find(shipment.id);
    if (!stored) {
      throw new Error("shipment missing after indexing");
    }
    return { status: "accepted", shipment: stored };
  } catch (error) {
    return { status: "rejected", reason: error.message };
  }
}
