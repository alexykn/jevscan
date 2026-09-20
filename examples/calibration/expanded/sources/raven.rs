enum Signal {
    Ready,
    Paused,
    Lost,
}

fn choose_delivery(signals: Vec<Signal>, online: bool) -> &'static str {
    let mut selected = "waiting";
    for signal in signals {
        match signal {
            Signal::Ready => {
                if online {
                    if selected == "waiting" {
                        selected = "send";
                    } else {
                        selected = "review";
                    }
                } else {
                    selected = "defer";
                }
            }
            Signal::Paused => {
                if selected == "send" {
                    selected = "review";
                } else if selected == "waiting" {
                    selected = "defer";
                }
            }
            Signal::Lost => selected = "drop",
        }
    }
    selected
}
