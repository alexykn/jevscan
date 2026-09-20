sub merge_route {
    my ($job, $remote) = @_;
    return "skipped" if $job->{state} eq "cancelled";
    return "deferred" unless $remote->reachable;
    return "current" unless $remote->version($job->{id}) < $job->{version};
    $remote->push($job);
    return "sent";
}
