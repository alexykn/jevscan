struct Order {
    account: u64,
    total: i64,
    cache_key: u64,
}

struct Receipt {
    account: u64,
    total: i64,
    items: usize,
}

struct CacheEntry {
    key: u64,
    value: i64,
    previous: Option<usize>,
    next: Option<usize>,
}

struct Cache {
    entries: Vec<Option<CacheEntry>>,
    front: Option<usize>,
    back: Option<usize>,
    capacity: usize,
    evictions: usize,
}

struct Ledger {
    pending: i64,
    committed: i64,
}

struct LedgerError;

impl Ledger {
    fn begin(&mut self) {
        self.pending = 0;
    }

    fn reserve(&mut self, amount: i64) -> Result<(), LedgerError> {
        if amount < 0 {
            return Err(LedgerError);
        }
        self.pending += amount;
        Ok(())
    }

    fn commit(&mut self) -> Result<(), LedgerError> {
        self.committed += self.pending;
        self.pending = 0;
        Ok(())
    }

    fn rollback(&mut self) {
        self.pending = 0;
    }
}

fn load_frame(
    orders: &[Order],
    cache: &mut Cache,
    ledger: &mut Ledger,
) -> Result<Receipt, LedgerError> {
    if cache.capacity == 0 {
        return Err(LedgerError);
    }
    ledger.begin();
    let mut account = 0;
    let mut total = 0;
    for order in orders {
        if account == 0 {
            account = order.account;
        }
        if let Err(error) = ledger.reserve(order.total) {
            ledger.rollback();
            return Err(error);
        }
        total += order.total;
        while cache.entries.len() >= cache.capacity {
            let oldest = match cache.front {
                Some(index) => index,
                None => break,
            };
            let successor = cache.entries[oldest]
                .as_ref()
                .and_then(|entry| entry.next);
            cache.front = successor;
            if let Some(next) = successor {
                cache.entries[next]
                    .as_mut()
                    .expect("cache link")
                    .previous = None;
            } else {
                cache.back = None;
            }
            cache.entries[oldest] = None;
            cache.evictions += 1;
        }
        let slot = cache.entries.len();
        cache.entries.push(Some(CacheEntry {
            key: order.cache_key,
            value: order.total,
            previous: cache.back,
            next: None,
        }));
        if let Some(back) = cache.back {
            cache.entries[back]
                .as_mut()
                .expect("cache link")
                .next = Some(slot);
        } else {
            cache.front = Some(slot);
        }
        cache.back = Some(slot);
    }
    if let Err(error) = ledger.commit() {
        ledger.rollback();
        return Err(error);
    }
    Ok(Receipt {
        account,
        total,
        items: orders.len(),
    })
}
