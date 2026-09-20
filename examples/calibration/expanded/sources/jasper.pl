sub merge_route {
    my ($job, $remote) = @_;
    my $tries = 0;
    while ($tries < 4) {
        if ($job->{state} eq "ready") {
            if ($remote->reachable) {
                if ($remote->version($job->{id}) < $job->{version}) {
                    if ($job->{locked}) {
                        $remote->push($job);
                        return "sent";
                    } else {
                        $job->{state} = "queued";
                    }
                } else {
                    $job->{state} = "current";
                }
            } else {
                $job->{state} = "waiting";
            }
        } elsif ($job->{state} eq "cancelled") {
            return "skipped";
        } else {
            $job->{state} = "queued";
        }
        $tries++;
    }
    return "deferred";
}
