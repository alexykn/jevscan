struct Order {
    account: u64,
    total: i64,
}

struct Receipt {
    account: u64,
    total: i64,
    items: usize,
}

#[derive(Clone)]
struct StoredReceipt {
    account: u64,
    total: i64,
    items: usize,
}

struct Cache {
    receipts: Vec<StoredReceipt>,
}

struct Ledger {
    pending: i64,
    committed: i64,
}

struct LedgerError;

fn apply_orders(orders: &[Order], ledger: &mut Ledger) -> Result<Receipt, LedgerError> {
    ledger.pending = 0;
    let mut account = 0;
    let mut total = 0;
    for order in orders {
        if order.total < 0 {
            ledger.pending = 0;
            return Err(LedgerError);
        }
        if account == 0 {
            account = order.account;
        }
        total += order.total;
        ledger.pending += order.total;
    }
    ledger.committed += ledger.pending;
    ledger.pending = 0;
    Ok(Receipt {
        account,
        total,
        items: orders.len(),
    })
}

fn remember_receipt(cache: &mut Cache, receipt: &Receipt) {
    cache.receipts.push(StoredReceipt {
        account: receipt.account,
        total: receipt.total,
        items: receipt.items,
    });
}

fn load_frame(
    orders: &[Order],
    cache: &mut Cache,
    ledger: &mut Ledger,
) -> Result<Receipt, LedgerError> {
    let receipt = apply_orders(orders, ledger)?;
    remember_receipt(cache, &receipt);
    Ok(receipt)
}
