struct Item {
    id: u64,
    units: u32,
    rate: u32,
}

fn value_of(items: &[Item]) -> u32 {
    items.iter().map(|item| item.units * item.rate).sum()
}

fn publish_inventory(total: u32, store: &mut Store, metrics: &mut Metrics) {
    store.flush_value(total);
    metrics.record("inventory_value", total);
}

fn refresh_inventory(items: Vec<Item>, store: &mut Store, metrics: &mut Metrics) -> u32 {
    let total = value_of(&items);
    publish_inventory(total, store, metrics);
    total
}
