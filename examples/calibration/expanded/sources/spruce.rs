enum Signal {
    Ready,
    Paused,
    Lost,
}

fn choose_delivery(signals: Vec<Signal>, online: bool) -> &'static str {
    let Some(signal) = signals.last() else {
        return "waiting";
    };
    match signal {
        Signal::Ready if online => "send",
        Signal::Ready => "defer",
        Signal::Paused => "review",
        Signal::Lost => "drop",
    }
}
