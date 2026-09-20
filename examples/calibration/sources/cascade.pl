sub sync_record {
    my ($record, $remote) = @_;
    my $attempts = 0;
    while ($attempts < 3) {
        if ($record->{state} eq "ready") {
            if ($remote->reachable) {
                if ($remote->version($record->{id}) < $record->{version}) {
                    $remote->push($record);
                    return 1;
                }
            } else {
                $record->{state} = "waiting";
            }
        } else {
            $record->{state} = "queued";
        }
        $attempts++;
    }
    return 0;
}
