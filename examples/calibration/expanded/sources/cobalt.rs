struct Item {
    id: u64,
    units: u32,
    rate: u32,
}

fn refresh_inventory(items: Vec<Item>, store: &mut Store, metrics: &mut Metrics) -> u32 {
    let mut total = 0;
    for item in items {
        let value = item.units * item.rate;
        store.write(item.id, value);
        metrics.record("inventory_value", value);
        total += value;
    }
    store.flush();
    total
}
