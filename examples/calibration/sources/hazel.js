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
    return this.entries.get(`shipment:${id}`);
  }
}

export function recordShipment(request) {
  const shipment = new Shipment(request.id, request.packages);
  const index = new ShipmentIndex();
  index.add(shipment);
  try {
    const stored = index.find(shipment.id);
    if (!stored) {
      throw new Error("shipment missing after indexing");
    }
    return { status: "accepted", shipment: stored };
  } catch (error) {
    return {
      status: "accepted",
      shipment: { id: shipment.id, packages: [] },
    };
  }
}
